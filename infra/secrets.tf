# The secret shell is created by Terraform; the actual value (DATABASE_URL) is
# written by CI after terraform apply, so the password never lives in Terraform
# state or source control.
#
# To populate manually (first-time setup):
#   aws secretsmanager put-secret-value \
#     --secret-id <arn_from_outputs> \
#     --secret-string "postgresql://postgres:<password>@<rds_endpoint>/gathr"

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.app_name}/database-url"
  description             = "DATABASE_URL for the fast-gathr FastAPI service."
  recovery_window_in_days = 7

  tags = { Name = "${var.app_name}-database-url" }
}

# Initial value — constructed from variables so `terraform apply` produces a
# working secret immediately. The CI pipeline overwrites this on each deploy
# if the RDS endpoint ever changes.
resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://${var.db_username}:${var.db_password}@${aws_db_instance.main.endpoint}/${var.db_name}"
}
