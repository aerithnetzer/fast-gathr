# ── Bedrock cost guardrail ────────────────────────────────────────────────────
# A monthly cost budget scoped to Amazon Bedrock, with an email alert when
# forecast/actual spend crosses the threshold. Email subscribers are supported
# by AWS Budgets directly (no SNS topic required).

variable "bedrock_monthly_budget_usd" {
  description = "Monthly USD budget for Amazon Bedrock spend."
  type        = number
  default     = 50
}

variable "budget_alert_email" {
  description = "Email address to notify on Bedrock budget threshold breaches."
  type        = string
  default     = "aerith.netzer@northwestern.edu"
}

resource "aws_budgets_budget" "bedrock" {
  name         = "${var.app_name}-bedrock-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.bedrock_monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  # Warn at 80% of forecasted spend.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # Warn again at 100% of actual spend.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
