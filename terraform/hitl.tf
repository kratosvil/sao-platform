# Modulo 10 (2026-08-13): token compartido para las rutas de consola
# (/hitl/pending, /hitl/review/*) -- generado por Terraform, nunca pasa por
# el chat/Claude. Recuperar con:
#   terraform output -raw hitl_console_token
resource "random_password" "hitl_console_token" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "hitl_console_token" {
  name  = "/sao/hitl/console-token"
  type  = "SecureString"
  value = random_password.hitl_console_token.result
}

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
        Sid      = "ReadWriteProposals"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/proposals/*"
      },
      {
        # Modulo 10: /hitl/pending necesita listar objetos, no solo leer uno
        # puntual por su key -- gap real encontrado al probar en vivo (el
        # HITL nunca antes necesitaba ListBucket, solo Get/Put por token
        # conocido). Acotado al prefijo proposals/, no todo el bucket.
        Sid      = "ListProposals"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}"
        Condition = {
          StringLike = {
            "s3:prefix" = ["proposals/*"]
          }
        }
      },
      {
        Sid      = "ReadWriteDigitalTwin"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/sao/digital_twin.json"
      },
      {
        Sid      = "BedrockEmbeddings"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v1"
      },
      {
        Sid      = "PublishSNS"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alarms.arn
      },
      {
        # Módulo 10: verifica el bearer token de las rutas de consola contra
        # este parámetro -- solo lectura, el valor lo genera Terraform.
        Sid      = "ReadConsoleToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.hitl_console_token.arn
      },
      # Módulo 2 (SAGA): se eliminaron los Sid ExecuteLambda/ExecuteECS/ExecuteRDS
      # (lambda:UpdateFunctionConfiguration, ecs:UpdateService, rds:RebootDBInstance,
      # etc.) -- eran 100% escritura directa AWS, sin lectura legítima asociada
      # (handler.py no hace ningún describe/get con esos clientes). El HITL ya
      # no puede ejecutar remediación directa; la única vía de escritura es el
      # PR de GitHub de argocd_rollback_via_git (Módulo 1). Las funciones legacy
      # (_execute_action: lambda_update_*, ecs_restart_service, rds_reboot_instance)
      # quedan en el código a propósito -- fallan con AccessDenied, es la evidencia
      # del test negativo de este módulo.
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
      GRAPH_BUCKET          = aws_s3_bucket.graph_store.bucket
      GRAPH_KEY             = "sao/digital_twin.json"
      HITL_SNS_TOPIC        = aws_sns_topic.alarms.arn
      GITOPS_TOKEN_SECRET   = aws_secretsmanager_secret.gitops_manifests_token.name
      GITOPS_MANIFESTS_REPO = "kratosvil/saga-gitops-manifests"
      CONSOLE_TOKEN_PARAM   = aws_ssm_parameter.hitl_console_token.name
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

# Modulo 10: rutas de la consola -- listar pendientes, ver/ajustar una
# propuesta. Todas pasan por _check_console_auth en el handler (bearer
# token), a diferencia de approve/reject que son de un solo uso.
resource "aws_apigatewayv2_route" "pending" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/pending"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_route" "review_get" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/review/{token}"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_route" "review_post" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "POST /hitl/review/{token}"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

# Modulo 10b (2026-08-13): login por cookie -- permite navegar la consola
# desde cualquier PC/navegador sin Bearer header ni proxy local (necesario
# porque el demo se graba desde otra maquina). Publicas a proposito: son el
# mecanismo para conseguir la sesion.
resource "aws_apigatewayv2_route" "login_get" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/login"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_route" "login_post" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "POST /hitl/login"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

resource "aws_apigatewayv2_route" "logout" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/logout"
  target    = "integrations/${aws_apigatewayv2_integration.hitl.id}"
}

# Modulo 10c (2026-08-13): historial de propuestas ya resueltas/rechazadas,
# con costo -- /hitl/pending pierde esa info apenas se aprueba/rechaza algo.
resource "aws_apigatewayv2_route" "history" {
  api_id    = aws_apigatewayv2_api.hitl.id
  route_key = "GET /hitl/history"
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
