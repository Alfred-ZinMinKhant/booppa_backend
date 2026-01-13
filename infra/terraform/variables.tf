variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name prefix"
  type        = string
  default     = "booppa"
}

variable "vpc_id" {
  description = "Existing VPC id to deploy resources into"
  type        = string
  default     = ""
}

variable "private_subnet_ids" {
  description = "List of private subnet ids for RDS/ElastiCache/ECS"
  type        = list(string)
  default     = []
}

variable "db_username" {
  type    = string
  default = "booppa_user"
}

variable "db_password" {
  type = string
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "s3_bucket_name" {
  description = "S3 bucket name for storing generated PDFs"
  type        = string
  default     = ""
}

variable "create_vpc" {
  description = "Whether to create a new VPC (true) or use existing vpc_id/private_subnet_ids (false)"
  type        = bool
  default     = true
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (one per AZ)"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets (one per AZ)"
  type        = list(string)
  default     = ["10.0.101.0/24", "10.0.102.0/24"]
}

variable "domain_name" {
  description = "Optional domain name for ALB (e.g., booppa.io)"
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Optional Route53 hosted zone id for domain validation and record creation"
  type        = string
  default     = ""
}

variable "app_desired_count" {
  description = "ECS desired count for app service"
  type        = number
  default     = 1
}

variable "app_cpu" {
  type    = number
  default = 256
}

variable "app_memory" {
  type    = number
  default = 512
}

variable "rds_multi_az" {
  description = "Whether to enable multi-AZ for RDS"
  type        = bool
  default     = false
}
