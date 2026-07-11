# Critical Fixes — Draft Diffs

Companion to `audit.md`. These are **proposals only** — nothing here has been applied.
Each maps to a Critical finding. Ordered by blast radius.

> ⚠️ Prerequisite for #1: the plaintext values currently in the ECS task defs must be
> treated as **compromised** and rotated (`STRIPE_SECRET_KEY`, `BLOCKCHAIN_PRIVATE_KEY`,
> `SECRET_KEY`, `CSP_PII_KEY_LOCAL`, `CSP_PII_SEARCH_PEPPER`, `ADMIN_PASSWORD`, `RESEND_API_KEY`,
> `CMS_ADMIN_TOKEN`, `DEEPSEEK/VIRUSTOTAL` keys). Anyone with `ecs:DescribeTaskDefinition`
> has already been able to read them.

---

## 1. Secrets out of plaintext env → Secrets Manager `secrets[]` (`ci.yml`)

**Finding:** `ci.yml:226-293` (app) and `:402-463` (worker) inject every secret as a plaintext
`environment[]` entry. Only `DATABASE_URL` uses the `secrets[]` path (`:295-297`, `:465-467`).

**Fix pattern** — for each *sensitive* var, move it from `environment` to `secrets`, sourced
from one Secrets Manager JSON secret. Store all app secrets as a single JSON secret
(e.g. `booppa/app-secrets`) and reference individual keys with the `:json-key::` ARN suffix.

Register-task-def step: replace the sensitive `environment` entries with:

```json
"secrets": [
  { "name": "DATABASE_URL",           "valueFrom": "${DB_SECRET_ARN}" },
  { "name": "STRIPE_SECRET_KEY",      "valueFrom": "${APP_SECRET_ARN}:STRIPE_SECRET_KEY::" },
  { "name": "STRIPE_WEBHOOK_SECRET",  "valueFrom": "${APP_SECRET_ARN}:STRIPE_WEBHOOK_SECRET::" },
  { "name": "SECRET_KEY",             "valueFrom": "${APP_SECRET_ARN}:SECRET_KEY::" },
  { "name": "BLOCKCHAIN_PRIVATE_KEY", "valueFrom": "${APP_SECRET_ARN}:BLOCKCHAIN_PRIVATE_KEY::" },
  { "name": "CSP_PII_KEY_LOCAL",      "valueFrom": "${APP_SECRET_ARN}:CSP_PII_KEY_LOCAL::" },
  { "name": "CSP_PII_SEARCH_PEPPER",  "valueFrom": "${APP_SECRET_ARN}:CSP_PII_SEARCH_PEPPER::" },
  { "name": "ADMIN_PASSWORD",         "valueFrom": "${APP_SECRET_ARN}:ADMIN_PASSWORD::" },
  { "name": "RESEND_API_KEY",         "valueFrom": "${APP_SECRET_ARN}:RESEND_API_KEY::" },
  { "name": "CMS_ADMIN_TOKEN",        "valueFrom": "${APP_SECRET_ARN}:CMS_ADMIN_TOKEN::" }
  // …repeat for VIRUSTOTAL_API_KEY, DEEPSEEK_API_KEY, and any Stripe price IDs you treat as private
]
```

Keep genuinely non-secret config (`ENVIRONMENT`, `ALLOWED_HOSTS`, `*_ORIGINS`,
`ANCHOR_CONTRACT_ADDRESS`, `S3_BUCKET`, `EU_SANCTIONS_XML_URL`, DB pool sizes) in `environment[]`.
Remove those secrets from the workflow-level `env:` map too (`ci.yml:134-204`, `:315-377`) —
they no longer need to reach the runner.

The **execution role** (`ecs_task_execution`) needs `secretsmanager:GetSecretValue` on
`booppa/app-secrets`; ECS injects the values at container start.

---

## 2. Least-privilege task role (`infra/terraform/iam.tf`)

**Finding:** `iam.tf:29-46` attaches `AmazonS3FullAccess`, `AmazonSESFullAccess`,
`SecretsManagerReadWrite` (write!), `CloudWatchLogsFullAccess` to the request-handling role.
The scoped policy `booppa-task-policy.json` already exists but is attached nowhere.

**Fix** — replace the four managed attachments (`iam.tf:28-47`) with the scoped inline policy:

```hcl
resource "aws_iam_role_policy" "ecs_task_scoped" {
  name   = "${var.project}-task-scoped"
  role   = aws_iam_role.ecs_task_role.id
  policy = file("${path.module}/booppa-task-policy.json")
}
```

Then tighten `booppa-task-policy.json`: change the SecretsManager `Resource:"*"` to the
specific secret ARNs, and (optionally) constrain SES with a `FromAddress`/identity condition.
`CloudWatchLogsFullAccess` on the **task** role is unnecessary — log writing is done by the
**execution** role's `AmazonECSTaskExecutionRolePolicy` (`iam.tf:18-21`), so it can be dropped.

