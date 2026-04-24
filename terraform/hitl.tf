# Lambda HITL executor — aprueba/rechaza propuestas del agente
data "archive_file" "hitl" {
  type        = "zip"
  source_file = "${path.module}/../lambda-hitl/handler.py"
  output_path = "${path.module}/../lambda-hitl/hitl.zip"
}

resource "aws_iam_role" "hitl" {
  name = "sao-lambda-hitl"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "hitl_basic" {
  role       = aws_iam_role.hitl.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "hitl" {
  name = "sao-hitl-policy"
  role = aws_iam_role.hitl.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadWriteProposals"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/proposals/*"
      },
      {
        Sid    = "PublishSNS"
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = aws_sns_topic.alarms.arn
      },
      {
        Sid    = "ExecuteLambda"
        Effect = "Allow"
        Action = [
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunctionConfiguration",
          "lambda:PutFunctionConcurrency",
        ]
        Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:*"
      },
      {
        Sid    = "ExecuteECS"
        Effect = "Allow"
        Action = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "*"
      },
      {
        Sid    = "ExecuteRDS"
        Effect = "Allow"
        Action = ["rds:RebootDBInstance"]
        Resource = "arn:aws:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:db:*"
      },
    ]
  })
}

resource "aws_lambda_function" "hitl" {
  function_name    = "sao-lambda-hitl"
  role             = aws_iam_role.hitl.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  timeout          = 30
  filename         = data.archive_file.hitl.output_path
  source_code_hash = data.archive_file.hitl.output_base64sha256

  environment {
    variables = {
      GRAPH_BUCKET   = aws_s3_bucket.graph_store.bucket
      HITL_SNS_TOPIC = aws_sns_topic.alarms.arn
    }
  }

  tags = { Name = "sao-lambda-hitl" }
}

# API Gateway HTTP API — expone los endpoints /hitl/approve y /hitl/reject
resource "aws_apigatewayv2_api" "hitl" {
  name          = "sao-hitl-api"
  protocol_type = "HTTP"
  description   = "SAO HITL — approve/reject incident proposals"
}

resource "aws_apigatewayv2_integration" "hitl" {
  api_id                 = aws_apigatewayv2_api.hitl.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.hitl.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "approve" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/approve"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_route" "reject" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/reject"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_stage" "hitl_default" {
  api_id      = aws_apigatewayv2_api.hitl.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "hitl_apigw" {
  statement_id  = "AllowAPIGatewayHITL"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.hitl.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.hitl.execution_arn}/*/*"
}
