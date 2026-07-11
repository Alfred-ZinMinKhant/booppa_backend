terraform {
  required_version = ">= 1.4"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  project = var.project
}

# ECR repositories for app and worker
resource "aws_ecr_repository" "app" {
  name = "${local.project}-app"
}

resource "aws_ecr_repository" "worker" {
  name = "${local.project}-worker"
}

# S3 bucket for reports
resource "aws_s3_bucket" "reports" {
  bucket = length(var.s3_bucket_name) > 0 ? var.s3_bucket_name : "${local.project}-reports-${random_id.bucket_suffix.hex}"
  acl    = "private"

  server_side_encryption_configuration {
    rule {
      apply_server_side_encryption_by_default {
        sse_algorithm = "AES256"
      }
    }
  }
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

// ECS cluster
resource "aws_ecs_cluster" "this" {
  name = "${local.project}-cluster"
}

# RDS Postgres (simple example). Requires `private_subnet_ids` to be set.

resource "aws_db_subnet_group" "rds" {
  name       = "${local.project}-rds-subnet-group"
  subnet_ids = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.private_subnet_ids
}

resource "aws_db_instance" "postgres" {
  identifier          = "${local.project}-postgres"
  engine              = "postgres"
  instance_class      = "db.t4g.micro"
  allocated_storage   = var.db_allocated_storage
  db_name             = "${local.project}db"
  username            = var.db_username
  password            = var.db_password
  publicly_accessible = false

  # Durability (Phase 3, item #3). Multi-AZ deliberately left off to stay within
  # the Lean Mode (<$80/mo) budget — these settings buy data-loss protection, not
  # availability, at near-zero cost and no downtime.
  backup_retention_period   = var.db_backup_retention_period      # automated daily snapshots, PITR
  backup_window             = "17:00-17:30"                       # ~01:00-01:30 SGT, low traffic
  copy_tags_to_snapshot     = true
  deletion_protection       = true                                # guard against accidental delete
  skip_final_snapshot       = false                               # take a snapshot if ever destroyed
  final_snapshot_identifier = "${local.project}-postgres-final"

  multi_az             = var.rds_multi_az                          # stays false under Lean Mode
  db_subnet_group_name = aws_db_subnet_group.rds.id
  depends_on           = [aws_db_subnet_group.rds]
}

# ElastiCache Redis (single node). HA (a replication group with automatic
# failover) is Phase 3 item #4 but is intentionally DEFERRED under Lean Mode —
# it roughly doubles the cache bill and requires a recreate window. Redis here is
# only the Celery broker/result backend, so a node loss costs in-flight tasks,
# not persisted data. Revisit when the budget can absorb HA.
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.project}-redis-subnet-group"
  subnet_ids = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.private_subnet_ids
}

resource "random_id" "elasticache_suffix" {
  byte_length = 3
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id        = "${local.project}-redis-${random_id.elasticache_suffix.hex}"
  engine            = "redis"
  node_type         = "cache.t4g.micro"
  num_cache_nodes   = 1
  subnet_group_name = aws_elasticache_subnet_group.redis.name
  depends_on        = [aws_elasticache_subnet_group.redis]
}
