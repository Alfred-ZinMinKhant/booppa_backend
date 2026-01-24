# Cloudflare Tunnel — setup notes

This file documents the minimal steps, secrets, and CI integration for running a Cloudflare Tunnel (`cloudflared`) as an ECS service for the Booppa backend.

Required GitHub Secrets (add in repo Settings → Secrets):
- `CLOUDFLARE_TUNNEL_SECRET_ARN` — ARN of the Secrets Manager secret that contains the Cloudflared credentials JSON (preferred). Example: `arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:booppa/cloudflared/credentials-XXXX`
- `EXECUTION_ROLE_ARN` — ECS task execution role ARN used by Fargate tasks.
- `TASK_ROLE_ARN` — ECS task role ARN (must have permission to read the secret).
- `TUNNEL_SUBNETS` — comma-separated subnet IDs to run the tunnel in (private subnets with NAT or public if you want assignPublicIp).
- `TUNNEL_SECURITY_GROUPS` — comma-separated SG IDs for the tunnel task.
- `AWS_REGION` — AWS region (e.g. `ap-southeast-1`).

Secrets Manager: storing credentials
1. Upload the Cloudflared credentials JSON (created by `cloudflared tunnel create`) to AWS Secrets Manager:
```
aws secretsmanager create-secret \
  --name booppa/cloudflared/credentials \
  --description "Cloudflared tunnel credentials for booppa" \
  --region <AWS_REGION> \
  --secret-string file://<PATH_TO_CREDENTIALS_JSON>
```
Copy the returned `ARN` and set it as `CLOUDFLARE_TUNNEL_SECRET_ARN` in GitHub Secrets.

IAM: allow the ECS task role to read the secret
Attach this policy to the `TASK_ROLE_ARN` (or the role name):
```json
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":["secretsmanager:GetSecretValue","secretsmanager:DescribeSecret"],
      "Resource":"<SECRET_ARN>"
    }
  ]
}
```

Deploy script and CI
- Script: `scripts/deploy_cloudflared_tunnel.sh` (provided in repo). It registers a task definition using `scripts/cloudflared_task_def.json.template` and creates/updates an ECS service.
- CI: the repository CI workflow includes an optional step that runs the deploy script when `CLOUDFLARE_TUNNEL_SECRET_ARN` is set.

Example CI step (already added to `.github/workflows/ci.yml`):
```yaml
- name: Deploy Cloudflare Tunnel (optional)
  if: ${{ secrets.CLOUDFLARE_TUNNEL_SECRET_ARN != '' }}
  env:
    CLOUDFLARE_TUNNEL_SECRET_ARN: ${{ secrets.CLOUDFLARE_TUNNEL_SECRET_ARN }}
    EXECUTION_ROLE_ARN: ${{ secrets.AWS_ECS_EXECUTION_ROLE_ARN }}
    TASK_ROLE_ARN: ${{ secrets.AWS_ECS_TASK_ROLE_ARN }}
    TUNNEL_SUBNETS: ${{ secrets.TUNNEL_SUBNETS }}
    TUNNEL_SECURITY_GROUPS: ${{ secrets.TUNNEL_SECURITY_GROUPS }}
    CLUSTER_NAME: ${{ env.ECS_CLUSTER }}
    AWS_REGION: ${{ secrets.AWS_REGION }}
  run: |
    chmod +x scripts/deploy_cloudflared_tunnel.sh
    ./scripts/deploy_cloudflared_tunnel.sh
```

Post-deploy: route DNS to the tunnel
- In Cloudflare Dashboard → Zero Trust → Tunnels, add a route for your domain (e.g., `api.example.com`) pointing to the tunnel `booppa-tunnel`. Or run locally:
```
cloudflared tunnel route dns booppa-tunnel api.example.com
```

Monitoring
- CloudWatch Logs group: `/ecs/cloudflared-tunnel` (configured in the task template).

Notes & recommendations
- Prefer storing the credentials JSON in Secrets Manager and use `CLOUDFLARE_TUNNEL_SECRET_ARN` rather than embedding tokens in CI.
- Run the tunnel in private subnets where possible; the connector makes outbound connections to Cloudflare so inbound rules are not required.
- Keep the secret rotated and monitor Cloudflare tunnel health.
