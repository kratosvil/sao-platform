output "graph_bucket" {
  value       = aws_s3_bucket.graph_store.bucket
  description = "S3 bucket del Digital Twin Context Map"
}

output "collector_lambda_arn" {
  value       = aws_lambda_function.collector.arn
  description = "ARN del Lambda Collector"
}
