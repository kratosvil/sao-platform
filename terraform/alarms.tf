# Lambda dispatcher — transforma CW Alarm EventBridge event → POST /incident
data "archive_file" "dispatcher" {
  type        = "zip"
  source_file = "${path.module}/../lambda-dispatcher/dispatcher.py"
  output_path = "${path.module}/../lambda-dispatcher/dispatcher.zip"
}

resource "aws_iam_role" "dispatcher" {
  name = "sao-alarm-dispatcher"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dispatcher_basic" {
  role       = aws_iam_role.dispatcher.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "dispatcher" {
  function_name    = "sao-alarm-dispatcher"
  role             = aws_iam_role.dispatcher.arn
  filename         = data.archive_file.dispatcher.output_path
  source_code_hash = data.archive_file.dispatcher.output_base64sha256
  runtime          = "python3.12"
  handler          = "dispatcher.handler"
  timeout          = 30

  environment {
    variables = {
      MCP_SERVER_URL = "http://${module.ecs_fargate.alb_dns_name}"
    }
  }

  tags = { Name = "sao-alarm-dispatcher" }
}

resource "aws_lambda_permission" "dispatcher_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatcher.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cw_alarm.arn
}

# EventBridge rule — captura alarmas que entran en estado ALARM
resource "aws_cloudwatch_event_rule" "cw_alarm" {
  name        = "sao-cw-alarm-trigger"
  description = "Dispara SAO incident cuando una alarma CW entra en ALARM"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      state = { value = ["ALARM"] }
    }
  })

  tags = { Name = "sao-cw-alarm-trigger" }
}

resource "aws_cloudwatch_event_target" "dispatcher" {
  rule      = aws_cloudwatch_event_rule.cw_alarm.name
  target_id = "sao-alarm-dispatcher"
  arn       = aws_lambda_function.dispatcher.arn
}

# Alarma de prueba — errores en Lambda Collector (threshold=1 para demo)
resource "aws_cloudwatch_metric_alarm" "collector_errors" {
  alarm_name          = "sao-collector-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "SAO: Lambda Collector ha reportado errores"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.collector.function_name
  }

  tags = { Name = "sao-collector-errors" }
}
