# ── DB Subnet Group ───────────────────────────────────────────────────────────

resource "aws_db_subnet_group" "main" {
  name = "${var.app_name}-db-subnet-group"
  # Include both public and private subnets. Public subnets are required
  # for publicly_accessible = true to yield an internet-reachable
  # endpoint; private subnets are retained because they are currently in
  # use and AWS refuses to remove in-use subnets from the group.
  subnet_ids = concat(aws_subnet.public[*].id, aws_subnet.private[*].id)

  tags = { Name = "${var.app_name}-db-subnet-group" }
}

# ── RDS PostgreSQL Instance ───────────────────────────────────────────────────

resource "aws_db_instance" "main" {
  identifier     = "gathr"
  engine         = "postgres"
  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  allocated_storage     = var.rds_allocated_storage
  max_allocated_storage = 100
  storage_type          = "gp2"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  multi_az            = false
  publicly_accessible = true
  port                = 5432

  backup_retention_period = 7
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = { Name = "gathr" }
}
