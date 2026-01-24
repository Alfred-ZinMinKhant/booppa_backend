#!/usr/bin/env bash
set -euo pipefail

# Deploys a Cloudflare Tunnel as an ECS task definition and (optionally) creates/updates a service.
# Requires: aws, jq, env vars described below.

CLUSTER_NAME=${CLUSTER_NAME:-booppa-cluster}
SERVICE_NAME=${SERVICE_NAME:-cloudflared-tunnel}
TASK_FAMILY=${TASK_FAMILY:-cloudflared-tunnel}
AWS_REGION=${AWS_REGION:-ap-southeast-1}

# Determine ECR image to use (default to account ECR mirror if possible)
AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}
if [ -n "$AWS_ACCOUNT_ID" ]; then
  ECR_CLOUDFLARED_IMAGE=${ECR_CLOUDFLARED_IMAGE:-$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/cloudflared-wrapper:latest}
fi

# Input methods for tunnel credentials (choose one):
# 1) Supply CLOUDFLARE_TUNNEL_TOKEN env var (less secure)
# 2) Supply CLOUDFLARE_TUNNEL_SECRET_ARN env var (ARN in AWS Secrets Manager)

if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ -z "${CLOUDFLARE_TUNNEL_SECRET_ARN:-}" ]; then
  echo "Provide either CLOUDFLARE_TUNNEL_TOKEN or CLOUDFLARE_TUNNEL_SECRET_ARN" >&2
  exit 1
fi

EXECUTION_ROLE_ARN=${EXECUTION_ROLE_ARN:-}
TASK_ROLE_ARN=${TASK_ROLE_ARN:-}
if [ -z "$EXECUTION_ROLE_ARN" ] || [ -z "$TASK_ROLE_ARN" ]; then
  echo "Set EXECUTION_ROLE_ARN and TASK_ROLE_ARN for the ECS task definition." >&2
  exit 1
fi

TMP_JSON=$(mktemp)
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cp "$SCRIPT_DIR/cloudflared_task_def.json.template" "$TMP_JSON"

# Inject execution and task role ARNs and AWS region into the JSON using jq to avoid shell substitution issues
jq --arg exec "$EXECUTION_ROLE_ARN" --arg task "$TASK_ROLE_ARN" --arg region "$AWS_REGION" '.executionRoleArn=$exec | .taskRoleArn=$task | (.containerDefinitions[].logConfiguration.options["awslogs-region"] = $region)' "$TMP_JSON" > ${TMP_JSON}.patched
mv ${TMP_JSON}.patched "$TMP_JSON"

# If token provided inline, patch the command to use it directly and remove secrets stanza
if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  # inject token into command (entryPoint is sh -c so command must be a single string)
  jq --arg tok "$CLOUDFLARE_TUNNEL_TOKEN" '.containerDefinitions[0].command = ["/usr/local/bin/cloudflared tunnel run --token " + $tok] | .containerDefinitions[0].secrets = []' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi

# If an ECR image was detected/constructed, inject it into the task JSON
if [ -n "${ECR_CLOUDFLARED_IMAGE:-}" ]; then
  jq --arg img "$ECR_CLOUDFLARED_IMAGE" '.containerDefinitions[0].image = $img' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi


# If using Secrets Manager ARN (and NOT running in token mode), ensure the template contains the ARN value
if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ -n "${CLOUDFLARE_TUNNEL_SECRET_ARN:-}" ]; then
  jq --arg arn "$CLOUDFLARE_TUNNEL_SECRET_ARN" '.containerDefinitions[0].secrets[0].valueFrom = $arn' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi

# If a separate config secret ARN is provided, inject it into the second secrets slot
if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ -n "${CLOUDFLARE_TUNNEL_CONFIG_SECRET_ARN:-}" ]; then
  jq --arg arn "$CLOUDFLARE_TUNNEL_CONFIG_SECRET_ARN" '.containerDefinitions[0].secrets[1].valueFrom = $arn' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi

# If a separate origincert secret ARN is provided, inject it into the third secrets slot
if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ -n "${CLOUDFLARE_TUNNEL_ORIGINCERT_SECRET_ARN:-}" ]; then
  jq --arg arn "$CLOUDFLARE_TUNNEL_ORIGINCERT_SECRET_ARN" '.containerDefinitions[0].secrets[2].valueFrom = $arn' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi

