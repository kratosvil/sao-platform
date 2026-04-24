# SNS topic — punto de entrada para alarmas CloudWatch
resource "aws_sns_topic" "alarms" {
  name              = "sao-platform-alarms"
  kms_master_key_id = "alias/aws/sns"

  tags = { Name = "sao-platform-alarms" }
}

# Suscripcion email al SNS topic — notificacion HITL
resource "aws_sns_topic_subscription" "operator_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.operator_email
}

module "ecs_fargate" {
  source = "github.com/kratosvil/tf-modules-forge//modules/ecs-fargate?ref=main"

  project_name       = "sao-platform"
  vpc_id             = module.networking.vpc_id
  subnet_public_ids  = module.networking.subnet_public_ids
  subnet_private_ids = module.networking.subnet_private_ids
  sg_alb_id          = module.networking.sg_alb_id
  sg_app_id          = module.networking.sg_app_id

  container_image  = "${module.ecr.repository_url}:latest"
  container_port   = var.mcp_server_port
  container_cpu    = var.mcp_server_cpu
  container_memory = var.mcp_server_memory

  container_environment = [
    { name = "GRAPH_BUCKET",      value = aws_s3_bucket.graph_store.bucket },
    { name = "GRAPH_KEY",         value = "sao/digital_twin.json" },
    { name = "BEDROCK_MODEL_ID",  value = var.bedrock_model_id },
    { name = "HITL_SNS_TOPIC",    value = aws_sns_topic.alarms.arn },
    { name = "HITL_API_URL",      value = aws_apigatewayv2_api.hitl.api_endpoint },
    { name = "SLACK_WEBHOOK_SSM", value = var.slack_webhook_ssm_param },
  ]

  depends_on = [module.bedrock_privatelink, aws_vpc_endpoint.s3]
}

# IAM — permisos del ECS Task role (lo crea el modulo, lo extendemos aqui)
resource "aws_iam_role_policy" "mcp_server" {
  name = "sao-mcp-server-policy"
  role = split("/", module.ecs_fargate.task_role_arn)[1]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        ]
      },
      {
        Sid    = "BedrockMarketplace"
        Effect = "Allow"
        Action = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
        Resource = "*"
      },
      {
        Sid    = "ReadGraph"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}/*"
      },
      {
        Sid      = "ListGraph"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.graph_bucket_name}"
      },
      {
        Sid    = "CloudWatch"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:GetMetricData",
          "logs:FilterLogEvents",
          "logs:GetLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = "*"
      },
      {
        Sid    = "ExecuteLambda"
        Effect = "Allow"
        Action = [
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunctionConfiguration",
          "lambda:InvokeFunction",
        ]
        Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:*"
      },
      {
        Sid    = "ExecuteECS"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMReadSlack"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/sao/*"
      },
      {
        Sid    = "PublishSNS"
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = aws_sns_topic.alarms.arn
      },
    ]
  })
}
