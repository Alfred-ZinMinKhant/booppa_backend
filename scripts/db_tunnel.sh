#!/usr/bin/env bash
# Tunnel localhost:5433 -> booppa-postgres, via SSM port forwarding through a
# running booppa-app ECS task. No bastion, no open ports, no IP allowlist.
# Requires: ECS Exec enabled on the service (it is), session-manager-plugin.
set -euo pipefail

CLUSTER=${CLUSTER:-booppa-cluster}
SERVICE=${SERVICE:-booppa-app}
LOCAL_PORT=${LOCAL_PORT:-5433}
DB_HOST=${DB_HOST:-booppa-postgres.cpg6ok6wwdwx.ap-southeast-1.rds.amazonaws.com}

TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
  --desired-status RUNNING --query 'taskArns[0]' --output text | awk -F/ '{print $NF}')
if [ -z "$TASK" ] || [ "$TASK" = "None" ]; then
  echo "No running task in $CLUSTER/$SERVICE" >&2
  exit 1
fi
RUNTIME=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
  --query 'tasks[0].containers[0].runtimeId' --output text)

echo "Tunnelling localhost:${LOCAL_PORT} -> ${DB_HOST}:5432 via task ${TASK}"
echo "Connect with: psql \"host=localhost port=${LOCAL_PORT} dbname=<db> user=<user>\""

exec aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK}_${RUNTIME}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${DB_HOST}\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
