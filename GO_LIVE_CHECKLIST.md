# Booppa Go-Live Checklist
## Pre-Launch Infrastructure ✅

- [ ] **PostgreSQL** — Production DB provisioned, connection pooling (PgBouncer or RDS Proxy)
- [ ] **Redis** — ElastiCache or separate Redis instance for feature flags + Celery broker
- [ ] **Environment Variables** — All secrets in AWS Secrets Manager / ECS task definition:
  - `DATABASE_URL`, `REDIS_URL`
  - `SECRET_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`
  - `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`
  - `WEB3_PROVIDER_URL`, `CONTRACT_ADDRESS`, `WEB3_PRIVATE_KEY`
  - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_BUCKET`
  - `FEATURE_*` flags (all `false` at launch)
- [ ] **Domain + SSL** — Cloudflare tunnel or ALB with ACM certificate
- [ ] **Docker build** — `docker build -t booppa-backend .` passes locally

## Database Migration

- [ ] Run `alembic upgrade head` against production DB
- [ ] Verify all V10 tables created: `marketplace_vendors`, `discovered_vendors`, `import_batches`, `funnel_events`, `revenue_events`, `subscription_snapshots`, `quarterly_leaderboards`, `achievements`, `score_milestones`, `prestige_slots`, `referrals`, `enterprise_invite_tokens`, `api_usage`, `certificate_logs`, `feature_flags`
- [ ] Verify `evidence_packages.tier` column added

## Data Seed

- [ ] Run ACRA import: `python scripts/acra_import.py --out data/acra-import.csv`
- [ ] Seed marketplace vendors: `python scripts/seed_vendors.py --file data/acra-import.csv`
- [ ] Verify vendor count: `SELECT COUNT(*) FROM marketplace_vendors;`

## Feature Flag Initialization

- [ ] All phase flags start **disabled** in production
- [ ] Verify via: `GET /api/v1/features/`
- [ ] Auto-activation worker running: `celery -A app.workers.celery_app worker -B`

## Smoke Tests

- [ ] `GET /api/v1/health` → 200
- [ ] `POST /api/v1/auth/login` → JWT returned
- [ ] `GET /api/v1/marketplace/search?q=test` → results (if seeded)
- [ ] `GET /api/v1/features/` → flag list
- [ ] `POST /api/v1/funnel/track` → 200
- [ ] `GET /api/v1/widget/badge/svg/{report_id}` → SVG image
- [ ] Stripe webhook endpoint active
- [ ] WebSocket/Socket.IO connection works

## Frontend Deployment

- [ ] Next.js build: `npm run build` passes
- [ ] Environment variables set: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_STRIPE_KEY`
- [ ] Middleware public routes updated for new pages
- [ ] Sitemap generated for SEO pages

## Monitoring

- [ ] Application logs forwarded to CloudWatch / logging service
- [ ] Error alerting configured (Sentry or equivalent)
- [ ] Uptime monitoring on `/api/v1/health`
- [ ] Redis memory alerts configured

## Post-Launch (First 48h)

- [ ] Monitor funnel events flowing: `SELECT stage, COUNT(*) FROM funnel_events GROUP BY stage;`
- [ ] Check auto-activation metrics: `GET /api/v1/features/metrics`
- [ ] Verify no 500 errors on new endpoints
- [ ] Test vendor claim flow end-to-end
