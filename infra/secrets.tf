# ── DATABASE_URL ─────────────────────────────────────────────────────────────

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.app_name}/database-url"
  description             = "DATABASE_URL for the fast-gathr FastAPI service."
  recovery_window_in_days = 7

  tags = { Name = "${var.app_name}-database-url" }
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://${var.db_username}:${var.db_password}@${aws_db_instance.main.endpoint}/${var.db_name}"
}

# ── JWT signing key ──────────────────────────────────────────────────────────
# Generated once by Terraform and stored only in Secrets Manager. Rotating
# this value will invalidate every issued JWT and force users to re-login.

resource "random_password" "jwt_secret" {
  length  = 64
  special = true
}

resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "${var.app_name}/jwt-secret"
  description             = "HS256 signing key for fast-gathr JWT access tokens."
  recovery_window_in_days = 7

  tags = { Name = "${var.app_name}-jwt-secret" }
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id     = aws_secretsmanager_secret.jwt_secret.id
  secret_string = random_password.jwt_secret.result
}

# ── Bootstrap admin password ─────────────────────────────────────────────────
# Read once on first startup to create the initial admin user. After that,
# the env var is harmless — the bootstrap function is a no-op if any admin
# already exists.

resource "aws_secretsmanager_secret" "bootstrap_admin_password" {
  name                    = "${var.app_name}/bootstrap-admin-password"
  description             = "Initial admin password for fast-gathr (used on first startup only)."
  recovery_window_in_days = 7

  tags = { Name = "${var.app_name}-bootstrap-admin-password" }
}

resource "aws_secretsmanager_secret_version" "bootstrap_admin_password" {
  secret_id     = aws_secretsmanager_secret.bootstrap_admin_password.id
  secret_string = var.bootstrap_admin_password
}
