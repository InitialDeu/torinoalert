terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

terraform {
  backend "s3" {
    bucket  = "torinoalert-terraform-state"
    key     = "terraform.tfstate"
    region  = "eu-south-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.region
}

locals {
  name = var.project
}

# -------------------------
# SSM Parameter Store (SecureString) - più economico di Secrets Manager
# -------------------------
resource "aws_ssm_parameter" "telegram_bot_token" {
  name  = "/${local.name}/telegram/bot_token"
  type  = "SecureString"
  value = var.telegram_bot_token
}

resource "aws_ssm_parameter" "telegram_chat_id" {
  name  = "/${local.name}/telegram/chat_id"
  type  = "SecureString"
  value = var.telegram_chat_id
}

# -------------------------
# DynamoDB (dedup) con TTL
# -------------------------
resource "aws_dynamodb_table" "dedup" {
  name         = "${local.name}-dedup"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = {
    Project = local.name
  }
}

# -------------------------
# Lambda package
# -------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/lambda.zip"
}

# -------------------------
# IAM Role for Lambda
# -------------------------
resource "aws_iam_role" "lambda_role" {
  name = "${local.name}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "${local.name}-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CloudWatch Logs
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      # Read SSM parameters (SecureString)
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = [
          aws_ssm_parameter.telegram_bot_token.arn,
          aws_ssm_parameter.telegram_chat_id.arn
        ]
      },
      # DynamoDB dedup
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.dedup.arn
      }
    ]
  })
}



# -------------------------
# Lambda function
# -------------------------
resource "aws_lambda_function" "torino_alert" {
  function_name = "${local.name}-fn"
  role          = aws_iam_role.lambda_role.arn

  runtime = "python3.11"
  handler = "handler.lambda_handler"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 25
  memory_size = 256

  environment {
    variables = {
      PROJECT_NAME     = local.name
      SSM_TOKEN_PARAM  = aws_ssm_parameter.telegram_bot_token.name
      SSM_CHATID_PARAM = aws_ssm_parameter.telegram_chat_id.name
      DDB_TABLE        = aws_dynamodb_table.dedup.name
      TTL_SECONDS      = "86400" # 48h dedup (modifica se vuoi)
    }
  }

  tags = {
    Project = local.name
  }
}

# CloudWatch log group retention (evita costi log)
resource "aws_cloudwatch_log_group" "lambda_lg" {
  name              = "/aws/lambda/${aws_lambda_function.torino_alert.function_name}"
  retention_in_days = 7
}

# -------------------------
# EventBridge schedule
# -------------------------
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name}-schedule"
  schedule_expression = "rate(${var.schedule_rate_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "schedule_target" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "lambda"
  arn       = aws_lambda_function.torino_alert.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.torino_alert.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}