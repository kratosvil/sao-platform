variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "graph_bucket_name" {
  type        = string
  description = "S3 bucket donde se almacena el Digital Twin JSON"
}

variable "tfstate_bucket_name" {
  type        = string
  default     = "kratosvil-tfstate-805778285334"
  description = "Bucket del tfstate — el Lambda Collector lee de aqui"
}

variable "slack_webhook_ssm_param" {
  type        = string
  description = "SSM Parameter Store path con la Slack webhook URL"
  default     = "/sao/slack/webhook_url"
}

variable "operator_email" {
  type        = string
  description = "Email del operador para notificaciones HITL fallback"
}
