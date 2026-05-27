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
