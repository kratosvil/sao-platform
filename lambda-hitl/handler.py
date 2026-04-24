"""
Lambda HITL executor — recibe APROBAR/RECHAZAR del operador via API Gateway.
Lee la propuesta de S3, ejecuta la acción predefinida via boto3 (solo APROBAR),
actualiza el estado en S3 y notifica por SNS.
"""
import json
import os
import boto3
from datetime import datetime, timezone

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")
SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
PROPOSALS_PREFIX = "proposals/"

s3 = boto3.client("s3", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)


def _load_proposal(token: str) -> dict:
    key = f"{PROPOSALS_PREFIX}{token}.json"
    obj = s3.get_object(Bucket=GRAPH_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _save_proposal(token: str, data: dict):
    key = f"{PROPOSALS_PREFIX}{token}.json"
    s3.put_object(
        Bucket=GRAPH_BUCKET,
        Key=key,
        Body=json.dumps(data, default=str).encode(),
        ContentType="application/json",
    )


def _notify(subject: str, message: str):
    if SNS_TOPIC:
        try:
            sns_client.publish(TopicArn=SNS_TOPIC, Subject=subject[:100], Message=message)
        except Exception as e:
            print(f"SNS publish failed: {e}")


def _execute_action(action: str, params: dict) -> str:
    """Ejecuta la acción boto3 predefinida. Retorna descripción de lo ejecutado."""
    lm = boto3.client("lambda", region_name=AWS_REGION)
    ecs = boto3.client("ecs", region_name=AWS_REGION)
    rds = boto3.client("rds", region_name=AWS_REGION)

    if action == "lambda_update_timeout":
        fn = params["function_name"]
        timeout = int(params["timeout"])
        lm.update_function_configuration(FunctionName=fn, Timeout=timeout)
        return f"Lambda {fn}: timeout actualizado a {timeout}s"

    if action == "lambda_update_memory":
        fn = params["function_name"]
        memory = int(params["memory_size"])
        lm.update_function_configuration(FunctionName=fn, MemorySize=memory)
        return f"Lambda {fn}: memoria actualizada a {memory}MB"

    if action == "lambda_update_reserved_concurrency":
        fn = params["function_name"]
        concurrency = int(params["reserved_concurrent_executions"])
        lm.put_function_concurrency(FunctionName=fn, ReservedConcurrentExecutions=concurrency)
        return f"Lambda {fn}: concurrencia reservada ajustada a {concurrency}"

    if action == "ecs_restart_service":
        cluster = params["cluster"]
        service = params["service"]
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        return f"ECS {service} (cluster {cluster}): force-redeploy iniciado"

    if action == "ecs_update_desired_count":
        cluster = params["cluster"]
        service = params["service"]
        desired = int(params["desired_count"])
        ecs.update_service(cluster=cluster, service=service, desiredCount=desired)
        return f"ECS {service} (cluster {cluster}): desired count actualizado a {desired}"

    if action == "rds_reboot_instance":
        identifier = params["db_instance_identifier"]
        rds.reboot_db_instance(DBInstanceIdentifier=identifier)
        return f"RDS {identifier}: reboot iniciado"

    if action == "none":
        reason = params.get("reason", "No automated action available")
        return f"Sin accion automatica: {reason}"

    raise ValueError(f"Accion desconocida: {action}")


def _html_response(status_code: int, title: str, body: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": f"""<!DOCTYPE html>
<html><head><title>SAO Platform — {title}</title>
<style>body{{font-family:sans-serif;max-width:600px;margin:60px auto;padding:0 20px}}
h2{{color:{'#1a7f37' if status_code == 200 else '#cf222e'}}}</style></head>
<body><h2>{title}</h2><p>{body}</p>
<hr><small>SAO Platform — Sovereign Agentic Operations</small></body></html>""",
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    token = params.get("token", "").strip()
    raw_path = event.get("rawPath", "")
    # extraer 'approve' o 'reject' del path
    action_type = raw_path.rstrip("/").split("/")[-1]

    print(f"HITL request: action={action_type} token={token}")

    if not token:
        return _html_response(400, "Token requerido", "No se proporcionó un token de propuesta.")

    if action_type not in ("approve", "reject"):
        return _html_response(400, "Ruta inválida", f"Ruta no reconocida: {raw_path}")

    try:
        proposal = _load_proposal(token)
    except s3.exceptions.NoSuchKey:
        return _html_response(404, "Propuesta no encontrada", f"No existe propuesta con token {token}.")
    except Exception as e:
        print(f"Error loading proposal: {e}")
        return _html_response(500, "Error interno", "No se pudo leer la propuesta.")

    if proposal.get("status") != "pending":
        status = proposal.get("status", "desconocido")
        return _html_response(
            409,
            "Ya procesada",
            f"Esta propuesta ya fue <strong>{status}</strong>. No se puede procesar dos veces.",
        )

    now = datetime.now(tz=timezone.utc).isoformat()
    alarm_name = proposal.get("alarm_name", "unknown")
    node_id = proposal.get("node_id", "unknown")
    risk = proposal.get("risk_level", "MEDIUM")

    # --- RECHAZAR ---
    if action_type == "reject":
        proposal["status"] = "rejected"
        proposal["resolved_at"] = now
        _save_proposal(token, proposal)
        _notify(
            f"[SAO] Propuesta RECHAZADA — {alarm_name}",
            f"El operador rechazó la propuesta de fix.\n\n"
            f"Alarma: {alarm_name}\nRecurso: {node_id}\nRiesgo: {risk}\n\n"
            f"Propuesta original:\n{proposal.get('proposal_text', '')}",
        )
        print(f"Proposal rejected: alarm={alarm_name} node={node_id} token={token}")
        return _html_response(
            200,
            "Propuesta rechazada",
            f"La propuesta fue rechazada. No se tomó ninguna acción automatizada.<br><br>"
            f"<strong>Alarma:</strong> {alarm_name}<br><strong>Recurso:</strong> {node_id}",
        )

    # --- APROBAR ---
    action = proposal.get("action", "none")
    action_params = proposal.get("action_params", {})

    try:
        result = _execute_action(action, action_params)
        proposal["status"] = "executed"
        proposal["execution_result"] = result
        proposal["resolved_at"] = now
        _save_proposal(token, proposal)
        _notify(
            f"[SAO] Fix EJECUTADO — {alarm_name}",
            f"El fix fue ejecutado exitosamente.\n\n"
            f"Alarma: {alarm_name}\nRecurso: {node_id}\nRiesgo: {risk}\n"
            f"Accion: {action}\nResultado: {result}\n\n"
            f"Propuesta original:\n{proposal.get('proposal_text', '')}",
        )
        print(f"Action executed: alarm={alarm_name} action={action} result={result} token={token}")
        return _html_response(
            200,
            "Fix ejecutado exitosamente",
            f"<strong>Resultado:</strong> {result}<br><br>"
            f"<strong>Alarma:</strong> {alarm_name}<br>"
            f"<strong>Recurso:</strong> {node_id}<br>"
            f"<strong>Accion:</strong> {action}",
        )
    except Exception as e:
        proposal["status"] = "failed"
        proposal["execution_error"] = str(e)
        proposal["resolved_at"] = now
        _save_proposal(token, proposal)
        _notify(
            f"[SAO] Fix FALLIDO — {alarm_name}",
            f"La ejecucion del fix falló.\n\n"
            f"Alarma: {alarm_name}\nRecurso: {node_id}\n"
            f"Accion: {action}\nError: {e}",
        )
        print(f"Action failed: alarm={alarm_name} action={action} error={e} token={token}")
        return _html_response(
            500,
            "Error en la ejecucion",
            f"El fix no pudo ejecutarse.<br><br>"
            f"<strong>Error:</strong> {e}<br>"
            f"<strong>Accion intentada:</strong> {action}",
        )
