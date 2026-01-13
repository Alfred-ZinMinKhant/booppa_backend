aws_region = "ap-southeast-1"
project = "booppa"
create_vpc = true
domain_name = "booppa.io"
# hosted_zone_id = "ZXXXXXXXXXXXXX" # set your Route53 Hosted Zone ID here if you want Terraform to create DNS/ACM records

# Database (DO NOT COMMIT; set a strong password locally)
db_username = "booppa_user"
db_password = "REPLACE_WITH_STRONG_PASSWORD"

# Optional: provide a specific S3 bucket name or leave blank to let Terraform create one
s3_bucket_name = ""

# Subnet CIDRs (two AZs)
public_subnet_cidrs = ["10.0.1.0/24", "10.0.2.0/24"]
private_subnet_cidrs = ["10.0.101.0/24", "10.0.102.0/24"]

# ECS sizing
app_desired_count = 1
app_cpu = 256
app_memory = 512

# RDS HA toggle
rds_multi_az = false

# IMPORTANT: remove this file from version control before committing, or add to .gitignore
