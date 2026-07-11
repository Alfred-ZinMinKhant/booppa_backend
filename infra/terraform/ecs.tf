// ─────────────────────────────────────────────────────────────────────────────
// SOURCE OF TRUTH: CI (.github/workflows/ci.yml) — NOT Terraform.
//
// The live ECS task definitions and services (booppa-app, booppa-worker,
// booppa-beat, booppa-cms) are registered and updated imperatively by ci.yml on
// every deploy. This Terraform is DRIFTED from production and is kept only as
// reference / documentation of the intended shape. It has never been `apply`ed
// against the live account and the local state here is stale.
//
// DO NOT run `terraform apply` against this directory expecting it to be a no-op.
// Doing so can revert running services to bootstrap task definitions and clobber
// the secrets[] / valueFrom wiring that CI maintains. If you need to change a live
// service, change it in ci.yml (or via the AWS CLI/console) and mirror it here.
// ─────────────────────────────────────────────────────────────────────────────

// ECS task definition and service for the app
resource "aws_ecs_task_definition" "app" {
  family                   = "${var.project}-app"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.app_cpu)
  memory                   = tostring(var.app_memory)
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name         = "app"
      image        = "${aws_ecr_repository.app.repository_url}:latest"
      essential    = true
      portMappings = [{ containerPort = 8000, hostPort = 8000, protocol = "tcp" }]
      environment = [
        { name = "ENV", value = "production" }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "/ecs/${var.project}-app"
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_cloudwatch_log_group" "app" {
  name = "/ecs/${var.project}-app"
}

resource "aws_ecs_service" "app" {
  name            = "${var.project}-app"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.app_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.private_subnet_ids
    assign_public_ip = false
    security_groups  = [aws_security_group.ecs.id]
  }
  dynamic "load_balancer" {
    for_each = var.create_alb ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.app_tg[0].arn
      container_name   = "app"
      container_port   = 8000
    }
  }
  # depends_on removed because Terraform requires a static list; the load_balancer
  # dynamic block already creates the dependency through resource references when used.
}

// Basic worker service (runs same image but command overridden by entrypoint/command)
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = "${aws_ecr_repository.worker.repository_url}:latest"
      essential   = true
      environment = [{ name = "ENV", value = "production" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "/ecs/${var.project}-worker"
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_cloudwatch_log_group" "worker" {
  name = "/ecs/${var.project}-worker"
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  # Workers can now scale horizontally because Celery beat runs in its own
  # single-replica service (see below) rather than embedded via `--beat`.
  desired_count = var.worker_desired_count
  launch_type   = "FARGATE"
  network_configuration {
    subnets          = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.private_subnet_ids
    assign_public_ip = false
    security_groups  = [aws_security_group.ecs.id]
  }
  lifecycle {
    # CI (ci.yml) registers a new task definition and updates the service on every
    # deploy; don't let Terraform revert the service to its bootstrap task def.
    ignore_changes = [task_definition]
  }
}

// Dedicated Celery beat scheduler — MUST stay at exactly one replica so scheduled
// tasks fire once. Runs the same image; CI overrides the command to `celery beat`.
resource "aws_ecs_task_definition" "beat" {
  family                   = "${var.project}-beat"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name        = "beat"
      image       = "${aws_ecr_repository.worker.repository_url}:latest"
      essential   = true
      command     = ["python", "-m", "celery", "-A", "app.workers.celery_app", "beat", "--loglevel=info"]
      environment = [{ name = "ENV", value = "production" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "/ecs/${var.project}-beat"
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_cloudwatch_log_group" "beat" {
  name = "/ecs/${var.project}-beat"
}

resource "aws_ecs_service" "beat" {
  name            = "${var.project}-beat"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.beat.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = var.create_vpc ? [for s in aws_subnet.private : s.id] : var.private_subnet_ids
    assign_public_ip = false
    security_groups  = [aws_security_group.ecs.id]
  }
  lifecycle {
    ignore_changes = [task_definition]
  }
}
