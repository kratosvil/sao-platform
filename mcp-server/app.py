"""
HTTP entry point para ECS Fargate.
Recibe eventos de alarma, carga el Digital Twin, consulta CloudWatch en tiempo
real y llama a Bedrock para obtener una propuesta de fix.
"""
import json
import logging
import boto3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from context_map import GraphStore, GraphQuery
from config import (
    AWS_REGION, BEDROCK_MODEL_ID, BEDROCK_MAX_TOKENS,
    GRAPH_BUCKET, HITL_SNS_TOPIC,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SAO Platform MCP Server", version="0.1.0")
store = GraphStore()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AlarmEvent(BaseModel):
    alarm_name: str
    node_id: str
    resource_type: str
    region: str = AWS_REGION
    account_id: str = ""


class IncidentResponse(BaseModel):
    status: str
    alarm_name: str
    node_id: str
    proposal: str
    risk_level: str
    timestamp: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cloudwatch_context(alarm_name: str, node_id: str, region: str) -> dict:
    """Consulta CW Logs y alarmas en tiempo real para el recurso afectado."""
    cw = boto3.client("cloudwatch", region_name=region)
    logs = boto3.client("logs", region_name=region)
    context = {}

    # Estado actual de la alarma
    try:
        resp = cw.describe_alarms(AlarmNames=[alarm_name])
        alarms = resp.get("MetricAlarms", [])
        context["alarm_state"] = alarms[0] if alarms else {}
    except Exception as e:
        logger.warning("Could not fetch alarm state: %s", e)
        context["alarm_state"] = {}

    # Logs recientes del recurso (asume log group /aws/lambda/<node_id> o similar)
    log_group = f"/aws/lambda/{node_id}"
    try:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(minutes=30)
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=50,
        )
        context["recent_logs"] = [e["message"] for e in resp.get("events", [])]
    except Exception as e:
        logger.warning("Could not fetch logs for %s: %s", log_group, e)
        context["recent_logs"] = []

    return context


def _build_prompt(event: AlarmEvent, graph_context: dict, cw_context: dict) -> str:
    return f"""You are an autonomous AWS infrastructure operations agent.
An alarm has fired. Analyze the full context and propose an exact, safe remediation.

## Alarm
- Name: {event.alarm_name}
- Affected resource: {event.node_id} (type: {event.resource_type})
- Region: {event.region}

## Alarm state (real-time)
{json.dumps(cw_context.get('alarm_state', {}), indent=2, default=str)}

## Recent logs (last 30 min)
{chr(10).join(cw_context.get('recent_logs', ['No logs available'])[:20])}

## Infrastructure context (from Digital Twin)
{json.dumps(graph_context, indent=2, default=str)}

## Instructions
1. Identify the root cause based on alarm state and logs.
2. Check the dependency graph to assess blast radius.
3. Review governance rules — never propose denied actions.
4. Check similar precedents for proven fixes.
5. Propose ONE exact fix with the specific parameters.
6. Assign risk level: LOW (config change), MEDIUM (restart/scale), HIGH (destructive).
7. Format your response as:
   ROOT_CAUSE: <one sentence>
   FIX: <exact boto3 call or action>
   RISK: LOW|MEDIUM|HIGH
   REASON: <why this fix is safe>
"""


def _extract_risk(proposal: str) -> str:
    for line in proposal.splitlines():
        if line.startswith("RISK:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("LOW", "MEDIUM", "HIGH"):
                return val
    return "MEDIUM"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "bucket": GRAPH_BUCKET}


@app.post("/incident", response_model=IncidentResponse)
def handle_incident(event: AlarmEvent):
    logger.info("Incident received: alarm=%s node=%s", event.alarm_name, event.node_id)

    # 1. Cargar Digital Twin
    try:
        twin = store.load_or_empty("SAO-CORE-VPC-PROD-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load Digital Twin: {e}")

    # 2. Construir contexto del grafo
    query = GraphQuery(twin)
    if twin.is_locked(event.node_id):
        raise HTTPException(status_code=409, detail=f"Node {event.node_id} is locked by another agent")
    graph_context = query.context_for_agent(event.alarm_name, event.node_id)

    # 3. Consultar CloudWatch en tiempo real
    cw_context = _get_cloudwatch_context(event.alarm_name, event.node_id, event.region)

    # 4. Llamar a Bedrock
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    prompt = _build_prompt(event, graph_context, cw_context)
    try:
        resp = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": BEDROCK_MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        result = json.loads(resp["body"].read())
        proposal = result["content"][0]["text"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bedrock error: {e}")

    risk = _extract_risk(proposal)
    logger.info("Proposal generated — risk=%s node=%s", risk, event.node_id)

    # 5. Publicar en SNS para HITL (Fase 3 lo reemplaza con Slack)
    if HITL_SNS_TOPIC:
        try:
            sns = boto3.client("sns", region_name=AWS_REGION)
            sns.publish(
                TopicArn=HITL_SNS_TOPIC,
                Subject=f"[SAO] Incident: {event.alarm_name} — Risk: {risk}",
                Message=proposal,
            )
        except Exception as e:
            logger.warning("SNS publish failed: %s", e)

    return IncidentResponse(
        status="proposed",
        alarm_name=event.alarm_name,
        node_id=event.node_id,
        proposal=proposal,
        risk_level=risk,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
    )
