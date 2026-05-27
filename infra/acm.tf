# ── ACM Certificate for api.gathrlab.org ─────────────────────────────────────
# Validation is done via DNS. After the first apply, the
# `acm_validation_records` output lists the CNAME(s) that must be created in
# Cloudflare (DNS only / grey cloud — proxying breaks ACM validation).

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${var.app_name}-api-cert" }
}

# Waits for the certificate to become ISSUED before downstream resources
# (the HTTPS listener) try to use it. Will block on the first apply until the
# DNS validation CNAME has been created in Cloudflare.
resource "aws_acm_certificate_validation" "api" {
  certificate_arn = aws_acm_certificate.api.arn
}
