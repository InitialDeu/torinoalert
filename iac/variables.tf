variable "region" {
  type    = string
  default = "eu-south-1"
}

variable "project" {
  type    = string
  default = "torino-alert"
}

variable "schedule_rate_minutes" {
  type        = number
  default     = 2
  description = "Ogni quanti minuti gira la Lambda."
  validation {
    condition     = var.schedule_rate_minutes >= 1 && floor(var.schedule_rate_minutes) == var.schedule_rate_minutes
    error_message = "schedule_rate_minutes deve essere un intero >= 1."
  }
}

variable "dedup_ttl_days" {
  type        = number
  default     = 7
  description = "Giorni dopo cui un evento sparito dalle fonti viene dimenticato (rinnovato finché resta pubblicato)."
}

variable "max_sends_per_run" {
  type        = number
  default     = 10
  description = "Tetto di messaggi Telegram per esecuzione; il resto parte al giro successivo."
}

variable "admin_chat_id" {
  type        = string
  default     = ""
  description = "Chat Telegram per gli avvisi tecnici (fonte giù / ripristinata). Vuoto = disattivato."
}

variable "alert_email" {
  type        = string
  default     = ""
  description = "Email per allarmi CloudWatch (Lambda in errore o ferma). Vuoto = nessun allarme."
}

variable "arpa_zones" {
  type        = list(string)
  default     = ["Piem-L"]
  description = "Zone di allerta ARPA monitorate (Piem-L = Pianura torinese e colline)."
}

variable "ddb_read_capacity" {
  type    = number
  default = 5
}

variable "ddb_write_capacity" {
  type    = number
  default = 5
}

variable "traffic_radius_km" {
  type        = number
  default     = 15
  description = "Raggio attorno a Torino per gli eventi di traffico 5T (chiusure, lavori)."
}

variable "digest_hour" {
  type        = number
  default     = 7
  description = "Ora (di Roma) del riepilogo mattutino sul canale."
  validation {
    condition     = var.digest_hour >= 2 && var.digest_hour <= 23 && floor(var.digest_hour) == var.digest_hour
    error_message = "digest_hour deve essere un intero tra 2 e 23."
  }
}
