# agent — the cloud enforcement layer (DESIGN.md, Layer 1).
#
# A dedicated IAM role for the agent pipeline: it can read observability and operate ONE staging
# Kubernetes namespace (EKS Edit, scoped), and is explicitly DENIED all prod RDS/secret mutation and
# EKS control-plane mutation. DB/EKS access is via kube RBAC (the access entry below), NOT IAM creds —
# that's why the allow policy is thin.
#
# Everything site-specific is a variable with a placeholder default. Supply your own values (account
# id, region, cluster, RDS instance names, principals, staging namespace) via terraform.tfvars or
# -var flags. The explicit Deny on prod resources is what makes this safe regardless of any allow.

variable "aws_account_id" {
  description = "AWS account id the role lives in"
  type        = string
  default     = "000000000000"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "eks_cluster_name" {
  description = "EKS cluster the agent operates in"
  type        = string
  default     = "my-cluster"
}

variable "staging_namespace" {
  description = "The single staging namespace the agent's EKS access is scoped to"
  type        = string
  default     = "staging"
}

variable "agent_role_name" {
  description = "Name of the agent IAM role"
  type        = string
  default     = "agent"
}

variable "trusted_principals" {
  description = "IAM principal ARNs allowed to assume the agent role (e.g. the dev-VM user)"
  type        = list(string)
  default     = ["arn:aws:iam::000000000000:user/CHANGEME"]
}

variable "prod_rds_instances" {
  description = "Prod RDS DB instance identifiers the agent must NEVER mutate"
  type        = list(string)
  default     = ["app"]
}

variable "prod_secrets_prefix" {
  description = "Secrets Manager name prefix the agent must NEVER read/write"
  type        = string
  default     = "agent/"
}

locals {
  prod_rds_arns = [
    for id in var.prod_rds_instances :
    "arn:aws:rds:${var.aws_region}:${var.aws_account_id}:db:${id}"
  ]
  prod_secrets_arn = "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${var.prod_secrets_prefix}*"
}

# Trust: the listed principals (e.g. the dev-VM IAM user) may assume this role.
resource "aws_iam_role" "agent" {
  name        = var.agent_role_name
  description = "Agent pipeline: read observability, staging-only EKS, NEVER write prod"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.trusted_principals }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "allow" {
  name = "${var.agent_role_name}-allow"
  role = aws_iam_role.agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EksDescribe", Effect = "Allow",
      Action = ["eks:DescribeCluster", "eks:ListClusters", "eks:DescribeAccessEntry"], Resource = "*" },
      { Sid = "ReadOnlyObservability", Effect = "Allow",
        Action = ["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogGroups",
          "logs:DescribeLogStreams", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics"], Resource = "*" },
    ]
  })
}

# The hard prod barrier — explicit Deny beats any allow.
resource "aws_iam_role_policy" "deny_prod" {
  name = "${var.agent_role_name}-deny-prod"
  role = aws_iam_role.agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "DenyProdRdsMutation", Effect = "Deny",
        Action = ["rds-db:connect", "rds:ModifyDBInstance", "rds:DeleteDBInstance",
      "rds:RebootDBInstance", "rds:StopDBInstance"], Resource = local.prod_rds_arns },
      { Sid = "DenyProdSecrets", Effect = "Deny",
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecret", "secretsmanager:DeleteSecret"],
      Resource = local.prod_secrets_arn },
      { Sid = "DenyEksMutation", Effect = "Deny",
        Action = ["eks:UpdateClusterConfig", "eks:UpdateNodegroupConfig", "eks:DeleteCluster",
          "eks:DeleteNodegroup", "eks:CreateAccessEntry", "eks:DeleteAccessEntry",
      "eks:AssociateAccessPolicy"], Resource = "*" },
    ]
  })
}

# EKS access entry: maps the role into the cluster, Edit policy SCOPED to the staging namespace only.
# This is the operative staging-write / prod-no-access boundary for the pod-based tools.
resource "aws_eks_access_entry" "agent" {
  cluster_name  = var.eks_cluster_name
  principal_arn = aws_iam_role.agent.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "agent_staging" {
  cluster_name  = var.eks_cluster_name
  principal_arn = aws_iam_role.agent.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
  access_scope {
    type       = "namespace"
    namespaces = [var.staging_namespace]
  }
}
