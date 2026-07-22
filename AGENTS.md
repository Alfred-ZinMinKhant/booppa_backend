# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

There is no `Makefile`, `pyproject.toml`, or `pytest.ini`. Discovery and configuration are mostly defaults; the few project-specific commands worth knowing:

### Tests

```bash
pytest -v                                              # full suite
pytest tests/path/to/test_file.py::test_name -v        # single test
```

Tests that hit Stripe's API **auto-skip** when `STRIPE_SECRET_KEY` doesn't start with `sk_test_`. Running with no Stripe key still gives full coverage of every non-Stripe code path — absence of failures is not evidence the Stripe paths work. End-to-end Stripe flows expect `STRIPE_TEST_PRICE_IDS_JSON` (one Price ID per SKU in `app/api/stripe_checkout.py:MODE_MAP`, ~26 entries). See `TESTING.md` for the JSON shape and a helper script.

### Dev server

Local (no Docker):

```bash
alembic upgrade head                                   # required before first boot
uvicorn app.main:app --reload --port 8000
```

Docker Compose:

```bash
docker compose up -d postgres redis
docker compose up app worker
```

- `app` runs `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `worker` runs `python -m celery -A app.workers.celery_app worker -B --loglevel=info -Q fast_queue,heavy_queue` — note `-B` (beat embedded in the worker, no separate beat process). Locally, `docker-compose.yml` splits this into two services (one `-Q fast_queue` with `-B`, one `-Q heavy_queue`); in ECS a single worker consumes both queues.
- Compose also includes `postgres:15`, `redis:7-alpine`, `django_admin`, and `browserless/chrome` (used by the PDPA scanner for headless renders).

### Migrations

```bash
alembic upgrade head                                   # apply
alembic revision --autogenerate -m "<msg>"             # create new
```

`entrypoint.sh` runs `alembic upgrade head` before uvicorn boots in containers.

### Lint / format

None configured. Do not invent a `ruff`/`black`/`isort` command — there's no config file backing one. If lint is needed, ask before adding tooling.

### Useful one-off scripts (in `scripts/`)

- `init_database.py` — bootstrap the schema from scratch.
- `validate_setup.py` — env-var sanity check.
- `seed_fake_users.py` / `seed_vendors.py` — local fixtures.
- `acra_import.py` — import the ACRA company registry.
- `sync_gebiz_now.py` — pull GeBIZ tenders synchronously (the scheduled task does this every 30 min).
- `export_stripe_price_ids.py` — emit the `STRIPE_TEST_PRICE_IDS_JSON` from a Stripe account.

## Architecture

### Routing dual-mount

`app/main.py` includes the same composite `api_router` at **both** `/api/v1` and `/api` (lines 74 and 77). Any endpoint added to the router under `app/api/` lands at both prefixes automatically — do not call `include_router` again from feature modules. The composite router itself is assembled in `app/api/__init__.py`.

### Auth

`app/core/auth.py` mints/verifies JWTs with **type discrimination** (`access`, `refresh`, `admin`, `password_reset`). Endpoints gate with `Security(oauth2_scheme)` then `verify_access_token(token)`. Passwords are bcrypt-hashed. The `oauth2_scheme` URL is `/api/v1/auth/token` but the `/api/` mount means `/api/auth/token` works equivalently.

### Models — versioned, not domain-split

`app/core/models.py` defines the core tables (`User`, `Report`, `Subscription`, etc.) and **imports `models_v6.py` through `models_v12.py` plus `models_enterprise.py` at its tail**, so Alembic's metadata picks up the full schema from a single import.

Convention: when adding a table tied to a product version N rollout, add it to `models_vN.py` rather than to `models.py`. Existing carve-outs:

- `models_v6.py` — vendor verification artifacts
- `models_v8.py` — PDPA dimension history / score snapshots
- `models_v10.py` — marketplace, funnel events, achievements, `CertificateLog`
- `models_v11.py` — compliance locker
- `models_v12.py` — API keys, **`PendingRfpIntake`** (see Stripe pipeline below)
- `models_enterprise.py` — orgs, webhooks, SSO

`User.parent_user_id` is a nullable self-FK supporting multi-subsidiary tenancy. Don't filter by `user_id` alone if a parent/child distinction matters.

### Background work — Celery on Redis

`REDIS_URL` is both broker **and** result backend. Two queues:

- `heavy_queue` — blocking work: PDF generation, blockchain anchoring, S3 uploads.
- `fast_queue` — async side effects; also the default queue (`task_default_queue="fast_queue"`) for any task without an explicit route in `celery_app.py:task_routes`.

Major flows live in `app/workers/tasks.py`:

- `process_report_task` — PDPA Quick Scan.
- `fulfill_rfp_task` — RFP Express/Complete kit generation (only queued after a brief is in; see Stripe pipeline).
- `fulfill_bundle_task` — Vendor Trust Pack / RFP Accelerator / Enterprise Bid Kit / Compliance Evidence Pack.
- `fulfill_cover_sheet_task` — generates and anchors the signed Compliance Cover Sheet.

`celery_app.conf.beat_schedule` (in `app/workers/celery_app.py`) runs ~14 cronjobs: monthly PDPA rescans (1st @ 03:00 UTC), GeBIZ tender sync every 30 min, weekly vendor-score digests, monthly compliance refresh, vendor contact scraping. Beat is **embedded in the worker** via `-B`, so the worker container is the only place schedules fire.

### Stripe purchase → fulfillment pipeline

`app/api/stripe_checkout.py` creates Checkout sessions; `app/api/stripe_webhook.py` consumes events. Both share a `MODE_MAP` that resolves product type → (Stripe mode, price ID env var).

**The recurring trip-up**: bundles that include an RFP component — `rfp_complete`, `rfp_express`, `rfp_accelerator`, `enterprise_bid_kit`, `compliance_evidence_pack` — do **not** fulfill the kit at webhook time. They defer:

1. Webhook calls `_defer_rfp_to_intake` → creates a `PendingRfpIntake` row (`models_v12.py`) with `status='pending'` + `session_id`.
2. Buyer is emailed a link to `/rfp-intake/{id}` (frontend route).
3. Buyer submits the brief → `app/api/rfp_intake.py:submit_intake` flips `status='submitted'` and queues `fulfill_rfp_task`.
4. Result lands in the cache at key `rfp_result:{session_id}`.

The post-checkout result page on the frontend polls `GET /api/stripe/checkout/verify?session_id=…` first. That endpoint resolves three signals:

- `pending_rfp_intake_id` — set when a brief is outstanding (frontend shows the brief CTA).
- `brief_satisfied` — true when (a) the kit is already cached, (b) Stripe metadata carries an `rfp_description`, or (c) a `PendingRfpIntake` *for this session* has `status='submitted'`. **Only then** does the frontend transition to "Generating…".
- `requires_brief` — true for any of the bundle product types above.

The lookup priority inside `checkout_verify` is **session-scoped first** (`PendingRfpIntake.session_id == session_id`), then the latest `status='pending'` row for the user. Do not swap this for "latest regardless of status" — that regression lets older `submitted` rows from prior cycles falsely set `brief_satisfied=True`.

Response carries `Cache-Control: no-store` because the brief state flips as the webhook fires.

### External services (`app/services/`)

- `pdf_service.py` (~100 KB) — PDPA reports + notarization PDFs. Section headers use `keepWithNext=1` to prevent orphan titles; major numbered sections (5/7/10) have explicit `PageBreak`s.
- `cover_sheet_generator.py` — Compliance Evidence Pack cover sheet, schema-versioned via `COVER_SHEET_SCHEMA_VERSION`. Bump the constant whenever visible structure changes; the UI surfaces an "outdated" badge + free regenerate to customers holding older versions.
- `email_service.py` — Resend (`RESEND_API_KEY`) preferred, falls back to AWS SES. **`send_html_email` returns `False` on provider rejection and does not raise.** Always check the return value if delivery matters; surface failures via `_alert_payment_fulfillment_issue` for fulfillment flows.
- `BlockchainService` — anchors SHA-256 hashes to Polygon Amoy via the `EvidenceAnchorV3` contract. Treat anchoring as expensive (gas) and idempotent on `report_id`.
- `S3Service` — uploads and presigned URL minting (presigns expire in 7 days — the cover sheet status endpoint re-presigns on every fetch).
- `AIService` / `BooppaAIService` — multi-provider (Anthropic, DeepSeek, OpenAI, Ollama) routed by config. Compliance narratives go through `BooppaAIService`.

### Shared PDF helpers — reuse before rolling new ones

`cover_sheet_generator.py` and `pdf_service.py` both rely on a small set of helpers and patterns: `_section_header` / `_section`, `_kv_table`, `_xml_escape`, `_pdpa_finding_block`, `_rfp_qa_block`, plus `KeepTogether` + `PageBreak` imported from `reportlab.platypus`. Prefer extending these over inventing new layout code.

User-supplied strings rendered in a `Paragraph` **must** be `_xml_escape`d — ReportLab's Paragraph mini-XML treats `&` and `<` as entity/tag starts. The "Q&A; Coverage" rendering glitch a few iterations back was exactly this.

### Frontend coordination

Sibling Next.js repo at `../booppa-nextjs`. Two polling contracts the frontend depends on — keep their response shapes stable:

- `GET /api/stripe/checkout/verify?session_id=…` → `{success, payment_status, product_type, requires_brief, brief_satisfied, pending_rfp_intake_id, customer_email, …}` with `Cache-Control: no-store`.
- `GET /api/stripe/rfp/result?session_id=…` → `202 {detail:"Not ready"}` until the worker caches the kit, then `200` with the result payload (`download_url`, `qa_answers`, `tx_hash`, etc.).

`POST /api/rfp-intake/{id}/submit` returns `session_id` so the intake page can redirect the buyer straight to `/rfp-acceleration/result?session_id=…` after submit (no "Go to dashboard" dead end).

## Deployment

`entrypoint.sh` runs `alembic upgrade head` then uvicorn. ECS deployment uses the `task-def-*.json` files at the repo root. The backend sits behind a Cloudflare Tunnel running in ECS Fargate — see `README_BACKEND.md` and `scripts/deploy_cloudflared_tunnel.sh` if tunnel work is needed.

## Pitfalls worth pre-warning

- `await EmailService().send_html_email(...)` returns `bool`. Failures are logged, not raised. Always check the return value when delivery matters.
- The webhook resolves the buyer's `User` row by `User.email == stripe_customer_email`. Stripe/DB email mismatches silently break fulfillment — the buyer pays, no row is found, no work is queued. Surface via `_alert_payment_fulfillment_issue`.
- Don't loosen the `PendingRfpIntake` lookup in `checkout_verify` to "latest regardless of status" without first checking `session_id == this session`. The regression mode is: older `submitted` row wins the `created_at desc` race, `brief_satisfied=True` returns falsely, and the result page sits on "Generating…" forever.
- New endpoints land at **both** `/api/v1/…` and `/api/…` automatically. Don't add a second `include_router` call.
- `COVER_SHEET_SCHEMA_VERSION` controls the regenerate prompt on `/compliance/cover-sheet`. Bump it whenever the visible structure of the cover sheet PDF changes, or existing customers won't get the updated layout.
