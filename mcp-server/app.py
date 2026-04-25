"""
HTTP entry point para ECS Fargate.
Recibe eventos de alarma, carga el Digital Twin, consulta CloudWatch en tiempo
real y llama a Bedrock para obtener una propuesta de fix.
"""
import json
import logging
import uuid
import boto3
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from context_map import GraphStore, GraphQuery
from config import (
    AWS_REGION, BEDROCK_MODEL_ID, BEDROCK_MAX_TOKENS,
    GRAPH_BUCKET, HITL_SNS_TOPIC, HITL_API_URL,
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
        start = end - timedelta(minutes=5)
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=int(start.timestamp() * 1000),
            endTime=int(end.timestamp() * 1000),
            limit=10,
        )
        context["recent_logs"] = [e["message"][:200] for e in resp.get("events", [])]
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

## Recent logs (last 5 min)
{chr(10).join(cw_context.get('recent_logs', ['No logs available']))}

## Infrastructure context (from Digital Twin)
{json.dumps(graph_context, indent=2, default=str)}

## Instructions
1. Identify the root cause based on alarm state and logs.
2. Check the dependency graph to assess blast radius.
3. Review governance rules — never propose denied actions.
4. Check similar precedents for proven fixes.
5. Propose ONE exact fix with the specific parameters.
6. Assign risk level: LOW (config change), MEDIUM (restart/scale), HIGH (destructive).
7. Format your response EXACTLY as (no extra text before or after these lines):
   ROOT_CAUSE: <one sentence>
   FIX: <description of the fix in plain English>
   RISK: LOW|MEDIUM|HIGH
   REASON: <why this fix is safe>
   ACTION: <action_name> [param1=value1 param2=value2 ...]