---

## 3. RDS durability (`infra/terraform/main.tf:58-71`)

**Finding:** single-AZ, `skip_final_snapshot=true`, no encryption, no backups, no deletion
protection — a data-loss risk for a "zero tolerance for data loss" system.

```hcl
resource "aws_db_instance" "postgres" {
  identifier              = "${local.project}-postgres"
  engine                  = "postgres"
  instance_class          = var.db_instance_class          # was hard-coded db.t4g.micro
  allocated_storage       = var.db_allocated_storage
  db_name                 = "${local.project}db"
  username                = var.db_username
  password                = var.db_password
  db_subnet_group_name    = aws_db_subnet_group.rds.id
  publicly_accessible     = false
  multi_az                = var.rds_multi_az                # set default true in variables.tf

  storage_encrypted          = true                        # + KMS at rest
  backup_retention_period    = 7                            # 7 days PITR
  deletion_protection        = true
  skip_final_snapshot        = false                       # take a final snapshot on destroy
  final_snapshot_identifier  = "${local.project}-postgres-final"
  performance_insights_enabled = true

  depends_on = [aws_db_subnet_group.rds]
}
```

Note: `storage_encrypted` / `multi_az` changes force replacement or a maintenance-window
reboot — apply during a planned window with a manual snapshot taken first.

---

## 4. Redis HA (`infra/terraform/main.tf:83-90`)

**Finding:** single-node `aws_elasticache_cluster` (`num_cache_nodes=1`). Node loss drops the
Celery broker + result backend → all queued/in-flight fulfillment lost.

**Fix** — replace with a replication group with automatic failover:

```hcl
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.project}-redis"
  description                = "booppa broker + result backend"
  engine                     = "redis"
  node_type                  = var.redis_node_type          # cache.t4g.small+ for prod
  num_cache_clusters         = 2                             # 1 primary + 1 replica
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true                          # needs rediss:// in REDIS_URL
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
}
```

Caveat: Celery brokered on Redis is **not** zero-loss even with failover (in-flight acks can be
lost on promotion). If fulfillment truly cannot drop a task, that's a design item (idempotent
retries / durable outbox) tracked separately in `audit.md`, not fixable in Terraform alone.

---

## 5. Boot-time migration race (`entrypoint.sh:11-15`)

**Finding:** every replica runs `alembic upgrade head` on boot and **swallows failure**
(`|| echo "…continuing"`), so N replicas race the same migration and a broken container still
serves traffic on a half-migrated schema.

**Fix** — do not migrate on boot by default; make it opt-in and fail-fast. CI already has a
dedicated one-off migration task (`ci.yml:542-610`), which is the correct place.

```bash
# Run alembic migrations only when explicitly requested (opt-in), and fail fast.
if [ -f /app/alembic.ini ] && [ "${RUN_MIGRATIONS_ON_BOOT:-0}" = "1" ]; then
  echo "Running alembic migrations"
  alembic upgrade head          # no `|| echo` — a failed migration must abort the boot
fi
```

Gate the real deploy on the one-off migration step succeeding before `update-service` runs.

---

## 6. Worker beat + scaling (`ci.yml:401`)

**Finding:** worker command embeds `--beat`, so scaling the worker past 1 replica fires every
cron N times; combined with `desired_count=1` (`infra/terraform/ecs.tf`) the whole fulfillment
path is a SPOF.

**Fix** — split beat from workers so workers scale horizontally:

```jsonc
// worker task def (ci.yml:401): drop --beat
"command": ["python","-m","celery","-A","app.workers.celery_app","worker","--loglevel=info","-Q","fast_queue,heavy_queue"],
```

Run beat as its own single-replica ECS service:

```jsonc
"command": ["python","-m","celery","-A","app.workers.celery_app","beat","--loglevel=info"],
```

Then worker `desired_count` can go ≥2 with autoscaling on queue depth.
(Alternative if you want to stay single-service short-term: keep `--beat` but pin
`desired_count=1` and document that the worker cannot scale — not acceptable for "millions/day".)

---

## 7. Gate deploys on tests (`ci.yml`)

**Finding:** `ci.yml` runs on push to main and deploys unconditionally; `test.yml` exists but is
not a gate → a red build still ships.

**Fix** — make the build/deploy job depend on tests. Either merge test.yml's job into this
workflow and add `needs: test`, or convert to a `workflow_run` gate:

```yaml
jobs:
  test:
    uses: ./.github/workflows/test.yml   # or inline the pytest job here
  build-and-deploy:
    needs: test                          # deploy only if tests pass
    runs-on: ubuntu-latest
    permissions:                         # replace write-all (ci.yml:38) with least privilege
      id-token: write
      contents: read
```

Also add: a `push --provenance`/image scan step (ECR scan-on-push or Trivy) and a post-deploy
smoke check against `/health` before marking the deploy green.
