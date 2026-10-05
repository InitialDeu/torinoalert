terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  backend "s3" {
    bucket       = "torinoalert-terraform-state"
    key          = "terraform.tfstate"
    region       = "eu-south-1"
    encrypt      = true
    use_lockfile = true # lock nativo S3: niente apply concorrenti, nessuna tabella DynamoDB
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = var.project
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name          = var.project
  function_name = "${var.project}-fn"
  account_id    = data.aws_caller_identity.current.account_id

  ssm_token_name  = "/${var.project}/telegram/bot_token"
  ssm_chatid_name = "/${var.project}/telegram/chat_id"
  ssm_param_arns = [
    "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_token_name}",
    "arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_chatid_name}",
  ]

  alerts_enabled = var.alert_email != ""
}

# -------------------------
# Secret Telegram in SSM Parameter Store (SecureString)
# Creati a mano con la CLI (vedi README) e referenziati solo per nome:
# il token non passa più da Terraform e non finisce nello state.
# I blocchi `removed` tolgono i vecchi parametri dallo state SENZA cancellarli.
# -------------------------
removed {
  from = aws_ssm_parameter.telegram_bot_token
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ssm_parameter.telegram_chat_id
  lifecycle {
    destroy = false
  }
}

# -------------------------
# DynamoDB (dedup) con TTL
# PROVISIONED: coperto dal free tier permanente (25 RCU/WCU), on-demand no.
# -------------------------
resource "aws_dynamodb_table" "dedup" {
  name           = "${local.name}-dedup"
  billing_mode   = "PROVISIONED"
  read_capacity  = var.ddb_read_capacity
  write_capacity = var.ddb_write_capacity
  hash_key       = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# -------------------------
# Lambda package (generato da scripts/build_lambda.py)
# -------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../build/lambda"
  output_path = "${path.module}/../build/lambda.zip"
}

# -------------------------
# IAM Role for Lambda (minimo privilegio)
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
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.lambda_lg.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = local.ssm_param_arns
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:BatchGetItem", "dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.dedup.arn
      }
    ]
  })
}

# -------------------------
# Lambda function
# -------------------------
resource "aws_cloudwatch_log_group" "lambda_lg" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 7
}

resource "aws_lambda_function" "torino_alert" {
  function_name = local.function_name
  role          = aws_iam_role.lambda_role.arn

  runtime       = "python3.13"
  architectures = ["arm64"] # Graviton: più economico, stesso codice (dipendenze Python puro)
  handler       = "handler.lambda_handler"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 60
  memory_size = 256

  # Una sola esecuzione alla volta: due run sovrapposti invierebbero doppioni.
  # Commentato perché richiede quota di concorrenza riservabile (account nuovi hanno limite 10).
  # reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda_lg.name
  }

  environment {
    # Le variabili vuote vengono omesse: Lambda non le conserva e il plan mostrerebbe sempre una differenza.
    variables = merge(
      {
        SSM_TOKEN_PARAM   = local.ssm_token_name
        SSM_CHATID_PARAM  = local.ssm_chatid_name
        DDB_TABLE         = aws_dynamodb_table.dedup.name
        DEDUP_TTL_DAYS    = tostring(var.dedup_ttl_days)
        MAX_SENDS_PER_RUN = tostring(var.max_sends_per_run)
        ARPA_ZONES        = join(",", var.arpa_zones)
      },
      var.admin_chat_id != "" ? { ADMIN_CHAT_ID = var.admin_chat_id } : {},
    )
  }
}

# -------------------------
# EventBridge schedule
# -------------------------
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name}-schedule"
  schedule_expression = var.schedule_rate_minutes == 1 ? "rate(1 minute)" : "rate(${var.schedule_rate_minutes} minutes)"
}

resource "aws_cloudwatch_event_target" "schedule_target" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "lambda"
  arn       = aws_lambda_function.torino_alert.arn

  # Un run perso non va recuperato: ci pensa il successivo.
  retry_policy {
    maximum_retry_attempts       = 0
    maximum_event_age_in_seconds = 60
  }
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.torino_alert.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}

# -------------------------
# Allarmi via email (opzionali, free tier: 10 allarmi + 1000 email SNS)
# Gli errori delle singole fonti arrivano invece su Telegram (admin_chat_id).
# -------------------------
resource "aws_sns_topic" "alerts" {
  count = local.alerts_enabled ? 1 : 0
  name  = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = local.alerts_enabled ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  count               = local.alerts_enabled ? 1 : 0
  alarm_name          = "${local.name}-lambda-errors"
  alarm_description   = "La Lambda termina in errore (crash, SSM, DynamoDB)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.torino_alert.function_name }
  statistic           = "Sum"
  period              = 600
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_not_running" {
  count               = local.alerts_enabled ? 1 : 0
  alarm_name          = "${local.name}-lambda-not-running"
  alarm_description   = "Nessuna esecuzione da 30 minuti: schedule disabilitato o rotto."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.torino_alert.function_name }
  statistic           = "Sum"
  period              = 1800
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}
