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

# Fix de contexto real del nucleo (2026-08-12): historial git real del manifiesto
# GitOps para darle a Bedrock el "ultimo tag bueno conocido" -- sin esto devolvia
# ACTION: none por falta de contexto. Solo el path -- mcp_server corre zero-egress
# y no toca GitHub/Secrets Manager el mismo, le pide el historial al Lambda HITL
# (ver app.py::_get_manifest_tag_history).
GITOPS_MANIFEST_PATH = os.getenv("GITOPS_MANIFEST_PATH", "overlays/dev/kustomization.yaml")

# Alarmas para las que este historial es relevante -- sin este filtro, CUALQUIER
# incidente (aunque no tenga nada que ver con este path) recibia el mismo
# historial y Bedrock proponia el mismo revert para alarmas no relacionadas
# (hallazgo real 2026-08-12: AlertmanagerFailedToSendAlerts/
# AlertmanagerClusterFailedToSendAlerts dispararon el mismo fix). No rompio nada
# porque el revert era idempotente y seguro, pero es el sintoma exacto del gap
# de dedup/rate-limiting ya anotado en el backlog -- el arreglo estructural real
# (no proponer una accion si ya hay una equivalente en curso) queda para el
# Modulo 12 (Cost & Policy Gate). Esto es solo el filtro rapido: no evita
# llamadas duplicadas a Bedrock, evita que una alarma no relacionada reciba
# contexto que no le corresponde.
GITOPS_RELEVANT_ALARMS = {a.strip() for a in os.getenv("GITOPS_RELEVANT_ALARMS", "SagaPodCrashLooping").split(",") if a.strip()}

# Nota: la clasificacion de riesgo real que decide auto_execute/escalate vive en
# app.py::_decide_state (SV-AOP-012 Modulo 3) -- es una regla de codigo sobre los
# params de la accion propuesta, nunca el RISK: que el modelo se autoasigna.
