Booppa Backend — README

Overview
-	This repository contains the Booppa backend (Django/FastAPI mixed microservices layout). The current work focused on securing backend exposure by routing traffic through a Cloudflare Tunnel (cloudflared) running in ECS Fargate so ECS task public IPs are not directly exposed.

What changed (summary)
- Cloudflare Tunnel integration:
  - Added an ECS-deployable Cloudflare Tunnel based on cloudflared running in Fargate.
  - Tunnel credentials, `config.yml`, and optional origin certificate are stored in AWS Secrets Manager and injected into the container as secrets.
- Wrapper image:
  - Built a small `cloudflared-wrapper` image (Alpine base) which installs `cloudflared` and provides a startup script capable of writing secrets-to-files before launching cloudflared. This fixes issues where the official cloudflared image lacked a shell for startup scripting.
- Automation and templates:
  - `scripts/cloudflared_task_def.json.template` — ECS task definition template (runtimePlatform, secrets, logging config).
  - `scripts/deploy_cloudflared_tunnel.sh` — create/register task definition and create/update ECS service; injects ARNs and builds a runtime `sh -c` command to write secrets and run cloudflared.
  - `scripts/cloudflare_update_dns_from_ecs_tasks.sh` — helper script to update Cloudflare DNS records with current ECS task public IPs (kept for reference/debugging).
- IAM & Secrets:
  - `sm-policy.json` and `kms-policy.json` examples were created to allow ECS execution role to fetch SecretsManager values (and KMS decrypt if needed).

Files added / modified (high level)
- scripts/cloudflared-wrapper/Dockerfile — wrapper image Dockerfile.
- scripts/cloudflared_task_def.json.template — ECS task definition template.
- scripts/deploy_cloudflared_tunnel.sh — deployment script that patches the template and registers/updates ECS service.
- scripts/cloudflare_update_dns_from_ecs_tasks.sh — script to update Cloudflare DNS from ECS tasks.
- sm-policy.json, kms-policy.json — IAM policy examples.
- README_BACKEND.md — this file.

Prerequisites (local)
- aws CLI configured with credentials that can create IAM, ECR, ECS, and SecretsManager objects as needed.
- docker (and docker buildx) for building/pushing the wrapper image.
- jq installed for the deploy script.
- Cloudflare account + Cloudflare Tunnel credentials (or the tunnel created and credentials JSON uploaded to Secrets Manager).

Quick start (build & deploy tunnel)
1) Build and push the wrapper image to ECR (amd64):

```bash
export AWS_REGION=ap-southeast-1
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO=cloudflared-wrapper
ECR_URI=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest

aws ecr create-repository --repository-name ${ECR_REPO} --region ${AWS_REGION} || true
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Use buildx to ensure linux/amd64 manifest is published (recommended for Fargate X86_64)
docker buildx create --use --name cbuilder || true
docker buildx build --platform linux/amd64 -t ${ECR_URI} -f scripts/cloudflared-wrapper/Dockerfile scripts/cloudflared-wrapper --push
```

2) Ensure Secrets Manager entries exist:
- `booppa/cloudflared/credentials-<id>` — tunnel credentials JSON (tunnel credentials file).
- `booppa/cloudflared/config-<id>` — `config.yml` for cloudflared ingress (optional but recommended).
- `booppa/cloudflared/origincert-<id>` — origin cert PEM if your config requires an origin cert (optional).

3) Attach SecretsManager read permissions to the ECS execution role used by tasks. Minimal example in `sm-policy.json` — attach to `ecsTaskExecutionRole` or the execution role used by the service.

4) Deploy the tunnel (register task definition + create/update service):

```bash
# required env vars (example values)
export EXECUTION_ROLE_ARN=arn:aws:iam::<account>:role/ecsTaskExecutionRole
export TASK_ROLE_ARN=arn:aws:iam::<account>:role/booppa-ecs-task-role
export CLOUDFLARE_TUNNEL_SECRET_ARN=arn:aws:secretsmanager:<region>:<account>:secret:booppa/cloudflared/credentials-XXXXX
export CLOUDFLARE_TUNNEL_CONFIG_SECRET_ARN=arn:aws:secretsmanager:<region>:<account>:secret:booppa/cloudflared/config-XXXXX
export CLOUDFLARE_TUNNEL_ORIGINCERT_SECRET_ARN=arn:aws:secretsmanager:<region>:<account>:secret:booppa/cloudflared/origincert-XXXXX
export TUNNEL_SUBNETS=subnet-aaa,subnet-bbb
export TUNNEL_SECURITY_GROUPS=sg-aaa,sg-bbb
export AWS_REGION=ap-southeast-1

./scripts/deploy_cloudflared_tunnel.sh
```

Notes:
- The deploy script uses `jq` to patch `scripts/cloudflared_task_def.json.template` and then calls `aws ecs register-task-definition`.
- If you prefer token-mode, set `CLOUDFLARE_TUNNEL_TOKEN` instead of providing `CLOUDFLARE_TUNNEL_SECRET_ARN`.

Running & debugging
- Tail logs:
```bash
aws logs tail /ecs/cloudflared-tunnel --follow --region ap-southeast-1
```
- Check service events:
```bash
aws ecs describe-services --cluster booppa-cluster --services cloudflared-tunnel --region ap-southeast-1
```
- List tasks and describe container status:
```bash
aws ecs list-tasks --cluster booppa-cluster --service-name cloudflared-tunnel --region ap-southeast-1
aws ecs describe-tasks --cluster booppa-cluster --tasks <taskArn> --region ap-southeast-1
```

Common issues & fixes
- "exec: \"sh\": executable file not found in $PATH": This happens if the image used lacks a shell. The wrapper image includes a shell (Alpine) so this is resolved. If you still see this, ensure the task definition `image` points to the wrapper image and not the raw cloudflared image.
- Image manifest/platform mismatch: "image Manifest does not contain descriptor matching platform 'linux/amd64'": Rebuild/push using buildx and `--platform linux/amd64` so Fargate (X86_64) can pull the image.
- Origin cert missing: cloudflared may error with "Cannot determine default origin certificate path" or "Error locating origin cert" if `cert.pem` isn't present when cloudflared starts. The wrapper image writes secrets to `/home/cloudflared` prior to exec'ing cloudflared; ensure `CLOUDFLARE_TUNNEL_ORIGINCERT` secret is set and the deploy script injected the secret ARN.
- Secrets access denied: ensure the ECS execution role has permissions to call `secretsmanager:GetSecretValue` on the secrets used by the task.

Useful file locations
- scripts/cloudflared-wrapper/Dockerfile — wrapper image build
- scripts/cloudflared_task_def.json.template — task definition template
- scripts/deploy_cloudflared_tunnel.sh — deploy helper
- sm-policy.json — example minimal SecretsManager access policy

Next steps (recommended)
- Validate cloudflared registers the tunnel and Cloudflare shows the tunnel as online.
- If you want, I can:
  - Enable `DEBUG_SHOW_CERT` in the runtime command to print cert diagnostics, re-register, and redeploy.
  - Automate creation/updating of Secrets Manager secrets from local files.

Contact
- If you want me to proceed with redeploy/debugging or to commit these changes with a CI tweak, tell me which action to take next.
