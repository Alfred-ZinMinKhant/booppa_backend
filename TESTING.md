# Testing & CI Setup

This is the canonical reference for the test suite + CI workflows across both
`booppa_backend` and `booppa-nextjs`. Run through it once when bootstrapping
the repo on a new GitHub org or refreshing a Stripe test account.

## Local quickstart

```bash
# Backend
docker compose up -d postgres redis
pip install -r requirements.txt
alembic upgrade head
pytest -v

# Frontend (in ../booppa-nextjs)
npm install --legacy-peer-deps
npm run test:e2e:install      # one-time: download Chromium
npm run build && npm start &  # background; Playwright reuses it
npm run test:e2e
```

Tests that need a real Stripe test key are auto-skipped if `STRIPE_SECRET_KEY`
doesn't start with `sk_test_` — you can run the full suite without credentials
and still get coverage of every code path that doesn't touch Stripe's API.

## GitHub Actions secrets

Set these in **both repos**' Settings → Secrets and variables → Actions, unless
noted otherwise.

| Secret | Where used | How to obtain |
|---|---|---|
| `STRIPE_TEST_SECRET_KEY` | both | Stripe dashboard → Developers → API keys → "Reveal test key" (`sk_test_…`) |
| `STRIPE_TEST_PUBLISHABLE_KEY` | frontend only | Stripe dashboard → API keys (`pk_test_…`); needed for the smoke-card flow |
| `STRIPE_TEST_WEBHOOK_SECRET` | both | Stripe dashboard → Developers → Webhooks → add `https://<dev-url>/api/v1/stripe/webhook`, copy the signing secret (`whsec_…`). For local-only tests, any `whsec_test_…` string works since the workflow generates payloads locally. |
| `STRIPE_TEST_PRICE_IDS_JSON` | both | A JSON object mapping `STRIPE_<UPPER_PRODUCT_TYPE>` → price ID. Example:<br>`{"STRIPE_VENDOR_PROOF":"price_1Abc…","STRIPE_PDPA_QUICK_SCAN":"price_1Def…", …}`<br>One entry per SKU in `app/api/stripe_checkout.py:MODE_MAP` (26 entries). |
| `PLAYWRIGHT_TEST_JWT` | frontend only | A pre-issued backend JWT for a seeded test user. Mint with: `python -c 'from app.core.auth import create_access_token; print(create_access_token({"sub":"qa+playwright@booppa.io"}))'` against the same `SECRET_KEY` the test backend uses. Required only for `api-direct.spec.ts` and `smoke-full-flow.spec.ts` — others auto-skip. |
| `GH_PAT_BACKEND_READ` | frontend only | A fine-scoped GitHub PAT with `Contents: Read` on `booppa_backend`. The frontend workflow checks out the backend as a sibling to boot uvicorn. Use a [fine-grained PAT](https://github.com/settings/personal-access-tokens) restricted to `booppa_backend`. |
| `TEST_AWS_ACCESS_KEY_ID` | backend | IAM user scoped to `booppa-reports/test/*` only — see "Real-AWS S3 setup" below. Used by tests that opt into the `real_s3` fixture. |
| `TEST_AWS_SECRET_ACCESS_KEY` | backend | Secret for the above IAM user. |

### Generating `STRIPE_TEST_PRICE_IDS_JSON`

The 26 SKUs in MODE_MAP each need a Stripe Price ID in test mode. Easiest path:

1. In the Stripe test dashboard, create a Product for each SKU (one-time or
   recurring as appropriate — see `MODE_MAP`).
2. Note each Price ID (`price_…`).
3. Assemble into JSON. Helper script:

   ```bash
   python - <<'PY'
   import json
   SKUS = [
     "VENDOR_PROOF", "PDPA_QUICK_SCAN",
     "RFP_EXPRESS", "RFP_COMPLETE",
     "COMPLIANCE_NOTARIZATION_1", "COMPLIANCE_NOTARIZATION_10", "COMPLIANCE_NOTARIZATION_50",
     "VENDOR_TRUST_PACK", "RFP_ACCELERATOR", "ENTERPRISE_BID_KIT", "COMPLIANCE_EVIDENCE_PACK",
     "VENDOR_ACTIVE_MONTHLY", "VENDOR_ACTIVE_ANNUAL",
     "PDPA_MONITOR_MONTHLY", "PDPA_MONITOR_ANNUAL",
     "ENTERPRISE_MONTHLY", "ENTERPRISE_PRO_MONTHLY",
     "STANDARD_SUITE_MONTHLY", "PRO_SUITE_MONTHLY",
     "EVALUATE_SUPPLIERS_MONTHLY", "VERIFY_SUPPLIER_EVIDENCE_MONTHLY",
     "COMPLIANCE_EVIDENCE_MONTHLY",
     "TENDER_INTELLIGENCE_MONTHLY", "TENDER_INTELLIGENCE_ANNUAL",
     "VENDOR_PRO_MONTHLY", "VENDOR_PRO_ANNUAL",
   ]
   # Replace each value with the matching price_… from your Stripe test dashboard
   out = {f"STRIPE_{s}": "price_REPLACE_ME" for s in SKUS}
   print(json.dumps(out, indent=2))
   PY
   ```

4. Paste the resulting JSON (one line, no indentation) into the
   `STRIPE_TEST_PRICE_IDS_JSON` secret.

The CI workflows decode the JSON and export each entry as an individual env
var (`STRIPE_VENDOR_PROOF`, etc.) before `pytest` / Playwright runs, matching
the lookup pattern in `_get_price()` at `app/api/stripe_checkout.py:21`.

## Real-AWS S3 setup

Tests using the `real_s3` fixture write PDFs to the production `booppa-reports`
bucket under a `test/<run-id>/` prefix and delete them on teardown. To make
that safe:

### 1. Add a lifecycle rule (one-time, via AWS console or CLI)

Expires anything under `test/` after 1 day so a crashed test can't leave
orphans. Apply via console (S3 → bucket → Management → Lifecycle rules → Create
rule) with:
- **Prefix filter:** `test/`
- **Expiration:** 1 day after object creation
- **Delete expired delete markers:** yes (optional)

Or via CLI:

```bash
aws s3api put-bucket-lifecycle-configuration --bucket booppa-reports \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-test-uploads",
      "Status": "Enabled",
      "Filter": { "Prefix": "test/" },
      "Expiration": { "Days": 1 }
    }]
  }'
```

### 2. Create a scoped IAM user

Create user `booppa-tests` with this policy — it can only touch `test/`
keys, never production ones:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TestPrefixObjectOps",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::booppa-reports/test/*"
    }
  ]
}
```

Generate an access key for this user and store it in the
`TEST_AWS_ACCESS_KEY_ID` / `TEST_AWS_SECRET_ACCESS_KEY` GitHub secrets.

### 3. Confirm the fixture works locally

```bash
export AWS_ACCESS_KEY_ID=…    # the booppa-tests user
export AWS_SECRET_ACCESS_KEY=…
pytest -v tests/pdf/test_pdf_s3_real.py
```

The test uploads a PDPA PDF and the fixture deletes it; you can verify in S3
that no `test/<run-id>/...` keys remain after the run.

## What CI runs

### `booppa_backend/.github/workflows/test.yml`
- `services`: postgres:15, redis:7
- Installs Python deps, runs `alembic upgrade head`, exports Stripe price IDs
  from the JSON secret, runs `pytest -v --cov=app tests/`.
- Coverage is uploaded as an artifact.

### `booppa-nextjs/.github/workflows/test.yml`
- Job 1 `lint-typecheck`: `npm ci && npm run lint && tsc --noEmit`.
- Job 2 `e2e`: checks out `booppa_backend` as a sibling using
  `GH_PAT_BACKEND_READ`, boots its uvicorn + Postgres + Redis, builds and
  starts Next.js, runs Playwright. Reports + backend logs uploaded on failure.

## Troubleshooting

- **`STRIPE_SECRET_KEY` not a sk_test_ key** — `stripe_test_mode` fixture
  skips the per-SKU `test_create_session.py` cases. Either set a real test key
  in the secret or accept the skip (other coverage still runs).
- **`pg_isready` failures in CI** — increase the `--health-retries` on the
  Postgres service.
- **Playwright bundle-modal test skips** — UI selectors changed. Update the
  text patterns in `e2e/flows/bundle-modal.spec.ts`; consider adding
  `data-test-checkout="<product_type>"` to the pricing-page buttons so future
  selectors are stable (this would also enable per-SKU UI click tests).
- **`Stripe — checkout returned 400 / "Invalid product type or price not configured"`** —
  the SKU isn't in `STRIPE_TEST_PRICE_IDS_JSON` or a typo in the env-var name.
  Confirm by reading the GH Actions log line for the failing SKU, then update
  the JSON secret.
