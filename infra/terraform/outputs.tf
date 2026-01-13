output "ecr_app_repo" {
  value = aws_ecr_repository.app.repository_url
}

output "ecr_worker_repo" {
  value = aws_ecr_repository.worker.repository_url
}

output "s3_reports_bucket" {
  value = aws_s3_bucket.reports.bucket
}

output "ecs_cluster" {
  value = aws_ecs_cluster.this.id
}

output "rds_endpoint" {
  value = aws_db_instance.postgres.address
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "alb_dns" {
  value = var.create_alb ? aws_lb.alb[0].dns_name : ""
}

output "app_service_name" {
  value = aws_ecs_service.app.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}