# If secret (credentials JSON) is used (and token not provided), write the secret env var to a credentials file at runtime and run cloudflared with --credentials-file
if [ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ] && [ -n "${CLOUDFLARE_TUNNEL_SECRET_ARN:-}" ]; then
  # Build runtime command to write credentials, optional config, and optional origincert, then run cloudflared
  CMD="mkdir -p /home/cloudflared && printf '%s' \"\$CLOUDFLARE_TUNNEL_CREDENTIALS\" > /home/cloudflared/credentials.json"
  CMD+=" && chown 1000:1000 /home/cloudflared/credentials.json"
  if [ -n "${CLOUDFLARE_TUNNEL_CONFIG_SECRET_ARN:-}" ]; then
    CMD+=" && printf '%s' \"\$CLOUDFLARE_TUNNEL_CONFIG\" > /home/cloudflared/config.yml && chown 1000:1000 /home/cloudflared/config.yml"
  fi
  if [ -n "${CLOUDFLARE_TUNNEL_ORIGINCERT_SECRET_ARN:-}" ]; then
    CMD+=" && printf '%s' \"\$CLOUDFLARE_TUNNEL_ORIGINCERT\" > /home/cloudflared/cert.pem && export TUNNEL_ORIGIN_CERT=/home/cloudflared/cert.pem && chown 1000:1000 /home/cloudflared/cert.pem"
  fi
  # If DEBUG_SHOW_CERT is set, prepend a diagnostic that logs the cert file length and a short preview
  if [ -n "${DEBUG_SHOW_CERT:-}" ]; then
    DIAG="if [ -f /home/cloudflared/cert.pem ]; then echo \"CERT_EXISTS\"; echo \"LEN:\" \$(wc -c < /home/cloudflared/cert.pem) 2>/dev/null; echo \"BEGIN_PREVIEW\"; sed -n '1,20p' /home/cloudflared/cert.pem; echo \"END_PREVIEW\"; else echo \"CERT_MISSING\"; fi"
    CMD+=" && $DIAG"
  fi
  # If config secret provided, pass explicit config path to cloudflared
  if [ -n "${CLOUDFLARE_TUNNEL_CONFIG_SECRET_ARN:-}" ]; then
    CMD+=" && /usr/local/bin/cloudflared --no-autoupdate --loglevel info --config /home/cloudflared/config.yml tunnel --credentials-file /home/cloudflared/credentials.json run booppa-tunnel"
  else
    CMD+=" && /usr/local/bin/cloudflared --no-autoupdate --loglevel info tunnel --credentials-file /home/cloudflared/credentials.json run booppa-tunnel"
  fi

  # Inject the constructed command into the task JSON
  jq --arg cmd "$CMD" '.containerDefinitions[0].command = [$cmd]' "$TMP_JSON" > ${TMP_JSON}.2
  mv ${TMP_JSON}.2 "$TMP_JSON"
fi

echo "Registering task definition..."
aws ecs register-task-definition --cli-input-json file://"$TMP_JSON" --region "$AWS_REGION" > task-def-cloudflared-out.json
TASK_DEF_ARN=$(jq -r '.taskDefinition.taskDefinitionArn' task-def-cloudflared-out.json)
echo "Registered task definition ARN: $TASK_DEF_ARN"

# Create or update a service to run the tunnel persistently
EXISTS=$(aws ecs describe-services --cluster "$CLUSTER_NAME" --services "$SERVICE_NAME" --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null || true)

# Prepare network configuration JSON from comma-separated env vars
ASSIGN_PUBLIC_IP=${ASSIGN_PUBLIC_IP:-DISABLED}

# Require TUNNEL_SUBNETS and TUNNEL_SECURITY_GROUPS to be set for awsvpc mode
if [ -z "${TUNNEL_SUBNETS:-}" ] || [ -z "${TUNNEL_SECURITY_GROUPS:-}" ]; then
  echo "Environment variables TUNNEL_SUBNETS and TUNNEL_SECURITY_GROUPS must be set (comma-separated)." >&2
  echo "Example: TUNNEL_SUBNETS=subnet-aaa,subnet-bbb TUNNEL_SECURITY_GROUPS=sg-aaa,sg-bbb ./scripts/deploy_cloudflared_tunnel.sh" >&2
  rm -f "$TMP_JSON"
  exit 1
fi

IFS=',' read -ra SUBS_ARR <<< "${TUNNEL_SUBNETS}"
IFS=',' read -ra SGS_ARR <<< "${TUNNEL_SECURITY_GROUPS}"
subs_json=$(printf '%s\n' "${SUBS_ARR[@]}" | jq -R . | jq -s .)
sgs_json=$(printf '%s\n' "${SGS_ARR[@]}" | jq -R . | jq -s .)
network_config=$(jq -n --argjson subs "$subs_json" --argjson sgs "$sgs_json" --arg assign "$ASSIGN_PUBLIC_IP" '{awsvpcConfiguration: {subnets: $subs, securityGroups: $sgs, assignPublicIp: $assign}}')

if [ "$EXISTS" = "ACTIVE" ]; then
  echo "Updating existing service $SERVICE_NAME to new task definition"
  aws ecs update-service --cluster "$CLUSTER_NAME" --service "$SERVICE_NAME" --task-definition "$TASK_DEF_ARN" --force-new-deployment --region "$AWS_REGION"
else
  echo "Creating service $SERVICE_NAME"
  aws ecs create-service --cluster "$CLUSTER_NAME" --service-name "$SERVICE_NAME" --task-definition "$TASK_DEF_ARN" --launch-type FARGATE --desired-count 1 --network-configuration "$network_config" --region "$AWS_REGION"
fi

rm -f "$TMP_JSON"
echo "Cloudflared tunnel task deployed (or updated). Monitor logs in CloudWatch Logs group /ecs/cloudflared-tunnel."
