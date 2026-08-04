variable "region" {
  type    = string
  default = "eu-south-1"
}

variable "project" {
  type    = string
  default = "torino-alert"
}

# Chat ID e token li inserisci in SSM (SecureString) via terraform
variable "telegram_bot_token" {
  type      = string
  sensitive = true
}

variable "telegram_chat_id" {
  type      = string
  sensitive = true
}

# polling
variable "schedule_rate_minutes" {
  type    = number
  default = 2
}