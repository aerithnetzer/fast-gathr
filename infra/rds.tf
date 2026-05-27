# Read the existing Aurora Serverless cluster — does NOT create or modify it.
data "aws_rds_cluster" "hunter_gathrer" {
  cluster_identifier = var.rds_cluster_identifier
}

# Find the security group(s) attached to the RDS cluster so we can add an
# ingress rule for the ECS task SG.  Aurora clusters can have multiple SGs;
# we use the first one. If your cluster has a dedicated SG with a known name
# or tag, replace the filter below with a name/tag filter for precision.
data "aws_security_group" "rds" {
  id = tolist(data.aws_rds_cluster.hunter_gathrer.vpc_security_group_ids)[0]
}
