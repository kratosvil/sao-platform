"""
Lambda HITL executor — recibe APROBAR/RECHAZAR del operador via API Gateway.
Lee la propuesta de S3, ejecuta la acción predefinida via boto3 (solo APROBAR),
actualiza el estado en S3 y notifica por SNS.

Modulo 10 (2026-08-13): agrega una consola minima sobre el mismo Lambda --
/hitl/pending (listar), /hitl/review/{token} (ver + ajustar antes de
aprobar). Reemplaza la dependencia de "buscar el link en el email" y permite
que el humano corrija un parametro (ej. revert_to) antes de ejecutar, no solo
aprobar/rechazar en binario. Los links de /hitl/approve|reject siguen
funcionando exactamente igual que antes -- no se tocan.
"""
import base64
import json
import os
import uuid
import urllib.request
import urllib.error
import urllib.parse
import boto3
from datetime import datetime, timezone

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")
GRAPH_KEY = os.getenv("GRAPH_KEY", "sao/digital_twin.json")
SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
GITOPS_TOKEN_SECRET = os.getenv("GITOPS_TOKEN_SECRET", "")
GITOPS_MANIFESTS_REPO = os.getenv("GITOPS_MANIFESTS_REPO", "")
CONSOLE_TOKEN_PARAM = os.getenv("CONSOLE_TOKEN_PARAM", "")
PROPOSALS_PREFIX = "proposals/"

s3 = boto3.client("s3", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)
ssm = boto3.client("ssm", region_name=AWS_REGION)


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
    """
    Escribe el precedente del fix ejecutado en el Digital Twin (S3). No-blocking.
    Modulo 10: si el humano ajusto parametros antes de aprobar (approved_params
    presente y distinto de action_params), el precedente registra AMBAS
    versiones -- la proxima vez que Bedrock busque precedentes similares debe
    aprender del ajuste humano, no de su propia sugerencia sin corregir.
    """
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
        "proposed_params": proposal.get("action_params", {}),
        "embedding": _compute_embedding(embed_text),
    }
    approved_params = proposal.get("approved_params")
    if approved_params is not None and approved_params != proposal.get("action_params", {}):
        precedent["approved_params"] = approved_params
        precedent["human_adjusted"] = True

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


def _extract_tag(content_b64: str) -> str:
    """
    Extrae el valor de newTag de un kustomization.yaml en base64 -- regex simple
    en vez de un parser YAML completo (mismo criterio que el resto del proyecto:
    sin dependencias/Lambda Layer, ver Modulo 1). Devuelve "" si no matchea.
    """
    import re
    text = base64.b64decode(content_b64).decode()
    m = re.search(r"newTag:\s*(\S+)", text)
    return m.group(1) if m else ""


def _get_manifest_tag_history(path: str, limit: int = 5) -> list:
    """
    Fix de contexto real del nucleo (2026-08-12): historial real de commits
    que tocaron este manifiesto, con el tag de imagen que tenia en cada uno --
    le da a Bedrock el "ultimo tag bueno conocido" para revertir. Invocado
    directo por mcp_server (que corre zero-egress, sin salida a GitHub ni a
    Secrets Manager) via lambda:InvokeFunction -- mismo patron que
    _invoke_hitl_approve pero al reves: este Lambda (que si tiene salida a
    internet, no esta en la VPC) hace de proxy hacia GitHub para el
    razonador aislado.
    """
    token = _get_gitops_token()
    commits = _github_request(
        "GET", f"/repos/{GITOPS_MANIFESTS_REPO}/commits?path={path}&sha=main&per_page={limit}", token,
    )
    history = []
    for c in commits:
        sha = c["sha"]
        tag = ""
        try:
            content = _github_request("GET", f"/repos/{GITOPS_MANIFESTS_REPO}/contents/{path}?ref={sha}", token)
            tag = _extract_tag(content["content"])
        except Exception as e:
            print(f"No se pudo leer {path} en {sha[:12]}: {e}")
        history.append({
            "sha": sha,
            "date": c["commit"]["author"]["date"],
            "message": c["commit"]["message"].splitlines()[0],
            "tag": tag,
        })
    return history


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
        # Modulo 4: guardados para la fase de erradicacion -- bad_tag es lo
        # que estaba en main ANTES del revert (la causa del incidente),
        # good_tag es a lo que se revirtio. current_file ya tiene el
        # contenido de main (la rama nueva se creo desde ahi), no hace
        # falta un fetch extra.
        "path": path,
        "bad_tag": _extract_tag(current_file["content"]),
        "good_tag": _extract_tag(old_content_b64),
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


