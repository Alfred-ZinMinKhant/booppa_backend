# Booppa Feature Activation Plan
## Phase-Based Feature Rollout

Features are gated behind feature flags that auto-activate based on growth metrics.
Manual override is always available via `PUT /api/v1/features/{flag_name}`.

---

## Phase 1 — Core (Active at Launch)

All core features are live from day one:

| Feature | Status | Notes |
|---------|--------|-------|
| Vendor Proof (scan + report) | ✅ Active | Core product |
| PDPA Snapshot | ✅ Active | S$79 |
| Notarization | ✅ Active | S$69 |
| Enterprise Dashboard | ✅ Active | Multi-vendor management |
| Marketplace Directory | ✅ Active | Searchable vendor catalog |
| Funnel Analytics | ✅ Active | Event tracking from launch |
| Referral System | ✅ Active | Viral growth engine |
| Widget / Badge | ✅ Active | Embeddable trust badges |

---

## Phase 2 — Growth Engine (Auto: 50 vendors)

**Flag:** `FEATURE_COMPARISON`, `FEATURE_SEO`
**Trigger:** `marketplace_vendors >= 50` OR manual override

| Feature | Description |
|---------|-------------|
| Vendor Comparison | Side-by-side comparison matrix of 2-4 vendors |
| Programmatic SEO | Auto-generated `/vendors/{industry}` and `/vendors/top/{sector}` pages |
| Industry Pages | Landing pages per sector with vendor listings |
| Country Pages | `/vendors/country/{code}` with local vendor data |
| Sitemap Generation | Dynamic sitemap for all SEO pages |

**Expected Impact:** Organic traffic 10x, discovery funnel broadens.

---

## Phase 3 — Competitive Layer (Auto: 200 vendors)

**Flag:** `FEATURE_RANKING`, `FEATURE_COMPETITION`
**Trigger:** `marketplace_vendors >= 200` OR manual override

| Feature | Description |
|---------|-------------|
| Quarterly Leaderboard | Ranked vendor list by trust score |
| Achievements & Badges | Awarded for milestones (first scan, top 10%, etc.) |
| Score Milestones | Track vendor progress toward score targets |
| Prestige Slots | Premium placement for top performers |
| Procurement Ranking | Vendor ranking for enterprise procurement |

**Expected Impact:** Vendor engagement increases, retention improves.

---

## Phase 4 — Intelligence (Auto: 500 vendors)

**Flag:** `FEATURE_GRAPH`, `FEATURE_INSIGHT`, `FEATURE_PROCUREMENT_AUTOMATION`
**Trigger:** `marketplace_vendors >= 500` OR manual override

| Feature | Description |
|---------|-------------|
| Vendor Graph | Relationship mapping between vendors |
| Insight Dome | AI-powered market intelligence dashboard |
| Procurement Automation | RFP auto-matching and vendor shortlisting |
| API Usage Metering | Per-user API call tracking and limits |

**Expected Impact:** Enterprise value proposition strengthens, B2B pipeline.

---

## Activation Commands

```bash
# Check current metrics
curl http://localhost:8000/api/v1/features/metrics

# List all flags and their status
curl http://localhost:8000/api/v1/features/

# Manually enable a feature
curl -X PUT http://localhost:8000/api/v1/features/FEATURE_COMPARISON \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Trigger auto-activation check
curl -X POST http://localhost:8000/api/v1/features/auto-activate

# Check via Celery (runs hourly automatically)
celery -A app.workers.celery_app call auto_activation_check
```

---

## Monitoring Auto-Activation

The `auto_activation_check` Celery task runs every hour and:
1. Counts `marketplace_vendors`, `users`, `reports`
2. Compares against thresholds defined in `app/services/feature_flags.py`
3. Enables flags when thresholds are met
4. Logs all activations

Query the metrics endpoint to see current state:
```
GET /api/v1/features/metrics
→ { "vendors": 47, "users": 120, "reports": 380 }
```
