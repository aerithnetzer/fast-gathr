# ── DB Subnet Group ───────────────────────────────────────────────────────────

resource "aws_db_subnet_group" "main" {
  # Renamed from "-db-subnet-group" to force Terraform to create a new
  # group: AWS cannot move an existing instance's ENI to different
  # subnets within the same VPC, so the instance must be recreated in a
  # public-only subnet group.
  #
  # Public-only: a publicly_accessible instance must place its ENI in a
  # subnet whose 0.0.0.0/0 route points at the internet gateway. Private
  # subnets route egress via NAT, which breaks inbound-initiated
  # connections even when the ENI has a public IP.
  name       = "${var.app_name}-db-public"
  subnet_ids = aws_subnet.public[*].id

  tags = { Name = "${var.app_name}-db-public" }
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

  backup_retention_period = 14
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = { Name = "gathr" }

  lifecycle {
    # Recreate the instance whenever the subnet group is replaced, since
    # an in-VPC subnet-group move is not permitted by RDS.
    replace_triggered_by = [aws_db_subnet_group.main.id]
  }
}
