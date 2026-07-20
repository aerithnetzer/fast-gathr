# ── Application Load Balancer ─────────────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "${var.app_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "${var.app_name}-alb" }
}

# ── Target Group ──────────────────────────────────────────────────────────────

resource "aws_lb_target_group" "app" {
  name        = "${var.app_name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # required for Fargate

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }

  tags = { Name = "${var.app_name}-tg" }
}

# ── HTTP Listener (redirects to HTTPS) ────────────────────────────────────────

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ── HTTPS Listener ────────────────────────────────────────────────────────────

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ── MCP target group + host-based routing ────────────────────────────────────

resource "aws_lb_target_group" "mcp" {
  name        = "${var.app_name}-mcp-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/health"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }

  # SSE responses are long-lived; bump the deregistration delay down so
  # rolling deploys don't drag, and disable target-group stickiness
  # (each MCP request can land on any healthy task).
  deregistration_delay = 30

  tags = { Name = "${var.app_name}-mcp-tg" }
}

resource "aws_lb_listener_rule" "mcp_https" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp.arn
  }

  condition {
    host_header {
      values = [var.mcp_domain]
    }
  }
}

# Mirror the host-based rule on the HTTP listener so requests to
# http://mcp.gathrlab.org get redirected to HTTPS like the API does.
resource "aws_lb_listener_rule" "mcp_http_redirect" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 100

  action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  condition {
    host_header {
      values = [var.mcp_domain]
    }
  }
}

# ── EMTL Historian Workflow (Django) target group + host routing ─────────────

resource "aws_lb_target_group" "emtl" {
  name        = "${var.app_name}-emtl-tg"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }

  tags = { Name = "${var.app_name}-emtl-tg" }
}

resource "aws_lb_listener_rule" "emtl_https" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 90

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.emtl.arn
  }

  condition {
    host_header {
      values = [var.emtl_domain]
    }
  }
}

resource "aws_lb_listener_rule" "emtl_http_redirect" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 90

  action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  condition {
    host_header {
      values = [var.emtl_domain]
    }
  }
}
