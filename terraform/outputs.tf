output "graph_bucket" {
  value       = aws_s3_bucket.graph_store.bucket
  description = "S3 bucket del Digital Twin Context Map"
}

output "collector_lambda_arn" {
  value       = aws_lambda_function.collector.arn
  description = "ARN del Lambda Collector"
}

output "ecr_repository_url" {
  value       = module.ecr.repository_url
  description = "ECR URL para push de la imagen del MCP Server"
}

output "mcp_server_url" {
  value       = "http://${module.ecs_fargate.alb_dns_name}"
  description = "URL del MCP Server (ALB)"
}

output "vpc_id" {
  value       = module.networking.vpc_id
  description = "VPC ID de la plataforma SAO"
}
