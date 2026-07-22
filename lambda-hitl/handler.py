"""
Lambda HITL executor — recibe APROBAR/RECHAZAR del operador via API Gateway.
Lee la propuesta de S3, ejecuta la acción predefinida via boto3 (solo APROBAR),
actualiza el estado en S3 y notifica por SNS.
"""
import json
import os
import uuid
import urllib.request
import urllib.error
import boto3
from datetime import datetime, timezone

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")
GRAPH_KEY = os.getenv("GRAPH_KEY", "sao/digital_twin.json")
SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
GITOPS_TOKEN_SECRET = os.getenv("GITOPS_TOKEN_SECRET", "")
GITOPS_MANIFESTS_REPO = os.getenv("GITOPS_MANIFESTS_REPO", "")
PROPOSALS_PREFIX = "proposals/"

s3 = boto3.client("s3", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)


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


def _compute_embedding(text: str) -> list:
    """Vectoriza texto con Titan Embeddings. Retorna [] si falla."""
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        resp = bedrock.invoke_model(
            modelId="amazon.titan-embed-text-v1",
            body=json.dumps({"inputText": text[:8000]}),
        )
        return json.loads(resp["body"].read())["embedding"]
    except Exception as e:
        print(f"Titan embedding failed: {e}")
        return []


def _register_precedent(proposal: dict, execution_result: str, resolved_at: str) -> None:
    """Escribe el precedente del fix ejecutado en el Digital Twin (S3). No-blocking."""
    if not GRAPH_KEY:
        return
    try:
        obj = s3.get_object(Bucket=GRAPH_BUCKET, Key=GRAPH_KEY)
        twin = json.loads(obj["Body"].read())
    except Exception as e:
        print(f"Could not load Digital Twin for precedent: {e}")
        return

    intent = proposal.get("alarm_name", "unknown")
    action = proposal.get("action", "none")
    nodes = [proposal["node_id"]] if proposal.get("node_id") else []
    embed_text = f"alarm:{intent} action:{action} outcome:Success nodes:{' '.join(nodes)}"

    precedent = {
        "timestamp": resolved_at,
        "agent": "sao-hitl-executor",
        "intent": intent,
        "action": action,
        "outcome": "Success",
        "confidence": 1.0,
        "nodes_affected": nodes,
        "embedding": _compute_embedding(embed_text),
    }

    twin.setdefault("precedents", {}).setdefault("remediations", []).append(precedent)

    try:
        s3.put_object(
            Bucket=GRAPH_BUCKET,
            Key=GRAPH_KEY,
            Body=json.dumps(twin, default=str).encode(),
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
        )
        print(f"Precedent registered: alarm={precedent['intent']} action={precedent['action']}")
    except Exception as e:
        print(f"Could not save Digital Twin with precedent: {e}")


def _github_request(method: str, path: str, token: str, body: dict = None) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"GitHub API {method} {path} -> {e.code}: {detail}")


def _get_gitops_token() -> str:
    resp = secretsmanager.get_secret_value(SecretId=GITOPS_TOKEN_SECRET)
    # .strip(): un \n colado al cargar el secret (paste/echo) rompe el
    # header Authorization silenciosamente -- GitHub devuelve 401 genérico,
    # nada indica que sea un problema de whitespace.
    return resp["SecretString"].strip()


