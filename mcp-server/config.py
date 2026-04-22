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
HITL_TIMEOUT_SECONDS = int(os.getenv("HITL_TIMEOUT_SECONDS", "300"))

# MCP
MCP_SERVER_NAME = "sao-platform"
MCP_SERVER_VERSION = "0.1.0"

# Politica de riesgo — define quien aprueba
RISK_POLICY = {
    "LOW": "auto",        # auto-aprobado
    "MEDIUM": "oncall",   # on-call engineer
    "HIGH": "manager",    # manager approval required
}