Available action names for the ACTION line:
- lambda_update_timeout function_name=<name> timeout=<seconds_int>
- lambda_update_memory function_name=<name> memory_size=<mb_int>
- lambda_update_reserved_concurrency function_name=<name> reserved_concurrent_executions=<n_int>
- ecs_restart_service cluster=<cluster_name> service=<service_name>
- ecs_update_desired_count cluster=<cluster_name> service=<service_name> desired_count=<n_int>
- rds_reboot_instance db_instance_identifier=<id>
- none reason=<brief_explanation_no_spaces_use_underscores>
"""


def _extract_risk(proposal: str) -> str:
    for line in proposal.splitlines():
        if line.startswith("RISK:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("LOW", "MEDIUM", "HIGH"):
                return val
    return "MEDIUM"


def _parse_action(proposal: str) -> tuple:
    """Extrae action name y params del ACTION: line. Retorna (action, params_dict)."""
    for line in proposal.splitlines():
        if line.startswith("ACTION:"):
            parts = line.split(":", 1)[1].strip().split()
            if not parts:
                return "none", {}
            action = parts[0]
            params = {}
            for part in parts[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
            return action, params
    return "none", {"reason": "No_automated_action_specified"}


def _compute_embedding(text: str) -> list[float]:
    """Llama a Titan Embeddings para vectorizar texto. Retorna [] si falla."""
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        resp = bedrock.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": text[:8000]}),
        )
        return json.loads(resp["body"].read())["embedding"]
    except Exception as e:
        logger.warning("Titan embedding failed: %s", e)
        return []


def _save_proposal(token: str, data: dict):
    """Guarda la propuesta en S3 bajo proposals/{token}.json."""
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(
        Bucket=GRAPH_BUCKET,
        Key=f"proposals/{token}.json",
        Body=json.dumps(data, default=str).encode(),
        ContentType="application/json",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "bucket": GRAPH_BUCKET}


@app.get("/debug/context/{node_id}")
def debug_context(node_id: str):
    """Devuelve el contexto del Digital Twin para un nodo — sin llamar a Bedrock."""
    try:
        twin = store.load_or_empty("SAO-CORE-VPC-PROD-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Digital Twin error: {e}")
    query = GraphQuery(twin)
    return {
        "node_id": node_id,
        "twin_id": twin.digital_twin_id,
        "last_updated": str(twin.dynamic_state.last_updated),
        "total_nodes": len(twin.topology.nodes),
        "total_edges": len(twin.topology.edges),
        "context": query.context_for_agent("debug", node_id),
    }


@app.post("/debug/prompt")
def debug_prompt(event: AlarmEvent):
    """Devuelve el prompt completo que se enviaría a Bedrock — sin invocarlo."""
    try:
        twin = store.load_or_empty("SAO-CORE-VPC-PROD-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Digital Twin error: {e}")
    query = GraphQuery(twin)
    query_text = f"alarm:{event.alarm_name} node:{event.node_id} type:{event.resource_type}"
    query_embedding = _compute_embedding(query_text)
    graph_context = query.context_for_agent(event.alarm_name, event.node_id, query_embedding)
    cw_context = _get_cloudwatch_context(event.alarm_name, event.node_id, event.region)
    prompt = _build_prompt(event, graph_context, cw_context)
    return {
        "model": BEDROCK_MODEL_ID,
        "max_tokens": BEDROCK_MAX_TOKENS,
        "prompt_chars": len(prompt),
        "prompt_tokens_estimate": len(prompt) // 4,
        "rag_mode": "semantic" if query_embedding else "fallback",
        "graph_context": graph_context,
        "cloudwatch_context": cw_context,
        "full_prompt": prompt,
    }


@app.post("/incident", response_model=IncidentResponse)
def handle_incident(event: AlarmEvent):
    logger.info("Incident received: alarm=%s node=%s", event.alarm_name, event.node_id)

    # 1. Cargar Digital Twin
    try:
        twin = store.load_or_empty("SAO-CORE-VPC-PROD-001")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Digital Twin error: {e}")

    # 2. Construir contexto del grafo
    query = GraphQuery(twin)
    if twin.is_locked(event.node_id):
        raise HTTPException(status_code=409, detail=f"Node {event.node_id} is locked by another agent")
    query_text = f"alarm:{event.alarm_name} node:{event.node_id} type:{event.resource_type}"
    query_embedding = _compute_embedding(query_text)
    graph_context = query.context_for_agent(event.alarm_name, event.node_id, query_embedding)

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
        raise HTTPException(status_code=500, detail=f"Bedrock error: {e}")

    risk = _extract_risk(proposal)
    action, action_params = _parse_action(proposal)
    logger.info("Proposal generated — risk=%s action=%s node=%s", risk, action, event.node_id)

    # 5. Guardar propuesta en S3 con token unico para HITL
    token = str(uuid.uuid4())
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    try:
        _save_proposal(token, {
            "token": token,
            "alarm_name": event.alarm_name,
            "node_id": event.node_id,
            "resource_type": event.resource_type,
            "proposal_text": proposal,
            "action": action,
            "action_params": action_params,
            "risk_level": risk,
            "status": "pending",
            "created_at": now_ts,
        })
        logger.info("Proposal saved — token=%s action=%s", token, action)
    except Exception as e:
        logger.warning("Could not save proposal to S3: %s", e)
        token = ""

    # 6. Publicar en SNS con links HITL
    if HITL_SNS_TOPIC:
        try:
            approve_url = f"{HITL_API_URL}/hitl/approve?token={token}" if token and HITL_API_URL else "N/A"
            reject_url = f"{HITL_API_URL}/hitl/reject?token={token}" if token and HITL_API_URL else "N/A"
            hitl_block = (
                f"\n\n---\nACCION DETECTADA: {action}\n"
                f"PARAMETROS: {action_params}\n\n"
                f"Para aprobar y ejecutar el fix automaticamente:\n  APROBAR: {approve_url}\n\n"
                f"Para rechazar sin tomar ninguna accion:\n  RECHAZAR: {reject_url}\n\n"
                f"Token: {token}"
            ) if token else "\n\n[HITL no disponible — propuesta no guardada en S3]"

            sns = boto3.client("sns", region_name=AWS_REGION)
            sns.publish(
                TopicArn=HITL_SNS_TOPIC,
                Subject=f"[SAO] Incidente: {event.alarm_name} — Riesgo: {risk}",
                Message=proposal + hitl_block,
            )
        except Exception as e:
            logger.warning("SNS publish failed: %s", e)

    return IncidentResponse(
        status="proposed",
        alarm_name=event.alarm_name,
        node_id=event.node_id,
        proposal=proposal,
        risk_level=risk,
        timestamp=now_ts,
    )
