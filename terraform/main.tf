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
    }
  }
}


# S3 event — actualiza topology en tiempo real cuando hay nuevo tfstate
# Requiere que el cliente tenga EventBridge notifications habilitado en su bucket:
#   aws s3api put-bucket-notification-configuration \
#     --bucket <tfstate_bucket> \
#     --notification-configuration '{"EventBridgeConfiguration":{}}'
# Es una operacion no destructiva — no toca notificaciones Lambda/SNS/SQS existentes.
resource "aws_cloudwatch_event_rule" "tfstate_s3_event" {
  name        = "sao-tfstate-updated"
  description = "Fires when a .tfstate file is uploaded to the monitored bucket"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [var.tfstate_bucket_name] }
      object = { key = [{ suffix = ".tfstate" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "tfstate_collector" {
  rule      = aws_cloudwatch_event_rule.tfstate_s3_event.name
  target_id = "sao-tfstate-collector"
  arn       = aws_lambda_function.collector.arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
    input_template = "{\"source\": \"s3\", \"bucket\": <bucket>, \"key\": <key>}"
  }
}

resource "aws_lambda_permission" "collector_s3_event" {
  statement_id  = "AllowEventBridgeS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.tfstate_s3_event.arn
}
