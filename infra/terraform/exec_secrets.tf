variable "database_secret_arn" {
  description = "ARN of the Secrets Manager secret containing DATABASE_URL"
  type        = string
  default     = "arn:aws:secretsmanager:ap-southeast-1:997493291407:secret:booppa/DatabaseUrl-ZjhS09"
}

resource "aws_iam_policy" "exec_secrets_get" {
  name   = "${var.project}-exec-secrets-get"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = var.database_secret_arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "exec_secrets_attach" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = aws_iam_policy.exec_secrets_get.arn
}
