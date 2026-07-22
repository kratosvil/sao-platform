# Modulo 3 de SAGA: poller que revisa PRs en pending_ci y los mergea solo si
# el CI paso (decision_state=auto_execute), o los marca auto_reject si el CI
# fallo o se agoto el timeout. Disparado por EventBridge cada 1 minuto -- se
# eligio polling en vez de webhook para no depender de otra feature de GitHub
# que pueda estar restringida por tier (ya paso con branch protection en el
# repo privado, Modulo 1).
# Modulo 4: el mismo poller tambien revisa pending_merge (PRs de escalate
# esperando un click humano en GitHub) y pending_loop_closure (confirmar en
# Prometheus que la alerta se resolvio antes de generar un guardrail).
data "archive_file" "hitl_poller" {
  type        = "zip"
  source_file = "${path.module}/../lambda-hitl-poller/handler.py"
  output_path = "${path.module}/../lambda-hitl-poller/hitl_poller.zip"
}

resource "aws_iam_role" "hitl_poller" {
  name = "sao-lambda-hitl-poller"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "hitl_poller_basic" {
  role       = aws_iam_role.hitl_poller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "hitl_poller" {
  name = "sao-hitl-poller-policy"
  role = aws_iam_role.hitl_poller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Necesita listar para descubrir propuestas pending_ci sin conocer el
        # token de antemano -- acotado por prefijo, no al bucket completo.
        Sid      = "ListProposals"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}"
        Condition = {
          StringLike = { "s3:prefix" = ["${"proposals/"}*"] }
        }
      },
      {
        Sid      = "ReadWriteProposals"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/proposals/*"
      },
      {
        # Modulo 4: registrar el precedente en el Digital Twin al confirmar
        # el cierre de loop (mismo path que usa lambda-hitl para el mismo fin).
        Sid      = "ReadWriteDigitalTwin"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/sao/digital_twin.json"
      },
      {
        Sid      = "ReadGitOpsToken"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.gitops_manifests_token.arn
      },
      {
        Sid      = "PublishSNS"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alarms.arn
      },
    ]
  })
}

resource "aws_lambda_function" "hitl_poller" {
  function_name    = "sao-lambda-hitl-poller"
  role             = aws_iam_role.hitl_poller.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  timeout          = 60
  filename         = data.archive_file.hitl_poller.output_path
  source_code_hash = data.archive_file.hitl_poller.output_base64sha256

  environment {
    variables = {
      GRAPH_BUCKET                 = aws_s3_bucket.graph_store.bucket
      GRAPH_KEY                    = "sao/digital_twin.json"
      HITL_SNS_TOPIC               = aws_sns_topic.alarms.arn
      GITOPS_TOKEN_SECRET          = aws_secretsmanager_secret.gitops_manifests_token.name
      GITOPS_MANIFESTS_REPO        = "kratosvil/saga-gitops-manifests"
      CI_TIMEOUT_MINUTES           = "15"
      PROMETHEUS_URL               = var.prometheus_url
      LOOP_CLOSURE_TIMEOUT_MINUTES = "10"
    }
  }

  tags = { Name = "sao-lambda-hitl-poller" }
}

# EventBridge -- dispara el poller cada 1 minuto
resource "aws_cloudwatch_event_rule" "hitl_poller_schedule" {
  name                = "sao-hitl-poller-schedule"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "hitl_poller" {
  rule = aws_cloudwatch_event_rule.hitl_poller_schedule.name
  arn  = aws_lambda_function.hitl_poller.arn
}

resource "aws_lambda_permission" "hitl_poller_eventbridge" {
  statement_id  = "AllowEventBridgeHitlPoller"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.hitl_poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.hitl_poller_schedule.arn
}