def _argocd_rollback_via_git(params: dict) -> dict:
    """
    Revierte un archivo del repo de manifiestos (saga-gitops-manifests) a
    una revision anterior conocida-buena, via un PR nuevo. El agente NUNCA
    commitea directo a `main` -- esa es la rama que ArgoCD observa. El merge
    del PR depende de decision_state (Modulo 3): auto_execute lo mergea solo
    si el CI pasa (ver _check_pending_ci / lambda-hitl-poller), escalate
    siempre requiere un click humano explicito.
    """
    path = params["path"]
    revert_to = params["revert_to"]
    repo = GITOPS_MANIFESTS_REPO
    token = _get_gitops_token()

    # 1. Contenido del archivo tal como estaba en la revision buena
    old_file = _github_request("GET", f"/repos/{repo}/contents/{path}?ref={revert_to}", token)
    old_content_b64 = old_file["content"]

    # 2. SHA del ultimo commit en main
    main_ref = _github_request("GET", f"/repos/{repo}/git/ref/heads/main", token)
    main_sha = main_ref["object"]["sha"]

    # 3. Rama nueva desde main -- nunca se escribe en main directo
    branch = f"saga-rollback-{uuid.uuid4().hex[:10]}"
    _github_request("POST", f"/repos/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch}",
        "sha": main_sha,
    })

    # 4. SHA actual del archivo en la rama nueva (la API lo exige para poder actualizarlo)
    current_file = _github_request("GET", f"/repos/{repo}/contents/{path}?ref={branch}", token)
    current_sha = current_file["sha"]

    # 5. Commit del revert en la rama nueva, no en main
    _github_request("PUT", f"/repos/{repo}/contents/{path}", token, {
        "message": f"saga: revert {path} to {revert_to[:12]}",
        "content": old_content_b64,
        "sha": current_sha,
        "branch": branch,
    })

    # 6. PR contra main -- el merge depende de decision_state (Modulo 3), nunca se
    # commitea directo a main sea cual sea el estado
    pr = _github_request("POST", f"/repos/{repo}/pulls", token, {
        "title": f"SAGA: revert {path} to {revert_to[:12]}",
        "head": branch,
        "base": "main",
        "body": (
            f"Fix propuesto automaticamente por SAGA.\n\n"
            f"- Archivo: `{path}`\n"
            f"- Revertido a: `{revert_to}`\n\n"
            f"El merge depende del gate de 3 estados (decision_state) -- ver estado.md "
            f"SV-AOP-012 Modulo 3."
        ),
    })

    return {
        "message": f"PR abierto: {pr['html_url']} (rama {branch})",
        "html_url": pr["html_url"],
        "pr_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "branch": branch,
    }


def _execute_action(action: str, params: dict):
    """
    Ejecuta la accion predefinida. Retorna un str para las acciones legacy
    (lambda_update_*/ecs_*/rds_reboot_instance -- quedan a proposito, ver
    Modulo 2: el IAM ya no permite escribir, sirven de evidencia del test
    negativo) o un dict con datos del PR para argocd_rollback_via_git.
    """
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

    if action == "argocd_rollback_via_git":
        return _argocd_rollback_via_git(params)

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
    decision_state = proposal.get("decision_state", "escalate")

    try:
        result = _execute_action(action, action_params)

        # Modulo 3: auto_execute no se da por ejecutado al abrir el PR -- queda
        # "pending_ci", esperando a que lambda-hitl-poller confirme que el CI
        # paso antes de mergear. escalate (o cualquier accion legacy) mantiene
        # el comportamiento de siempre: se marca ejecutado al abrir/correr.
        if action == "argocd_rollback_via_git" and decision_state == "auto_execute" and isinstance(result, dict):
            proposal["status"] = "pending_ci"
            proposal["pr_number"] = result["pr_number"]
            proposal["head_sha"] = result["head_sha"]
            proposal["pr_branch"] = result["branch"]
            proposal["pr_opened_at"] = now
            _save_proposal(token, proposal)
            _notify(
                f"[SAO] PR en cola de auto-merge — {alarm_name}",
                f"decision_state=auto_execute -- el PR se mergea solo si el CI pasa, "
                f"sin intervencion humana. Si el CI falla, se marca auto_reject.\n\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}\n"
                f"PR: {result['html_url']}\n\n"
                f"Propuesta original:\n{proposal.get('proposal_text', '')}",
            )
            print(f"Auto-execute PR opened, pending CI: token={token} pr={result['pr_number']}")
            return _html_response(
                200,
                "PR abierto — en cola de auto-merge",
                f"<strong>PR:</strong> <a href=\"{result['html_url']}\">{result['html_url']}</a><br><br>"
                f"decision_state=auto_execute -- se mergea solo si el CI pasa "
                f"(lambda-hitl-poller revisa cada 1 min), sin click humano.<br>"
                f"<strong>Alarma:</strong> {alarm_name}<br><strong>Recurso:</strong> {node_id}",
            )

        proposal["status"] = "executed"
        proposal["execution_result"] = result
        proposal["resolved_at"] = now
        _save_proposal(token, proposal)
        _register_precedent(proposal, result, now)
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
