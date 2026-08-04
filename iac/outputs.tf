output "lambda_name" {
  value = aws_lambda_function.torino_alert.function_name
}

output "dynamodb_table" {
  value = aws_dynamodb_table.dedup.name
}

output "ssm_token_param" {
  value = aws_ssm_parameter.telegram_bot_token.name
}

output "ssm_chatid_param" {
  value = aws_ssm_parameter.telegram_chat_id.name
}