def _console_page(status_code: int, title: str, body: str) -> dict:
    """Version con un poco mas de estilo para las rutas de consola (Modulo 10)
    -- tablas y formularios, sigue siendo HTML servido por el Lambda, sin
    framework de frontend ni build step."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": f"""<!DOCTYPE html>
<html><head><title>SAGA — {title}</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:780px;margin:40px auto;padding:0 20px;color:#16211d}}
a{{color:#0f6e64}}
table{{width:100%;border-collapse:collapse;margin-top:16px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #c7d0c9;font-size:14px}}
th{{font-size:11px;text-transform:uppercase;color:#74807a}}
.btn{{display:inline-block;padding:7px 16px;border-radius:5px;text-decoration:none;font-size:13.5px;margin-right:8px;border:none;cursor:pointer;font-family:inherit}}
.btn.approve{{background:#0f6e64;color:#fff}}
.btn.reject{{background:#a83731;color:#fff}}
label{{display:block;margin:12px 0;font-size:13.5px}}
input[type=text]{{font-family:ui-monospace,monospace;padding:6px 8px;width:100%;max-width:420px;margin-top:4px;border:1px solid #c7d0c9;border-radius:4px}}
pre{{white-space:pre-wrap;background:#f7f9f7;padding:14px;border-radius:6px;font-size:12.5px}}
.pill{{font-family:ui-monospace,monospace;font-size:11px;padding:2px 8px;border-radius:99px;background:#e4e9e5}}
</style></head>
<body><h2>{title}</h2>{body}
<hr><small><a href="/hitl/pending">&larr; volver a pendientes</a></small></body></html>""",
    }


def _check_console_auth(event: dict) -> bool:
    """
    Bearer token compartido para las rutas de consola (Modulo 10). Los links
    de /hitl/approve|reject NO pasan por aca -- son de un solo uso, seguros
    por posesion del link. /hitl/pending lista TODO en una sola URL, blast
    radius mayor, necesita su propio gate. Fail-closed: sin
    CONSOLE_TOKEN_PARAM configurado o sin match exacto, deniega.
    """
    if not CONSOLE_TOKEN_PARAM:
        return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth_header = headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer "):].strip()
    try:
        resp = ssm.get_parameter(Name=CONSOLE_TOKEN_PARAM, WithDecryption=True)
        expected = resp["Parameter"]["Value"]
    except Exception as e:
        print(f"No se pudo leer el token de consola: {e}")
        return False
    return presented == expected


def _list_pending_proposals() -> list:
    """Escanea proposals/ en S3 y devuelve las que esperan revision humana
    (status=pending -- tanto auto_execute como escalate arrancan asi, pero
    auto_execute se auto-aprueba casi al instante, en la practica esto lista
    las que de verdad estan esperando a un humano)."""
    items = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix=PROPOSALS_PREFIX):
        for obj in page.get("Contents", []):
            token = obj["Key"][len(PROPOSALS_PREFIX):-len(".json")]
            try:
                data = _load_proposal(token)
            except Exception:
                continue
            if data.get("status") == "pending":
                items.append({"token": token, **data})
    items.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return items


def _reject_proposal(token: str, proposal: dict, now: str) -> tuple:
    """Comun a GET /hitl/reject (link de email) y POST /hitl/review (consola).
    Devuelve (status_code, title, body_html)."""
    alarm_name = proposal.get("alarm_name", "unknown")
    node_id = proposal.get("node_id", "unknown")
    risk = proposal.get("risk_level", "MEDIUM")

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
    return (
        200,
        "Propuesta rechazada",
        f"La propuesta fue rechazada. No se tomó ninguna acción automatizada.<br><br>"
        f"<strong>Alarma:</strong> {alarm_name}<br><strong>Recurso:</strong> {node_id}",
    )


def _process_approval(token: str, proposal: dict, now: str, action_params_override: dict = None) -> tuple:
    """
    Ejecuta la accion de una propuesta aprobada. Comun a GET /hitl/approve
    (link de email, siempre ejecuta los action_params originales) y POST
    /hitl/review/{token} (consola, Modulo 10 -- el humano puede haber
    ajustado un parametro, ej. revert_to, antes de aprobar).

    action_params_override, si no es None, se guarda en
    proposal["approved_params"] SIN pisar proposal["action_params"] -- ese
    campo sigue siendo lo que la IA realmente propuso, para que
    _register_precedent pueda distinguir ambas versiones. Devuelve
    (status_code, title, body_html).
    """
    alarm_name = proposal.get("alarm_name", "unknown")
    node_id = proposal.get("node_id", "unknown")
    risk = proposal.get("risk_level", "MEDIUM")
    action = proposal.get("action", "none")
    decision_state = proposal.get("decision_state", "escalate")

    action_params = action_params_override if action_params_override is not None else proposal.get("action_params", {})
    if action_params_override is not None:
        proposal["approved_params"] = action_params_override

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
            proposal["gitops_path"] = result["path"]
            proposal["bad_tag"] = result["bad_tag"]
            proposal["good_tag"] = result["good_tag"]
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
            return (
                200,
                "PR abierto — en cola de auto-merge",
                f"<strong>PR:</strong> <a href=\"{result['html_url']}\">{result['html_url']}</a><br><br>"
                f"decision_state=auto_execute -- se mergea solo si el CI pasa "
                f"(lambda-hitl-poller revisa cada 1 min), sin click humano.<br>"
                f"<strong>Alarma:</strong> {alarm_name}<br><strong>Recurso:</strong> {node_id}",
            )

        # Modulo 4: un fix de argocd_rollback_via_git en escalate ya paso la
        # aprobacion humana para el CONTENIDO del PR, pero el merge en si lo
        # hace el humano a mano en GitHub cuando quiera -- no hay forma de
        # saber desde aca cuando pasa eso. Queda "pending_merge", y el poller
        # (mismo ciclo de 1 min que ya revisa pending_ci) chequea si ya se
        # mergeo antes de pasar a la verificacion de cierre de loop.
        if action == "argocd_rollback_via_git" and isinstance(result, dict):
            proposal["status"] = "pending_merge"
            proposal["pr_number"] = result["pr_number"]
            proposal["pr_branch"] = result["branch"]
            proposal["pr_opened_at"] = now
            proposal["gitops_path"] = result["path"]
            proposal["bad_tag"] = result["bad_tag"]
            proposal["good_tag"] = result["good_tag"]
            _save_proposal(token, proposal)
            _notify(
                f"[SAO] PR abierto, esperando merge humano — {alarm_name}",
                f"PR abierto y validado -- falta que lo mergees a mano en GitHub cuando "
                f"quieras revisarlo. Una vez mergeado, SAGA verifica solo que la alerta "
                f"se resuelva antes de generar el guardrail (Modulo 4).\n\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}\n"
                f"PR: {result['html_url']}\n\n"
                f"Propuesta original:\n{proposal.get('proposal_text', '')}",
            )
            print(f"Escalate PR opened, pending human merge: token={token} pr={result['pr_number']}")
            return (
                200,
                "PR abierto — esperando merge humano",
                f"<strong>PR:</strong> <a href=\"{result['html_url']}\">{result['html_url']}</a><br><br>"
                f"Mergealo a mano en GitHub cuando quieras. SAGA sigue el resto solo.<br>"
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
        return (
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
        return (
            500,
            "Error en la ejecucion",
            f"El fix no pudo ejecutarse.<br><br>"
            f"<strong>Error:</strong> {e}<br>"
            f"<strong>Accion intentada:</strong> {action}",
        )


def _handle_pending(event: dict) -> dict:
    if not _check_console_auth(event):
        return {"statusCode": 401, "body": "Unauthorized"}
    proposals = _list_pending_proposals()
    if not proposals:
        body = "<p>No hay propuestas pendientes ahora mismo.</p>"
    else:
        rows = "".join(
            f"<tr><td>{p.get('alarm_name','')}</td><td><span class=\"pill\">{p.get('risk_level','')}</span></td>"
            f"<td>{p.get('action','')}</td><td>{p.get('created_at','')[:19]}</td>"
            f"<td><a class=\"btn approve\" href=\"/hitl/review/{p['token']}\">Revisar</a></td></tr>"
            for p in proposals
        )
        body = f"<table><tr><th>Alarma</th><th>Riesgo</th><th>Acción</th><th>Creada</th><th></th></tr>{rows}</table>"
    return _console_page(200, f"Propuestas pendientes ({len(proposals)})", body)


def _handle_review_get(event: dict) -> dict:
    if not _check_console_auth(event):
        return {"statusCode": 401, "body": "Unauthorized"}
    token = (event.get("pathParameters") or {}).get("token", "")
    try:
        proposal = _load_proposal(token)
    except Exception:
        return _console_page(404, "No encontrada", "<p>No existe esa propuesta.</p>")
    if proposal.get("status") != "pending":
        return _console_page(409, "Ya procesada", f"<p>Estado actual: <b>{proposal.get('status')}</b></p>")

    action_params = proposal.get("action_params", {})
    # Form generico: un campo de texto por cada parametro de la accion
    # propuesta -- hoy en la practica es `path` + `revert_to`
    # (argocd_rollback_via_git), generaliza sin cambios a acciones futuras
    # con otros parametros.
    fields = "".join(
        f'<label>{k}<br><input type="text" name="{k}" value="{urllib.parse.quote(str(v))}"></label>'
        for k, v in action_params.items()
    )
    body = f"""
    <p><b>Alarma:</b> {proposal.get('alarm_name')} &nbsp;
    <b>Recurso:</b> {proposal.get('node_id')} &nbsp;
    <b>Riesgo (IA):</b> <span class="pill">{proposal.get('risk_level')}</span></p>
    <p><b>Acción propuesta:</b> {proposal.get('action')}</p>
    <details open><summary>Razonamiento completo de Bedrock</summary>
    <pre>{proposal.get('proposal_text','')}</pre></details>
    <form method="POST" action="/hitl/review/{token}">
      {fields}
      <p>
        <button class="btn approve" type="submit" name="decision" value="approve">Aprobar (con los valores de arriba)</button>
        <button class="btn reject" type="submit" name="decision" value="reject">Rechazar</button>
      </p>
    </form>
    """
    return _console_page(200, f"Revisar — {proposal.get('alarm_name')}", body)


def _handle_review_post(event: dict) -> dict:
    if not _check_console_auth(event):
        return {"statusCode": 401, "body": "Unauthorized"}
    token = (event.get("pathParameters") or {}).get("token", "")
    body_raw = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode()
    form = urllib.parse.parse_qs(body_raw)
    decision = form.get("decision", [""])[0]

    try:
        proposal = _load_proposal(token)
    except Exception:
        return _console_page(404, "No encontrada", "<p>No existe esa propuesta.</p>")
    if proposal.get("status") != "pending":
        return _console_page(409, "Ya procesada", f"<p>Estado actual: <b>{proposal.get('status')}</b></p>")

    now = datetime.now(tz=timezone.utc).isoformat()

    if decision == "reject":
        status_code, title, body = _reject_proposal(token, proposal, now)
        return _console_page(status_code, title, body)

    original_params = proposal.get("action_params", {})
    submitted_params = {k: v[0] for k, v in form.items() if k != "decision"}
    # Solo cuenta como "ajuste humano" si algun valor realmente cambio -- si
    # el form se mando tal cual vino, se ejecuta exactamente lo propuesto
    # (mismo comportamiento que aprobar por el link de email).
    override = submitted_params if submitted_params != original_params else None

    status_code, title, body = _process_approval(token, proposal, now, action_params_override=override)
    return _console_page(status_code, title, body)


def handler(event, context):
    # Fix de contexto real del nucleo (2026-08-12): invocacion directa (no via
    # API Gateway) para pedir el historial git de un manifiesto -- mcp_server
    # corre zero-egress y no puede llegar a GitHub/Secrets Manager el mismo,
    # se lo pide a este Lambda. Se distingue de los eventos de API Gateway
    # (que siempre traen rawPath) por la clave "action".
    if event.get("action") == "get_manifest_history":
        path = event.get("path", "overlays/dev/kustomization.yaml")
        try:
            history = _get_manifest_tag_history(path, event.get("limit", 5))
            return {"status": "ok", "history": history}
        except Exception as e:
            print(f"Error obteniendo historial de {path}: {e}")
            return {"status": "error", "error": str(e), "history": []}

    raw_path = event.get("rawPath", "")
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "GET")

    # Modulo 10: rutas de consola -- todas requieren CONSOLE_TOKEN_PARAM.
    if raw_path == "/hitl/pending" and method == "GET":
        return _handle_pending(event)
    if raw_path.startswith("/hitl/review/") and method == "GET":
        return _handle_review_get(event)
    if raw_path.startswith("/hitl/review/") and method == "POST":
        return _handle_review_post(event)

    # --- flujo original: links de un solo uso por email, sin cambios ---
    params = event.get("queryStringParameters") or {}
    token = params.get("token", "").strip()
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

    if action_type == "reject":
        status_code, title, body = _reject_proposal(token, proposal, now)
        return _html_response(status_code, title, body)

    status_code, title, body = _process_approval(token, proposal, now)
    return _html_response(status_code, title, body)
