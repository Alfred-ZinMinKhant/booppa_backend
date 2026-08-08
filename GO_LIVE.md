# Going live on Stripe

Production has run in **test mode since launch**. Checkout completes, webhooks
fire, entitlements are granted and kits are delivered — but no card is ever
charged and no money has ever moved. This is a deliberate state for now; this
document is what makes leaving it a checklist instead of an investigation.

Verified 2026-08-08. See `AUDIT_2026-08-08.md` P0-D.

---

## Where the credentials actually live

The audit found the frontend reads **zero** Stripe environment variables — no
secret key, no publishable key, not one price ID. Checkout is entirely
redirect-based: the browser posts to a BFF, the backend creates the Session, and
the browser is sent to Stripe's hosted page. Stripe.js is never initialised.

That means going live touches **two** places, not three:

| What | Where | How it gets there |
|---|---|---|
| `STRIPE_SECRET_KEY` | AWS Secrets Manager `booppa/app-secrets` | `ci.yml` → ECS `secrets[]` |
| `STRIPE_WEBHOOK_SECRET` | AWS Secrets Manager `booppa/app-secrets` | `ci.yml` → ECS `secrets[]` |
| ~37 `STRIPE_*` price IDs | GitHub repo secrets | `ci.yml` → ECS `environment[]` |

Everything Stripe-related currently sitting in Amplify is dead weight — see
"Clean up first" below. Delete it before switching, so there is only one copy of
the truth to change.

## Clean up first (do this now, not at go-live)

42 of 44 Amplify environment variables are read by nothing. Two of them are
credentials:

- **`STRIPE_SECRET_KEY`** — was only ever used by
  `app/api/checkout/create-session/route.ts`, deleted as P0-A. A secret key in a
  build environment that no code reads is pure liability.
- **`STRIPE_WEBHOOK_SECRET`** — same; the webhook is a backend route.

Also removable: all ~37 `STRIPE_*` price IDs, `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY`,
`CMS_ADMIN_TOKEN`, `CMS_BASE`, `NEXT_PUBLIC_API_Backend` (the Django CMS was
deleted 2026-08-05), and `SUPPORT_EMAIL` (backend-only).

**Keep exactly these two:** `COOKIE_SIGNING_SECRET` and `NEXT_PUBLIC_API_BASE`.

`NEXT_PUBLIC_MAINTENANCE_MODE` is set but read by nothing — maintenance mode is
currently inert. Either wire it up or drop it; do not rely on it in an incident.

Regenerate `COOKIE_SIGNING_SECRET` while you are in there. It has been readable
in plaintext by anyone with Amplify console access, and it is what stops a user
forging the `vendor_plan` cookie to unlock PRO routes.

## The switch

Order matters. Steps 1–3 change nothing for customers; step 4 is the cutover.

1. **Activate the Stripe account for live payments.** Business details, bank
   account, identity verification. The Delaware LLC (Booppa Smart Care LLC)
   covers this.

2. **Create the live products and prices.** Test-mode prices do not carry over.
   `scripts/export_stripe_price_ids.py` emits the current mapping — use it as
   the checklist so nothing is missed. Confirm each price against
   `app/services/pricing.py`; `tests/checkout/test_pricing_parity.py` already
   guarantees that file agrees with the storefront.

3. **Create the live webhook endpoint** at `https://api.booppa.io/api/v1/stripe/webhook`
   and copy its signing secret. **This is the step that gets forgotten**, and the
   failure mode is the worst one available: the payment succeeds, signature
   verification rejects the notification, and the customer is charged for
   something no code will ever deliver.

4. **Update both places in one deploy.** All ~37 price IDs in GitHub secrets,
   plus `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in `booppa/app-secrets`.
   Set `STRIPE_MODE=live` at the same time — `app/billing/stripe_mode.py` will
   refuse to agree with itself if the key and the declared mode disagree.

5. **Buy something with a real card.** One low-value SKU, end to end: card
   charged → webhook received → entitlement granted → deliverable received. The
   money leg has never been exercised, so nothing about it is proven by the test
   suite, which runs entirely against mocks and test mode.

## Verifying which mode you are in

Never infer this from a completed checkout, a `paid` webhook, or a delivered
kit — all four look identical in both modes. Check the credential:

```bash
# The authoritative answer, without exposing the key:
curl -s https://api.booppa.io/api/v1/health | jq '.stripe'
# → { "mode": "test", "prices_configured": 37, "config_problems": [] }
```

Every boot also logs one unmissable line:
`[Stripe] TEST MODE — no real money will move (37 price ids configured)`.

## Half-migrated states and what they look like

`app/billing/stripe_mode.py` detects these; each is pinned by a test in
`tests/checkout/test_stripe_mode_guard.py`.

| State | Symptom |
|---|---|
| live secret + test prices | every checkout 400s on "No such price" |
| live secret + test webhook secret | **payment taken, nothing fulfilled** |
| test secret + live prices | same, mirrored |
| `STRIPE_MODE` disagrees with the key | flagged at startup and on `/health` |

## While still in test mode

A real visitor who clicks Buy today gets a declined card and concludes the
product is broken — only Stripe test cards succeed. If the site is publicly
reachable and marketed before step 4, gate checkout behind a waitlist rather
than letting people fail at the payment step.
