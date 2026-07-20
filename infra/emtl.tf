# ── EMTL Historian Workflow (Django) ─────────────────────────────────────────
# Deployed as a third ECS service behind the shared ALB, routed by the
# app.gathrlab.org host header. Talks to the existing gathr Postgres DB as
# the least-privilege emtl_django role. Site-wide password protection is
# provided by Cloudflare Access in front of the ALB (configured out of band).

# ── ECR repository ────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "emtl" {
  name                 = "${var.app_name}-emtl"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "${var.app_name}-emtl" }
}

resource "aws_ecr_lifecycle_policy" "emtl" {
  repository = aws_ecr_repository.emtl.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the last 10 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ── Secrets ───────────────────────────────────────────────────────────────────
# The DATABASE_URL secret is provisioned out of band (it holds the emtl_django
# role password, created via psql). Terraform only reads it so the task
# definition can reference its ARN — it never manages its contents.

data "aws_secretsmanager_secret" "emtl_database_url" {
  name = "${var.app_name}/emtl/database-url"
}

# Django SECRET_KEY: generated once by Terraform, stored only in Secrets Manager.
resource "random_password" "emtl_django_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "emtl_django_secret_key" {
  name                    = "${var.app_name}/emtl/django-secret-key"
  description             = "Django SECRET_KEY for the EMTL Historian Workflow app."
  recovery_window_in_days = 7

  tags = { Name = "${var.app_name}-emtl-django-secret-key" }
}

resource "aws_secretsmanager_secret_version" "emtl_django_secret_key" {
  secret_id     = aws_secretsmanager_secret.emtl_django_secret_key.id
  secret_string = random_password.emtl_django_secret.result
}

# ── CloudWatch log group ──────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "emtl" {
  name              = "/ecs/${var.app_name}-emtl"
  retention_in_days = 30

  tags = { Name = "${var.app_name}-emtl-logs" }
}

# ── Task definition ─────────────────────────────────────────────────────────��─

resource "aws_ecs_task_definition" "emtl" {
  family                   = "${var.app_name}-emtl"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.emtl_task_cpu
  memory                   = var.emtl_task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "${var.app_name}-emtl"
      image     = "${aws_ecr_repository.emtl.repository_url}:${var.emtl_image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      secrets = [
        {
          name      = "DATABASE_URL"
          valueFrom = data.aws_secretsmanager_secret.emtl_database_url.arn
        },
        {
          name      = "DJANGO_SECRET_KEY"
          valueFrom = aws_secretsmanager_secret.emtl_django_secret_key.arn
        }
      ]

      environment = [
        { name = "DJANGO_SETTINGS_MODULE", value = "emtl_site.settings_prod" },
        { name = "DJANGO_DEBUG", value = "0" },
        { name = "DJANGO_ALLOWED_HOSTS", value = "${var.emtl_domain},localhost,127.0.0.1" },
        { name = "DJANGO_PUBLIC_HOST", value = var.emtl_domain },
        { name = "EMTL_CHATBOT_PROVIDER", value = "unconfigured" },
        { name = "EMTL_STAGE_PROVIDER", value = "unconfigured" },
        { name = "EMTL_WORKFLOW_REPOSITORY", value = "unconfigured" },
        { name = "EMTL_ARTIFACT_STORE", value = "local" },
        { name = "EMTL_LOCAL_ARTIFACT_ROOT", value = "/tmp/emtl-artifacts" },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.emtl.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:${var.container_port}/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 90
      }
    }
  ])

  tags = { Name = "${var.app_name}-emtl" }
}

# ── ECS service ─────────────────────────────────────────────────────────────��─

resource "aws_ecs_service" "emtl" {
  name            = "${var.app_name}-emtl"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.emtl.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  force_new_deployment = true

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.emtl.arn
    container_name   = "${var.app_name}-emtl"
    container_port   = var.container_port
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [
    aws_lb_listener_rule.emtl_https,
    aws_iam_role_policy_attachment.ecs_task_execution_managed,
  ]

  tags = { Name = "${var.app_name}-emtl" }
}
