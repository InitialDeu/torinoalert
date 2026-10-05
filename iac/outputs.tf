output "lambda_name" {
  value = aws_lambda_function.torino_alert.function_name
}

output "dynamodb_table" {
  value = aws_dynamodb_table.dedup.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.lambda_lg.name
}

output "ssm_token_param" {
  value = local.ssm_token_name
}

output "ssm_chatid_param" {
  value = local.ssm_chatid_name
}
