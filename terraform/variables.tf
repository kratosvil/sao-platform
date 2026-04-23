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
  description = "Bucket del tfstate — el Lambda Collector lee de aqui. Formato: <prefix>-tfstate-<account_id>"
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

variable "tfstate_kms_key_arn" {
  type        = string
  default     = null
  description = "ARN de la KMS key usada para cifrar el tfstate bucket. Requerido si el bucket usa SSE-KMS."
}

# --- Networking ---

variable "vpc_cidr" {
  type    = string
  default = "10.1.0.0/16"
}

# --- MCP Server (ECS) ---

variable "mcp_server_port" {
  type    = number
  default = 8080
}

variable "mcp_server_cpu" {
  type    = number
  default = 512
}

variable "mcp_server_memory" {
  type    = number
  default = 1024
}

variable "bedrock_model_id" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-6"
}
