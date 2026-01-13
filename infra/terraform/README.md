# Terraform starter for Booppa backend

This folder contains a minimal, opinionated starter Terraform configuration to provision AWS resources used by the backend:

- ECR repositories (`booppa-app`, `booppa-worker`)
- S3 bucket for reports
- ECS cluster (no task definitions configured)
- RDS Postgres example (requires `vpc_id` and `private_subnet_ids`)
- ElastiCache (Redis) example (requires `subnet_group`)

This is a starter template — review, harden, and adapt for production (security groups, subnet placement, backups, multi-AZ RDS, etc.).

Usage:

1. Install Terraform 1.5+ and configure AWS credentials.
2. Fill `terraform.tfvars` with required variables (see `variables.tf`).
3. Run:

```bash
terraform init
terraform plan
terraform apply
```
