# Handoff — RFP Kit redesign + Tender Intelligence fix

**Date:** 2026-06-13
**Scope:** Two unrelated workstreams done in one session —
(1) Tender Intelligence digest was sending nothing; (2) RFP Kit PDF + result
page were badly laid out. Plus an admin-checkout RFP tweak.

> **State at handoff:** all code changes below are **uncommitted** in the local
> working tree (branch `main`). The *deployed* worker image already contained
> the GeBIZ dataset-ID fix (`HAS FIX: True` in prod) — so someone built from
> this tree or there's a parallel commit. **Reconcile git before pushing** so
> nothing gets clobbered. Nothing here has been committed by me.

---

## 1. Tender Intelligence digest — why subscribers got no email

`send_tender_intelligence_digest` skips (`if not rows: return`) when
`gebiz_award_history` has no awards in its window. That table was **empty** in
prod because its only writer, `refresh_gebiz_base_rates`
(`app/workers/tasks.py`), used **dead data.gov.sg dataset IDs**:

- `d_a2c0b1c04e3e55e4e8d39f86b42b0e57` → 404
- `5ab68aac-91f6-4f39-9b21-698610bdf3f7` → wrong dataset (UEN company registry;
  no award fields). It's correctly used elsewhere by
  `app/services/evidence_enricher.py` for ACRA lookups — leave that one alone.

### Fixes applied to `app/workers/tasks.py`

- **Correct dataset:** `d_acde1106003906a75c3fa052592f2fcb`
  ("Government Procurement via GeBIZ", MOF — 18,464 rows). Fields:
  `tender_no, tender_description, agency, award_date (D/M/YYYY),
  tender_detail_status, supplier_name, awarded_amt`.
- **Field mapping:** read `awarded_amt` (code was reading `award_amt`/`award_amount`);
  "awarded" gated on `tender_detail_status` (`Awarded to Suppliers` /
  `Awarded by Items`) + `supplier_name`.
- **Digest window** (`send_tender_intelligence_digest`): anchored on
  `max(awarded_date)` with a 90-day lookback instead of `current_date - 30`.
  GeBIZ data lags (latest award ~2026-03-31), so a "last 30 days from today"
  window was always empty. Email/PDF wording uses a real `period_label`.
- **Rate-limit / resilience:** `refresh_gebiz_base_rates` now retries each page
  with exponential backoff (2→4→8s, cap 30s, honours `Retry-After`) on
  `429`/`5xx`/network errors, pauses 0.6s between pages, and no longer aborts
  the whole run on one bad page. data.gov.sg 429s aggressively — this is what
  bit us when triggering manually.

### Trigger a backfill (don't wait for the Monday 02:00 UTC cron)

Inside the worker container (see AWS section), once the image has the fix:

```sh
python -c "from app.workers.tasks import refresh_gebiz_base_rates; refresh_gebiz_base_rates()"
```

Verify in psql:

```sql
SELECT count(*), max(awarded_date) FROM gebiz_award_history;   -- expect ~18k, ~2026-03
```

Test the digest send for one user:

```sh
python -c "from app.workers.tasks import send_tender_intelligence_digest_for_user; send_tender_intelligence_digest_for_user('<user_id>')"
```

> Only the **"Monthly digest email"** bullet is actually an email. The other
> Tender Intelligence features (historical lookup, bid/watch/pass, supplier
> benchmarking) are dashboard-only at `/tender-intelligence`.

### Celery queue-routing bug (found via admin first-cycle not delivering)

`app/workers/celery_app.py` had **no `task_default_queue`**, so Celery's default
(`celery`) was the fallback. The worker only consumes `-Q reports,default`.
Tasks registered with explicit short `name=`s that aren't listed in `task_routes`
(all the per-user first-cycle tasks: `send_tender_intelligence_digest_for_user`,
`run_compliance_evidence_cycle_for_user`, `run_pdpa_monitor_cycle_for_user`,
`run_vendor_active_check_for_user`, `run_vendor_pro_activation_for_user`)
routed to `celery` → **never consumed → silently dropped**. This hit **real
subscribers too**, not just admin tests — their instant first-cycle delivery
never ran. The digest *code* was fine (a direct in-process call sends correctly);
only the `.delay()` enqueue was misrouted.

**Fix:** added `task_default_queue="default"` to the Celery conf. Verified the
stranded tasks now route to `default`; `reports` tasks unchanged. Needs the
worker redeployed to take effect.

**Full audit (every Celery task):** the stranded set was large — 15 of 16 beat
tasks (all except cleanup) plus `.delay()`/`.apply_async()` sites with no
`queue=`: the five `run_*_for_user`/`send_tender_intelligence_digest_for_user`
first-cycle tasks, `anchor_signed_cover_sheet_task`, `post_payment_drip`,
`send_referral_reward_email_task`, `fire_strategy_6_task`,
`scrape_vendor_contact_task`. All now route to `default`.

**Second, separate bug fixed:** the `cleanup-old-tasks` beat entry referenced
`"app.workers.tasks.cleanup_old_tasks"` but the task is registered
`name="cleanup_old_tasks"` → beat published an unregistered name → worker
rejected it → hourly cleanup never ran. Changed the beat ref to
`"cleanup_old_tasks"`.

