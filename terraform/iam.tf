data "aws_caller_identity" "current" {}

# --- Lambda Collector ---

resource "aws_iam_role" "collector" {
  name = "sao-lambda-collector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Logs en CloudWatch (crear log group, escribir eventos)
resource "aws_iam_role_policy_attachment" "collector_basic" {
  role       = aws_iam_role.collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# S3 — leer tfstate del cliente + leer/escribir el Digital Twin
resource "aws_iam_role_policy" "collector_s3" {
  name = "sao-collector-s3"
  role = aws_iam_role.collector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadTfstate"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.tfstate_bucket_name}/*"
      },
      {
        Sid    = "ReadWriteGraph"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/*"
      }
    ]
  })
}

# CloudWatch — leer alarmas activas y metricas
resource "aws_iam_role_policy" "collector_cloudwatch" {
  name = "sao-collector-cloudwatch"
  role = aws_iam_role.collector.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ReadMetrics"
      Effect = "Allow"
      Action = [
        "cloudwatch:DescribeAlarms",
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics"
      ]
      Resource = "*"
    }]
  })
}
