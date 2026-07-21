# ── ECS Task Execution Role ───────────────────────────────────────────────────
# Used by the ECS agent to pull the image from ECR and read secrets.

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.app_name}-ecs-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to read every secret used by the task.
data "aws_iam_policy_document" "secrets_read" {
  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.jwt_secret.arn,
      aws_secretsmanager_secret.bootstrap_admin_password.arn,
      data.aws_secretsmanager_secret.emtl_database_url.arn,
      aws_secretsmanager_secret.emtl_django_secret_key.arn,
      aws_secretsmanager_secret.emtl_bootstrap_admin_password.arn,
    ]
  }
}

resource "aws_iam_policy" "secrets_read" {
  name   = "${var.app_name}-secrets-read"
  policy = data.aws_iam_policy_document.secrets_read.json
}

resource "aws_iam_role_policy_attachment" "ecs_secrets_read" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = aws_iam_policy.secrets_read.arn
}

# ── ECS Task Role ─────────────────────────────────────────────────────────────
# Assumed by the running application container itself.
# Add additional policies here if the app needs to call other AWS services.

resource "aws_iam_role" "ecs_task" {
  name               = "${var.app_name}-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

# Allow the EMTL app (running under the task role) to invoke the Bedrock model
# for stage generation. Current-gen Claude requires a cross-region inference
# profile, so we must permit both the inference-profile ARN and the underlying
# foundation-model ARNs in every region the profile can route to.
data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid    = "InvokeBedrockInferenceProfile"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      # The cross-region inference profile itself.
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
      # Underlying foundation models the "us." profile fans out to.
      "arn:aws:bedrock:us-east-1::foundation-model/*",
      "arn:aws:bedrock:us-east-2::foundation-model/*",
      "arn:aws:bedrock:us-west-2::foundation-model/*",
    ]
  }

  statement {
    sid       = "ListBedrockModels"
    effect    = "Allow"
    actions   = ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "bedrock_invoke" {
  name   = "${var.app_name}-bedrock-invoke"
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_bedrock" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}
