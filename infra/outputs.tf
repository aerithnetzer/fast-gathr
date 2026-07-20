output "alb_dns_name" {
  description = "Public DNS name of the Application Load Balancer."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL. Use this as the Docker image base in CI."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Name of the ECS service."
  value       = aws_ecs_service.app.name
}

output "database_url_secret_arn" {
  description = "ARN of the Secrets Manager secret holding DATABASE_URL."
  value       = aws_secretsmanager_secret.database_url.arn
}

output "rds_endpoint" {
  description = "Endpoint of the RDS PostgreSQL instance (host:port)."
  value       = aws_db_instance.main.endpoint
}

output "vpc_id" {
  description = "ID of the created VPC."
  value       = aws_vpc.main.id
}

# ── ACM DNS Validation ────────────────────────────────────────────────────────
# After the first `terraform apply`, create these CNAME records in Cloudflare
# (DNS only / grey cloud) so ACM can validate ownership of the domain.

output "acm_validation_records" {
  description = "CNAME records to create in Cloudflare for ACM DNS validation."
  value = {
    for dvo in aws_acm_certificate.api.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }
}

output "api_domain" {
  description = "Public API domain — point a CNAME from this to the ALB DNS name."
  value       = var.api_domain
}

output "mcp_domain" {
  description = "Public MCP domain — point a CNAME from this to the ALB DNS name (proxied)."
  value       = var.mcp_domain
}

output "mcp_ecr_repository_url" {
  description = "ECR repository URL for the MCP server image."
  value       = aws_ecr_repository.mcp.repository_url
}

output "mcp_ecs_service_name" {
  description = "Name of the MCP ECS service."
  value       = aws_ecs_service.mcp.name
}

output "emtl_domain" {
  description = "Public EMTL Django domain — point a CNAME from this to the ALB DNS name (proxied for Cloudflare Access)."
  value       = var.emtl_domain
}

output "emtl_ecr_repository_url" {
  description = "ECR repository URL for the EMTL Django image."
  value       = aws_ecr_repository.emtl.repository_url
}

output "emtl_ecs_service_name" {
  description = "Name of the EMTL Django ECS service."
  value       = aws_ecs_service.emtl.name
}
