variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Short name used to prefix all resource names."
  type        = string
  default     = "fast-gathr"
}

variable "environment" {
  description = "Deployment environment label (e.g. prod, staging)."
  type        = string
  default     = "prod"
}

# ── VPC ─────────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR block for the new VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of AZs to use (must be ≥ 2 for the ALB)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for public subnets (one per AZ)."
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "CIDR blocks for private subnets (one per AZ)."
  type        = list(string)
  default     = ["10.0.10.0/24", "10.0.11.0/24"]
}

# ── ECS / App ────────────────────────────────────────────────────────────────

variable "container_port" {
  description = "Port the FastAPI container listens on."
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "Fargate task CPU units (256 = 0.25 vCPU)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Desired number of running ECS tasks."
  type        = number
  default     = 1
}

variable "image_tag" {
  description = "Docker image tag to deploy. Set by CI to the commit SHA."
  type        = string
  default     = "latest"
}

# ── RDS ──────────────────────────────────────────────────────────────────────

variable "rds_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "rds_allocated_storage" {
  description = "Initial allocated storage in GiB."
  type        = number
  default     = 20
}

variable "rds_engine_version" {
  description = "PostgreSQL engine version."
  type        = string
  default     = "16"
}

variable "db_name" {
  description = "Name of the database to create inside the RDS instance."
  type        = string
  default     = "gathr"
}

variable "db_username" {
  description = "Master username for the RDS instance."
  type        = string
  default     = "postgres"
}

variable "db_password" {
  description = "Master password for the RDS instance. Stored in Secrets Manager — never committed."
  type        = string
  sensitive   = true
}