**⚠ Contradiction to verify on the live broker (don't skip):** static routing
says ~all scheduled tasks fell to the unconsumed `celery` queue, yet
`gebiz_tenders` has rows from today 05:26 and cover-sheet anchoring works. Likely
a pre-restart worker consumed `celery`; the current worker (restarted 05:52)
runs `-Q reports,default`, so stranding is newly active. Confirm with
`redis-cli -u "$REDIS_URL" LLEN celery` (+ `default`/`reports`) and check the ECS
worker task def's actual `-Q`. The queue fix only affects NEW enqueues — anything
already stuck in `celery` won't drain (beat re-fires on next cron; one-off
`.delay()` jobs are lost and need re-trigger).

**Not scheduled anywhere (flag):** `auto_activation_check`,
`compute_quarterly_leaderboard`, `compute_monthly_snapshot` (auto_activation.py)
are not in `beat_schedule`.

---

## 2. RFP Kit redesign (PDF + frontend)

Old PDF was a wall of text with `■■■■` tofu (`⚠`/`─` rendered in Helvetica, which
has no glyphs) and a truncated date ("13 Jun 202" — non-ISO `created_at` fell
through `_cover_strip`'s `[:10]` slice).

### Backend — `app/services/rfp_express_builder.py`

- `_build_pdf` now emits **structured data** (`report_data["rfp_kit"]` with
  `scope_intro/bullets/closing`, `details`, `checklist`,
  `checklist_confirmations`, `qa`, `template_used`, `discrepancies`) instead of
  cramming everything into one `ai_narrative` string. `created_at` is ISO.
  All symbol-free (no `⚠`/`✓`/`─`).

### Backend — `app/services/pdf_service.py`

- New `is_rfp` branch in `generate_pdf` → `_rfp_kit_story()` renders:
  amber scope callout → evidence meta-table → **real checkbox** pre-flight
  checklist (`_checkbox`/`_checklist_table`) → per-question Q&A with
  colour-coded verification captions. Blockchain section + disclaimer reused.
- Helpers added: `_pdf_escape`, `_format_date_long` (robust date parse — fixes
  truncation), `_amber_callout`, `_checkbox`, `_checklist_table`,
  `_rfp_kit_story`; styles `RfpCalloutHead`/`RfpQ`/`RfpVerif`; colors
  `AMBER`/`AMBER_BG`/`AMBER_BORDER`.

### Frontend — `../booppa-nextjs/app/rfp-acceleration/result/page.tsx`

- Restyled (unified `Card`, gradient hero header, `StatusScreen`, numbered Q&A
  cards). **All polling/state-machine logic preserved verbatim** — only the JSX
  visual layer changed. `tsc --noEmit` clean.

### Local verify

```sh
# PDF: render a sample and eyeball
python -c "from app.services.pdf_service import PDFService; ..."   # see /tmp/rfp_test.pdf approach in session
# Frontend:
cd ../booppa-nextjs && npm run dev   # visit /rfp-acceleration/result?session_id=...
```

---

## 3. Admin test checkout — skip RFP brief (done this session)

`app/api/admin.py` `simulate-purchase` + `app/api/stripe_webhook.py`
`_fulfill_bundle`: when `test_simulation` is set, RFP-bearing bundles fulfill
the kit directly with a canned QA brief instead of creating a `PendingRfpIntake`
+ emailing the brief link. Real user purchases unchanged.

**Extended to subscriptions** (`compliance_evidence_monthly`): activation
auto-fulfils the `compliance_evidence_pack` bundle via **two** paths inside
`_activate_subscription` — the first-cycle task (`run_compliance_evidence_cycle_for_user`,
~line 330) and an inline `fulfill_bundle_task` (~line 564) — both previously
omitted `test_simulation`, so the RFP component emailed a brief link even for
admin tests. Now `_activate_subscription(test_simulation=...)` threads the flag
into both metadata dicts (and into `run_compliance_evidence_cycle_for_user`'s
new `test_simulation` param), so admin CE tests auto-generate the kit.
- Files: `app/api/admin.py`, `app/api/stripe_webhook.py`, `app/workers/tasks.py`.
- **Pre-existing quirk (not fixed):** both paths fulfil the same bundle, so an
  admin CE test generates the CEP twice. Harmless for testing; worth dedup later.

---

## 3b. Suite onboarding email (Standard / Pro Suite)

Standard/Pro Suite are **access tiers** — no per-feature report to generate
(MAS TRM controls are initialised in-DB; AI gap analysis needs user-provided
context so it can't be meaningfully auto-run; notarization limits are lazy;
API keys are on-demand). Previously they only got the generic
"{label} — Activated" email. Now `_activate_subscription` (`stripe_webhook.py`)
sends a **rich itemised onboarding email** instead: one CTA per entitlement —
TRM workspace (`/vendor/trm`), gap analysis, `{50|100}` notarizations
(`/notarization`), API keys (`/vendor/api-keys`), and for Pro also SSO
(`/vendor/sso`), white-label, multi-subsidiary (`/vendor/subsidiaries`).
- Generic activation email is gated off for suites (`new_plan not in (...)`).
- Sent **synchronously** in the suite block → works on admin simulate-purchase
  and doesn't depend on the Celery queue. Notar count from
  `ENTERPRISE_NOTARIZATION_LIMITS` (`models_v8.py`).
- Decided against auto-running the 13-domain DeepSeek gap analysis at checkout
  (no user context = generic output, ~13 AI calls/purchase vs Lean Mode budget).

## 4. AWS access cheat-sheet (booppa account `997493291407`, `ap-southeast-1`)

> The Booppa account is **not** in your SSO profiles (those are Issara/Golden
> Dreams). It's the `[default]` static-key profile in `~/.aws/credentials`,
> which expires — refresh it, or just use **CloudShell** in the console
> (inherits your console login, no key juggling). CloudShell is what worked.

### Infra facts

| Thing | Value |
|---|---|
| ECS cluster | `booppa-cluster` |
| Worker service | `booppa-worker` (container name `worker`) |
| Other task families | `booppa-cms`, `cloudflared-tunnel` |
| RDS host | `booppa-postgres.cpg6ok6wwdwx.ap-southeast-1.rds.amazonaws.com` |
| DB / user | `booppadb` / `booppa_user` (RDS IAM auth, `sslmode=verify-full`) |
| Redis | Redis Labs (broker + result backend; `redis-11961.c252...redislabs.com:11961`) |
| DATABASE_URL secret | `arn:aws:secretsmanager:ap-southeast-1:997493291407:secret:booppa/DatabaseUrl-ZjhS09` |
| Task role | `booppa-ecs-task-role` · Exec role | `booppa-ecs-execution-role` |

### Connect to RDS (from CloudShell, in-VPC; laptop can't reach it — private VPC)

Use the RDS console "Connect with psql" snippet (IAM auth token as password).
The DB lives in a private VPC, so connect from CloudShell or via ECS Exec, not
your laptop.

```sql
SELECT count(*) AS total_rows,
       count(*) FILTER (WHERE awarded_date IS NOT NULL) AS with_date,
       count(*) FILTER (WHERE awarded_date >= current_date - 30) AS last_30d,
       max(awarded_date) AS most_recent
FROM gebiz_award_history;
```
(Paste as **one line** in psql to avoid the continuation-prompt mess.)

### ECS Exec into the worker (run tasks / inspect code)

```bash
# list running worker tasks
aws ecs list-tasks --cluster booppa-cluster --service-name booppa-worker \
  --desired-status RUNNING --region ap-southeast-1

# confirm exec agent is up — NOTE: managedAgents is on the CONTAINER, not the
# task. `tasks[0].managedAgents` always returns null (wrong path); use:
aws ecs describe-tasks --cluster booppa-cluster --tasks <taskId> --region ap-southeast-1 \
  --query 'tasks[0].{last:lastStatus,exec:enableExecuteCommand,agent:containers[0].managedAgents}'
# In practice the ssmmessages policy persists across deploys, so just try
# execute-command directly — don't trust a null agent reading.

# shell in
aws ecs execute-command --cluster booppa-cluster \
  --task <taskId> --container worker \
  --interactive --command "/bin/sh" --region ap-southeast-1

# inside the container — confirm the deployed image has the dataset fix:
python -c "import inspect, app.workers.tasks as t; print('HAS FIX:', 'd_acde1106003906a75c3fa052592f2fcb' in inspect.getsource(t.refresh_gebiz_base_rates))"
```

### ECS Exec was broken — what fixed it (already applied)

- Enabled exec on the service: `aws ecs update-service --cluster booppa-cluster
  --service booppa-worker --enable-execute-command --force-new-deployment --region ap-southeast-1`
- `TargetNotConnected` / `agent: null` persisted → **task role lacked SSM perms**.
  Added inline policy **`ecs-exec-ssm`** to `booppa-ecs-task-role` granting
  `ssmmessages:CreateControlChannel/CreateDataChannel/OpenControlChannel/OpenDataChannel`,
  then forced a new deployment. Exec then connected. (If it ever breaks again,
  check that policy is still attached.)

---

## 5. TODO / next session

- [ ] **Reconcile git** (deployed worker has dataset fix but tree is uncommitted)
      then **commit** the 4 files: `app/workers/tasks.py`,
      `app/services/pdf_service.py`, `app/services/rfp_express_builder.py`
      (backend) + `app/rfp-acceleration/result/page.tsx` (frontend).
      Plus admin checkout: `app/api/admin.py`, `app/api/stripe_webhook.py`.
- [ ] **Build + push** the worker image so the rate-limit backoff is live for
      the Monday cron (current deployed image has the dataset fix but NOT the
      backoff).
- [ ] **Backfill** `gebiz_award_history` (trigger `refresh_gebiz_base_rates`,
      then verify ~18k rows) and **send a test digest**.
- [ ] Eyeball the restyled `/rfp-acceleration/result` page in a browser.
- [ ] Regenerate a real RFP kit and confirm the new PDF layout in prod.
