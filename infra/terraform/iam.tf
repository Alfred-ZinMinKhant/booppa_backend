// IAM roles and policies for ECS tasks.
//
// NOTE: This directory is reference/documentation only — CI (ci.yml) is the
// source of truth for the running ECS stack (see the banner in ecs.tf). The
// execution role below already carries the Secrets Manager read grant in the
// live account (verified by app/worker/beat starting cleanly with valueFrom),
// so this file records the intended IAM shape rather than driving it.
resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.project}-ecs-exec-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role.json
}

data "aws_iam_policy_document" "ecs_task_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role_policy_attachment" "ecs_exec_attach" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

// The execution role (not the task role) is what ECS uses to resolve `secrets[]`
// valueFrom references at container start. Grant read on the app-secrets bundle
// so the app/worker/beat/cms task defs can pull STRIPE_SECRET_KEY, SECRET_KEY,
// BLOCKCHAIN_PRIVATE_KEY, etc. from Secrets Manager instead of baking them into
// the task definition. Scoped to the single secret (with the "-*" suffix so the
// 6-char Secrets Manager random suffix matches).
resource "aws_iam_role_policy" "ecs_exec_app_secrets" {
  name = "${var.project}-exec-app-secrets-read"
  role = aws_iam_role.ecs_task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = ["arn:aws:secretsmanager:${var.aws_region}:*:secret:booppa/app-secrets-*"]
    }]
  })
}

resource "aws_iam_role" "ecs_task_role" {
  name               = "${var.project}-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role.json
}

// Least-privilege inline policy for the task role: scoped S3 (reports bucket),
// read-only Secrets Manager, and SES send. Replaces the previous broad managed
// policies (AmazonS3FullAccess / AmazonSESFullAccess / SecretsManagerReadWrite /
// CloudWatchLogsFullAccess). Log writing is handled by the execution role's
// AmazonECSTaskExecutionRolePolicy, so the task role needs no CloudWatch grant.
resource "aws_iam_role_policy" "ecs_task_scoped" {
  name   = "${var.project}-task-scoped"
  role   = aws_iam_role.ecs_task_role.id
  policy = file("${path.module}/booppa-task-policy.json")
}
