# Da applicare UNA volta, in locale, con le tue credenziali AWS:
#   cd iac/bootstrap && terraform init && terraform apply
# Crea il ruolo che GitHub Actions assume via OIDC (nessuna access key salvata su GitHub).
# State locale (gitignored): sono 2-3 risorse che non cambiano.

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "eu-south-1"
}

variable "project" {
  type    = string
  default = "torino-alert"
}

variable "github_repository" {
  type        = string
  default     = "InitialDeu/torinoalert"
  description = "owner/repo autorizzato ad assumere il ruolo (solo branch main)."
}

variable "github_repository_ids" {
  type        = string
  default     = "InitialDeu@64744009/torinoalert@1323391970"
  description = <<-EOT
    owner@id/repo@id come compare nel claim "sub" del nuovo formato GitHub (ID immutabili:
    un repo omonimo ricreato da altri non potrebbe assumere il ruolo).
    Gli ID si leggono da https://api.github.com/repos/<owner>/<repo> (owner.id e id).
  EOT
}

variable "state_bucket" {
  type    = string
  default = "torinoalert-terraform-state"
}

variable "create_oidc_provider" {
  type        = bool
  default     = true
  description = "false se l'account ha già il provider OIDC token.actions.githubusercontent.com."
}

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  p       = var.project
  r       = var.region
  oidc_arn = (
    var.create_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${local.account}:oidc-provider/token.actions.githubusercontent.com"
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "deploy" {
  name = "${local.p}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Formato classico e formato con ID immutabili (usato ora da GitHub per questo repo).
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repository}:ref:refs/heads/main",
            "repo:${var.github_repository_ids}:ref:refs/heads/main",
          ]
        }
      }
    }]
  })
}

# Permessi limitati alle risorse del progetto (prefisso "${var.project}-").
resource "aws_iam_role_policy" "deploy" {
  name = "${local.p}-github-deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "TerraformStateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.state_bucket}"
      },
      {
        Sid      = "TerraformStateObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.state_bucket}/*"
      },
      {
        Sid      = "Lambda"
        Effect   = "Allow"
        Action   = ["lambda:*"]
        Resource = "arn:aws:lambda:${local.r}:${local.account}:function:${local.p}-*"
      },
      {
        Sid    = "LambdaRole"
        Effect = "Allow"
        Action = [
          "iam:GetRole", "iam:CreateRole", "iam:DeleteRole", "iam:UpdateAssumeRolePolicy",
          "iam:TagRole", "iam:UntagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
        ]
        Resource = "arn:aws:iam::${local.account}:role/${local.p}-lambda-role"
      },
      {
        Sid       = "PassLambdaRole"
        Effect    = "Allow"
        Action    = ["iam:PassRole"]
        Resource  = "arn:aws:iam::${local.account}:role/${local.p}-lambda-role"
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = "arn:aws:dynamodb:${local.r}:${local.account}:table/${local.p}-*"
      },
      {
        Sid    = "Logs"
        Effect = "Allow"
        Action = ["logs:*"]
        Resource = [
          "arn:aws:logs:${local.r}:${local.account}:log-group:/aws/lambda/${local.p}-*",
          "arn:aws:logs:${local.r}:${local.account}:log-group:/aws/lambda/${local.p}-*:*",
        ]
      },
      {
        Sid      = "LogsDescribe"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "*"
      },
      {
        Sid      = "EventBridge"
        Effect   = "Allow"
        Action   = ["events:*"]
        Resource = "arn:aws:events:${local.r}:${local.account}:rule/${local.p}-*"
      },
      {
        Sid      = "Sns"
        Effect   = "Allow"
        Action   = ["sns:*"]
        Resource = "arn:aws:sns:${local.r}:${local.account}:${local.p}-*"
      },
      {
        Sid      = "Alarms"
        Effect   = "Allow"
        Action   = ["cloudwatch:*"]
        Resource = "arn:aws:cloudwatch:${local.r}:${local.account}:alarm:${local.p}-*"
      },
      {
        Sid      = "AlarmsDescribe"
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarms"]
        Resource = "*"
      },
    ]
  })
}

output "deploy_role_arn" {
  description = "Da impostare come variabile di repository GitHub AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.deploy.arn
}
