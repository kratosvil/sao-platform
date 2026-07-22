import os

# AWS
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
TFSTATE_BUCKET = os.getenv("TFSTATE_BUCKET")          # required — set in env or ECS task def
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")          # S3 bucket para el grafo
GRAPH_KEY = os.getenv("GRAPH_KEY", "sao/digital_twin.json")

# Bedrock
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-5")
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))

# HITL
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
HITL_SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
HITL_TIMEOUT_SECONDS = int(os.getenv("HITL_TIMEOUT_SECONDS", "300"))
HITL_API_URL = os.getenv("HITL_API_URL", "")  # API Gateway URL del executor HITL
HITL_LAMBDA_NAME = os.getenv("HITL_LAMBDA_NAME", "sao-lambda-hitl")  # invoke directo para auto_execute

# MCP
MCP_SERVER_NAME = "sao-platform"
MCP_SERVER_VERSION = "0.1.0"

# Nota: la clasificacion de riesgo real que decide auto_execute/escalate vive en
# app.py::_decide_state (SV-AOP-012 Modulo 3) -- es una regla de codigo sobre los
# params de la accion propuesta, nunca el RISK: que el modelo se autoasigna.
