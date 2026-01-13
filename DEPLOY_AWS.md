Deploying backend to AWS — quick steps

This file describes the exact steps to create the GitHub Secrets used by CI, run Terraform to provision AWS infra, and trigger the GitHub Actions CI to build/push images and deploy to ECS.

IMPORTANT: do not commit secrets (DB passwords, AWS keys). Create a local `terraform.tfvars` from `infra/terraform/terraform.tfvars.example` and fill secrets locally.

1) Required GitHub repository secrets

- `AWS_ACCESS_KEY_ID` (deploy user / CI credentials)
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` = `ap-southeast-1`
- `AWS_ACCOUNT_ID` (your account id)
- `AWS_ECR_REPOSITORY` (optional)
- `AWS_ECS_EXECUTION_ROLE_ARN` (ARN of the ECS task execution role)
- `AWS_ECS_TASK_ROLE_ARN` (ARN of the ECS task role)
- `AWS_ECS_WORKER_SERVICE_NAME` (the name of the worker service in ECS)

Runtime application secrets (store in AWS Secrets Manager; do not store in GitHub unless encrypted):
- `DATABASE_URL`
- `REDIS_URL`
- `STRIPE_SECRET_KEY`
- `SES_FROM`
- `S3_BUCKET` (if not created by Terraform)

Set GitHub secrets via CLI (example using `gh`):

```bash
gh secret set AWS_ACCESS_KEY_ID --body "$(cat /path/to/aws_access_key_id)" --repo <owner>/booppa_backend
gh secret set AWS_SECRET_ACCESS_KEY --body "$(cat /path/to/aws_secret)" --repo <owner>/booppa_backend
gh secret set AWS_REGION --body "ap-southeast-1" --repo <owner>/booppa_backend
gh secret set AWS_ACCOUNT_ID --body "<ACCOUNT_ID>" --repo <owner>/booppa_backend
gh secret set AWS_ECS_EXECUTION_ROLE_ARN --body "arn:aws:iam::<ACCOUNT_ID>:role/booppa-ecs-exec-role" --repo <owner>/booppa_backend
gh secret set AWS_ECS_TASK_ROLE_ARN --body "arn:aws:iam::<ACCOUNT_ID>:role/booppa-ecs-task-role" --repo <owner>/booppa_backend
gh secret set AWS_ECS_WORKER_SERVICE_NAME --body "booppa-worker" --repo <owner>/booppa_backend
```

2) Run Terraform to create infra (locally)

Copy example tfvars and fill secrets (never commit `terraform.tfvars`):

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars and set db_password and hosted_zone_id (if you want DNS/ACM)
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

After `apply` completes, note outputs: `ecr_app_repo`, `ecr_worker_repo`, `alb_dns`, `app_service_name`, `worker_service_name`. You can fetch ARNs for IAM roles if necessary:

```bash
aws iam get-role --role-name booppa-ecs-exec-role --query Role.Arn --output text
aws iam get-role --role-name booppa-ecs-task-role --query Role.Arn --output text
```

Set the returned ARNs as GitHub secrets `AWS_ECS_EXECUTION_ROLE_ARN` and `AWS_ECS_TASK_ROLE_ARN`.

3) Prepare ECR and push images (optional — CI will push images automatically)

Locally you can build and push a test image:

```bash
aws ecr get-login-password --region ap-southeast-1 | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com
docker build -t booppa-app:latest -f Dockerfile .
docker tag booppa-app:latest ${AWS_ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/booppa-app:latest
docker push ${AWS_ACCOUNT_ID}.dkr.ecr.ap-southeast-1.amazonaws.com/booppa-app:latest
```

4) Trigger CI / deploy

Push to `main` branch — the GitHub Actions workflow `.github/workflows/ci.yml` will:
- build and push `app` and `worker` images to ECR
- register two ECS task definitions (app and worker) referencing the pushed images
- update the ECS services to use the new task definition revisions

To test the pipeline manually:

```bash
git add .
git commit -m "CI: add deploy files"
git push origin main
```

5) Post-deploy tasks

- If you enabled Route53/ACM in Terraform, wait a few minutes for certificate validation and DNS propagation.
- Run DB migrations once RDS is ready (use `alembic upgrade head` either locally against `DATABASE_URL` or as an ECS run-task).
- Verify the full flow: quick-scan in frontend → Stripe checkout → webhook → worker processes → PDF in S3 + presigned URL returned.

Troubleshooting tips

- If ALB shows unhealthy targets, check the `HEALTHCHECK` path `/health` and that security groups allow traffic from ALB to ECS tasks (port 8000).
- If Playwright fails in runtime, ensure the final `app` image includes browsers (the `Dockerfile` installs Playwright). For extra reliability, run `browserless` as an internal ECS service.
- SES requires domain verification and sandbox removal for sending to unverified recipients.
