// Security groups
resource "aws_security_group" "alb" {
  name        = "${var.project}-alb-sg"
  description = "Allow HTTP(s) to ALB"
  vpc_id      = var.create_vpc ? aws_vpc.this[0].id : var.vpc_id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ecs" {
  name        = "${var.project}-ecs-sg"
  description = "ECS tasks SG"
  vpc_id      = var.create_vpc ? aws_vpc.this[0].id : var.vpc_id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

// ALB
resource "aws_lb" "alb" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.create_vpc ? [for s in aws_subnet.public : s.id] : var.public_subnet_ids
}

resource "aws_lb_target_group" "app_tg" {
  name     = "${var.project}-tg"
  port     = 8000
  protocol = "HTTP"
  vpc_id   = var.create_vpc ? aws_vpc.this[0].id : var.vpc_id
  health_check {
    path = "/health"
    matcher = "200-399"
    interval = 30
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.alb.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app_tg.arn
  }
}

// Optional: create ACM cert and Route53 record if hosted_zone_id and domain_name provided
resource "aws_acm_certificate" "cert" {
  count = length(var.domain_name) > 0 && length(var.hosted_zone_id) > 0 ? 1 : 0
  domain_name = var.domain_name
  validation_method = "DNS"
}

resource "aws_route53_record" "cert_validation" {
  count = length(var.domain_name) > 0 && length(var.hosted_zone_id) > 0 ? length(aws_acm_certificate.cert[0].domain_validation_options) : 0
  zone_id = var.hosted_zone_id
  name    = aws_acm_certificate.cert[0].domain_validation_options[count.index].resource_record_name
  type    = aws_acm_certificate.cert[0].domain_validation_options[count.index].resource_record_type
  records = [aws_acm_certificate.cert[0].domain_validation_options[count.index].resource_record_value]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "cert_validation" {
  count = length(var.domain_name) > 0 && length(var.hosted_zone_id) > 0 ? 1 : 0
  certificate_arn = aws_acm_certificate.cert[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

// Create alias record for ALB if hosted zone provided
resource "aws_route53_record" "alb_record" {
  count = length(var.domain_name) > 0 && length(var.hosted_zone_id) > 0 ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_lb.alb.dns_name
    zone_id                = aws_lb.alb.zone_id
    evaluate_target_health = true
  }
}
