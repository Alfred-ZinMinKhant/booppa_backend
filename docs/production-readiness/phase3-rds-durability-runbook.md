# Phase 3 — RDS Durability Runbook (Lean Mode)

**Scope:** Data-loss protection only (item #3). Multi-AZ and Redis HA (item #4) are
**deferred** under Lean Mode (<$80/mo) — see the comments in `infra/terraform/main.tf`.

**Why these and not Multi-AZ:** automated backups + deletion protection + a final
snapshot stop *data loss* at ~zero cost and no downtime. Multi-AZ buys *availability*
(surviving an AZ outage) at roughly 2× the DB bill — a separate decision.

**Cost delta of this runbook:** essentially $0. Backup storage is free up to 100% of
the DB's provisioned storage (20 GB here), which we will never exceed.

---

## Preconditions

- Run in a low-traffic window (backup window below is set to ~01:00–01:30 SGT).
- AWS creds with `rds:*` on the instance. Region: `ap-southeast-1`.
- Instance identifier: `booppa-postgres`.

## Step 0 — Take a manual snapshot first (standing rule)

```bash
AWS_REGION=ap-southeast-1
aws rds create-db-snapshot \
  --db-instance-identifier booppa-postgres \
  --db-snapshot-identifier booppa-postgres-pre-phase3-$(date +%Y%m%d) \
  --region "$AWS_REGION"

# Wait until it's available before proceeding:
aws rds wait db-snapshot-completed \
  --db-snapshot-identifier booppa-postgres-pre-phase3-$(date +%Y%m%d) \
  --region "$AWS_REGION"
```

## Step 1 — Confirm current durability posture

```bash
aws rds describe-db-instances \
  --db-instance-identifier booppa-postgres \
  --region "$AWS_REGION" \
  --query 'DBInstances[0].{Backups:BackupRetentionPeriod,Window:PreferredBackupWindow,DeleteProtect:DeletionProtection,MultiAZ:MultiAZ}'
```

If `Backups` is already ≥ 7 and `DeleteProtect` is `true`, you're done — nothing to change.

## Step 2 — Apply durability settings (single live modify, no downtime)

Enabling automated backups when retention was previously 0 triggers a **brief I/O
pause the first time only**; going 1→7 (the RDS console default is 1) does not. Either
way it's applied online, so `--apply-immediately` is safe:

```bash
aws rds modify-db-instance \
  --db-instance-identifier booppa-postgres \
  --backup-retention-period 7 \
  --preferred-backup-window 17:00-17:30 \
  --deletion-protection \
  --copy-tags-to-snapshot \
  --apply-immediately \
  --region "$AWS_REGION"
```

> Note: `skip_final_snapshot=false` / `final_snapshot_identifier` are **delete-time**
> behaviors — there is no live API flag for them. They take effect only if the instance
> is ever destroyed. With `deletion-protection` now on, a destroy is blocked anyway,
> which is the stronger guard. These are captured in Terraform for the intended shape.

## Step 3 — Verify

```bash
aws rds describe-db-instances \
  --db-instance-identifier booppa-postgres \
  --region "$AWS_REGION" \
  --query 'DBInstances[0].{Backups:BackupRetentionPeriod,Window:PreferredBackupWindow,DeleteProtect:DeletionProtection}'
```

Expect `Backups: 7`, `Window: 17:00-17:30`, `DeleteProtect: true`.

## Recovery expectations after this change

- **RPO:** ~5 min (RDS point-in-time recovery replays to any second within the 7-day
  window; worst case is the ~5-min transaction-log flush interval).
- **RTO:** minutes-to-low-tens-of-minutes — PITR provisions a *new* instance, so plan to
  repoint `DATABASE_URL` (it's synced from the GitHub secret into `booppa/app-secrets`,
  so update the GitHub secret and redeploy, or edit the secret directly for speed).
- **Not covered (deferred):** AZ-outage survival — that needs Multi-AZ. A single-AZ RDS
  failure still requires a manual PITR/restore. Accepted trade-off under Lean Mode.

## Rollback

Durability settings are additive and safe; there is nothing to roll back. If you must
revert deletion protection to run maintenance that requires a replace:

```bash
aws rds modify-db-instance --db-instance-identifier booppa-postgres \
  --no-deletion-protection --apply-immediately --region "$AWS_REGION"
```

---

## Deferred (item #4, Redis HA)

Redis is the Celery broker/result backend only — no persisted business data — so a node
loss costs in-flight background tasks, not customer data. HA (a replication group with
automatic failover) roughly doubles the cache bill and needs a recreate window. Revisit
when the budget can absorb it; the Terraform block in `main.tf` documents the current
single-node shape and the deferral.
