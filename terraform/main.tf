# --- Graph Store (Digital Twin) ---
resource "aws_s3_bucket" "graph_store" {
  bucket = var.graph_bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "graph_store" {
  bucket = aws_s3_bucket.graph_store.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_versioning" "graph_store" {
  bucket = aws_s3_bucket.graph_store.id
  versioning_configuration { status = "Enabled" }
}

# --- Lambda Collector ---
resource "aws_lambda_function" "collector" {
  function_name = "sao-lambda-collector"
  role          = aws_iam_role.collector.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  timeout       = 300
  memory_size   = 512
  filename      = "${path.module}/../lambda-collector/collector.zip"

  environment {
    variables = {
      TFSTATE_BUCKET = var.tfstate_bucket_name
      GRAPH_BUCKET   = aws_s3_bucket.graph_store.bucket
      AWS_REGION     = var.aws_region
    }
  }
}

# EventBridge — actualiza dynamic_state cada 5 minutos
resource "aws_cloudwatch_event_rule" "collector_schedule" {
  name                = "sao-collector-schedule"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "collector" {
  rule      = aws_cloudwatch_event_rule.collector_schedule.name
  target_id = "sao-collector"
  arn       = aws_lambda_function.collector.arn
  input     = jsonencode({ source = "scheduled" })
}

resource "aws_lambda_permission" "collector_eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collector_schedule.arn
}

# S3 event — actualiza topology cuando hay nuevo tfstate
resource "aws_s3_bucket_notification" "tfstate_trigger" {
  bucket = var.tfstate_bucket_name
  lambda_function {
    lambda_function_arn = aws_lambda_function.collector.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".tfstate"
  }
}
