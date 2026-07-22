"""
Lambda poller HITL — SV-AOP-012 Modulo 3 (CI/auto-merge) + Modulo 4 (cierre de
loop + erradicacion). Disparado por EventBridge cada 1 minuto, tres etapas
independientes por ciclo:

1. pending_ci (decision_state=auto_execute): revisa si el CI del PR paso.
   Si paso, mergea solo. Si fallo o hizo timeout, marca auto_reject.
2. pending_merge (decision_state=escalate): revisa si un humano ya mergeo
   el PR a mano en GitHub. Si lo cerraron sin mergear, marca rejected.
3. pending_loop_closure (ambas rutas convergen aca tras el merge): revisa
   si la alerta original en Prometheus dejo de estar firing. Si se confirma,
   genera un guardrail OPA (PR nuevo, SIEMPRE escalate, nunca auto-merge) y
   registra el precedente. Si hace timeout sin confirmar, no genera guardrail
   -- un guardrail basado en un fix no verificado no sirve (ver estado.md).
"""
import base64
import json
import os
import re
import urllib.request
import urllib.error
import boto3
from datetime import datetime, timezone

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")
GRAPH_KEY = os.getenv("GRAPH_KEY", "sao/digital_twin.json")
HITL_SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
GITOPS_TOKEN_SECRET = os.getenv("GITOPS_TOKEN_SECRET", "")
GITOPS_MANIFESTS_REPO = os.getenv("GITOPS_MANIFESTS_REPO", "")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")
CI_TIMEOUT_MINUTES = int(os.getenv("CI_TIMEOUT_MINUTES", "15"))
LOOP_CLOSURE_TIMEOUT_MINUTES = int(os.getenv("LOOP_CLOSURE_TIMEOUT_MINUTES", "10"))
PROPOSALS_PREFIX = "proposals/"

s3 = boto3.client("s3", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)
secretsmanager = boto3.client("secretsmanager", region_name=AWS_REGION)


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
    return resp["SecretString"].strip()


def _list_proposals_by_status(*statuses: str) -> dict:
    """Un solo scan de proposals/ en S3, agrupado por status pedido."""
    found = {s: [] for s in statuses}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix=PROPOSALS_PREFIX):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=GRAPH_BUCKET, Key=obj["Key"])["Body"].read()
            data = json.loads(body)
            st = data.get("status")
            if st in found:
                found[st].append((obj["Key"], data))
    return found


def _check_run_conclusion(token: str, sha: str) -> str:
    """
    success | failure | pending -- pending si algun workflow run no completo o
    no hay runs aun. Usa la API de Actions (permiso "Actions: read") en vez de
    la API de Checks -- el PAT de grano fino de este repo no tiene "Checks"
    disponible como categoria (no explicado por GitHub, ver estado.md Modulo 3),
    Actions es equivalente para este caso (un solo workflow, validate.yml).
    """
    resp = _github_request(
        "GET", f"/repos/{GITOPS_MANIFESTS_REPO}/actions/runs?head_sha={sha}", token
    )
    runs = resp.get("workflow_runs", [])
    if not runs:
        return "pending"
    if any(r["status"] != "completed" for r in runs):
        return "pending"
    if all(r.get("conclusion") == "success" for r in runs):
        return "success"
    return "failure"


def _merge_pr(token: str, pr_number: int) -> dict:
    return _github_request(
        "PUT", f"/repos/{GITOPS_MANIFESTS_REPO}/pulls/{pr_number}/merge", token,
        {"merge_method": "squash"},
    )


def _pr_state(token: str, pr_number: int) -> dict:
    return _github_request("GET", f"/repos/{GITOPS_MANIFESTS_REPO}/pulls/{pr_number}", token)


def _alert_firing(alarm_name: str) -> bool:
    """True si la alerta sigue firing en Prometheus ahora mismo."""
    if not PROMETHEUS_URL:
        return False
    url = f"{PROMETHEUS_URL}/api/v1/query?query=ALERTS%7Balertname%3D%22{alarm_name}%22%2Calertstate%3D%22firing%22%7D"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
    return len(data.get("data", {}).get("result", [])) > 0


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _open_guardrail_pr(token: str, proposal: dict) -> dict:
    """
    Modulo 4: abre un PR con una policy OPA nueva que bloquea el tag/config
    que causo el incidente. SIEMPRE decision_state=escalate -- una policy
    generada nunca se auto-mergea, sea cual sea el riesgo del fix original
    que la origino (ver estado.md Modulo 4).
    """
    repo = GITOPS_MANIFESTS_REPO
    bad_tag = proposal.get("bad_tag", "")
    alarm_name = proposal.get("alarm_name", "unknown")
    slug = _slugify(f"{alarm_name}-{bad_tag}")
    policy_path = f"policy/generated/{slug}.rego"
    today = datetime.now(tz=timezone.utc).date().isoformat()

    rego = f"""package main

# Guardrail auto-generado por SAGA (SV-AOP-012 Modulo 4) tras confirmar en
# Prometheus que el incidente '{alarm_name}' se resolvio. Bloquea que se
# vuelva a desplegar el tag que lo causo. Revisar y mergear a mano -- las
# policies generadas nunca se auto-mergean.
deny[msg] {{
\tinput.kind == "Deployment"
\timg := input.spec.template.spec.containers[_].image
\tcontains(img, "{bad_tag}")
\tmsg := sprintf("guardrail SAGA: el tag '%v' causo el incidente '%v' (%v), bloqueado", ["{bad_tag}", "{alarm_name}", "{today}"])
}}
"""
    content_b64 = base64.b64encode(rego.encode()).decode()

    main_ref = _github_request("GET", f"/repos/{repo}/git/ref/heads/main", token)
    main_sha = main_ref["object"]["sha"]

    branch = f"saga-guardrail-{slug}"
    _github_request("POST", f"/repos/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch}", "sha": main_sha,
    })
    _github_request("PUT", f"/repos/{repo}/contents/{policy_path}", token, {
        "message": f"saga: guardrail para incidente '{alarm_name}' (tag {bad_tag})",
        "content": content_b64,
        "branch": branch,
    })
    pr = _github_request("POST", f"/repos/{repo}/pulls", token, {
        "title": f"SAGA guardrail: bloquear tag {bad_tag} ({alarm_name})",
        "head": branch,
        "base": "main",
        "body": (
            f"Guardrail auto-generado tras confirmar que el fix resolvio el incidente "
            f"real en Prometheus (Modulo 4, SV-AOP-012).\n\n"
            f"- Alarma original: `{alarm_name}`\n"
            f"- Tag bloqueado: `{bad_tag}`\n"
            f"- Tag bueno vigente: `{proposal.get('good_tag', '?')}`\n\n"
            f"**Requiere aprobacion humana explicita -- las policies generadas nunca "
            f"se auto-mergean, sin excepcion.**"
        ),
    })
    return pr


def _register_precedent(proposal: dict) -> None:
    """
    Version minima del registro de precedentes (sin Titan embeddings -- el
    poller no tiene permiso bedrock:InvokeModel, no vale la pena el alcance
    de IAM extra solo para esto). embedding=[] es el mismo fallback que ya
    usa lambda-hitl/handler.py cuando Titan falla, no es un caso nuevo.
    """
    if not GRAPH_KEY:
        return
    try:
        obj = s3.get_object(Bucket=GRAPH_BUCKET, Key=GRAPH_KEY)
        twin = json.loads(obj["Body"].read())
    except Exception as e:
        print(f"No se pudo cargar el Digital Twin para el precedente: {e}")
        return

    nodes = [proposal["node_id"]] if proposal.get("node_id") else []
    precedent = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "agent": "sao-hitl-poller",
        "intent": proposal.get("alarm_name", "unknown"),
        "action": proposal.get("action", "none"),
        "outcome": "Success",
        "confidence": 1.0,
        "nodes_affected": nodes,
        "embedding": [],
    }
    twin.setdefault("precedents", {}).setdefault("remediations", []).append(precedent)
    try:
        s3.put_object(
            Bucket=GRAPH_BUCKET, Key=GRAPH_KEY,
            Body=json.dumps(twin, default=str).encode(),
            ContentType="application/json", ServerSideEncryption="aws:kms",
        )
    except Exception as e:
        print(f"No se pudo guardar el precedente: {e}")


def _save_proposal(key: str, data: dict) -> None:
    s3.put_object(
        Bucket=GRAPH_BUCKET, Key=key,
        Body=json.dumps(data, default=str).encode(), ContentType="application/json",
    )


def _notify(subject: str, message: str) -> None:
    if not HITL_SNS_TOPIC:
        return
    try:
        sns_client.publish(TopicArn=HITL_SNS_TOPIC, Subject=subject[:100], Message=message)
    except Exception as e:
        print(f"SNS publish failed: {e}")


def _process_pending_ci(token: str, items: list, now: datetime) -> None:
    for key, proposal in items:
        pr_number = proposal["pr_number"]
        head_sha = proposal["head_sha"]
        alarm_name = proposal.get("alarm_name", "unknown")
        node_id = proposal.get("node_id", "unknown")
        age_minutes = (now - datetime.fromisoformat(proposal["pr_opened_at"])).total_seconds() / 60

        try:
            conclusion = _check_run_conclusion(token, head_sha)
        except Exception as e:
            print(f"Error consultando checks de PR #{pr_number}: {e}")
            continue

        if conclusion == "success":
            try:
                _merge_pr(token, pr_number)
            except Exception as e:
                print(f"Merge de PR #{pr_number} fallo, se reintenta el proximo ciclo: {e}")
                continue
            proposal["status"] = "pending_loop_closure"
            proposal["merged_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] Auto-merge completado — {alarm_name}",
                f"CI paso, PR #{pr_number} mergeado automaticamente sin intervencion humana "
                f"(decision_state=auto_execute). Verificando que la alerta se resuelva antes "
                f"de dar el incidente por cerrado.\nAlarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} mergeado (auto_execute), pasa a pending_loop_closure")

        elif conclusion == "failure":
            proposal["status"] = "rejected"
            proposal["decision_state"] = "auto_reject"
            proposal["execution_error"] = "CI fallo -- ver checks del PR en GitHub"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] Auto-reject — CI fallo — {alarm_name}",
                f"El CI del PR #{pr_number} fallo. No se mergeo nada.\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} auto_reject (CI fallo)")

        elif age_minutes > CI_TIMEOUT_MINUTES:
            proposal["status"] = "rejected"
            proposal["decision_state"] = "auto_reject"
            proposal["execution_error"] = f"Timeout esperando CI ({CI_TIMEOUT_MINUTES} min)"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] Auto-reject — timeout CI — {alarm_name}",
                f"El PR #{pr_number} no completo el CI en {CI_TIMEOUT_MINUTES} min.\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} auto_reject (timeout)")

        else:
            print(f"PR #{pr_number} sigue en CI ({age_minutes:.1f} min)")


def _process_pending_merge(token: str, items: list, now: datetime) -> None:
    for key, proposal in items:
        pr_number = proposal["pr_number"]
        alarm_name = proposal.get("alarm_name", "unknown")
        node_id = proposal.get("node_id", "unknown")

        try:
            pr = _pr_state(token, pr_number)
        except Exception as e:
            print(f"Error consultando estado de PR #{pr_number}: {e}")
            continue

        if pr.get("merged"):
            proposal["status"] = "pending_loop_closure"
            proposal["merged_at"] = pr.get("merged_at") or now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] PR mergeado — {alarm_name}",
                f"PR #{pr_number} mergeado por un humano. Verificando que la alerta se "
                f"resuelva antes de dar el incidente por cerrado.\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} mergeado (humano), pasa a pending_loop_closure")
        elif pr.get("state") == "closed":
            proposal["status"] = "rejected"
            proposal["execution_error"] = "PR cerrado sin mergear en GitHub"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] PR cerrado sin mergear — {alarm_name}",
                f"Un humano cerro el PR #{pr_number} sin mergearlo.\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} cerrado sin merge")
        else:
            print(f"PR #{pr_number} sigue esperando merge humano")


def _process_pending_loop_closure(token: str, items: list, now: datetime) -> None:
    for key, proposal in items:
        alarm_name = proposal.get("alarm_name", "unknown")
        node_id = proposal.get("node_id", "unknown")
        pr_number = proposal.get("pr_number")
        merged_at = datetime.fromisoformat(proposal["merged_at"])
        age_minutes = (now - merged_at).total_seconds() / 60

        try:
            firing = _alert_firing(alarm_name)
        except Exception as e:
            print(f"Error consultando Prometheus para '{alarm_name}': {e}")
            continue

        if not firing:
            proposal["status"] = "resolved"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _register_precedent(proposal)
            print(f"Loop cerrado confirmado — alarma '{alarm_name}' ya no firing")

            guardrail_msg = ""
            if proposal.get("bad_tag"):
                try:
                    pr = _open_guardrail_pr(token, proposal)
                    proposal["guardrail_pr_number"] = pr["number"]
                    proposal["guardrail_pr_url"] = pr["html_url"]
                    _save_proposal(key, proposal)
                    guardrail_msg = f"\nGuardrail generado: {pr['html_url']} (requiere aprobacion humana)"
                    print(f"Guardrail PR #{pr['number']} abierto para bloquear tag {proposal['bad_tag']}")
                except Exception as e:
                    print(f"No se pudo abrir el PR de guardrail: {e}")
                    guardrail_msg = f"\nNo se pudo generar el guardrail automaticamente: {e}"

            _notify(
                f"[SAO] Incidente resuelto y confirmado — {alarm_name}",
                f"La alerta '{alarm_name}' dejo de estar firing -- el fix (PR #{pr_number}) "
                f"funciono de verdad, no solo se mergeo.\nAlarma: {alarm_name}\nRecurso: {node_id}"
                f"{guardrail_msg}",
            )

        elif age_minutes > LOOP_CLOSURE_TIMEOUT_MINUTES:
            proposal["status"] = "resolved_unconfirmed"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] Fix mergeado pero sin confirmar — {alarm_name}",
                f"El PR #{pr_number} se mergeo pero la alerta '{alarm_name}' sigue firing "
                f"{LOOP_CLOSURE_TIMEOUT_MINUTES} min despues -- no se genera guardrail sobre "
                f"un fix que no se pudo confirmar que funciono. Revisar a mano.\n"
                f"Alarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"Loop closure timeout para '{alarm_name}' -- sin guardrail, revisar a mano")

        else:
            print(f"'{alarm_name}' sigue firing, esperando ({age_minutes:.1f} min)")


def handler(event, context):
    token = _get_gitops_token()
    now = datetime.now(tz=timezone.utc)
    grouped = _list_proposals_by_status("pending_ci", "pending_merge", "pending_loop_closure")
    print(
        f"Poller: {len(grouped['pending_ci'])} pending_ci, "
        f"{len(grouped['pending_merge'])} pending_merge, "
        f"{len(grouped['pending_loop_closure'])} pending_loop_closure"
    )

    _process_pending_ci(token, grouped["pending_ci"], now)
    _process_pending_merge(token, grouped["pending_merge"], now)
    _process_pending_loop_closure(token, grouped["pending_loop_closure"], now)

    return {
        "checked_ci": len(grouped["pending_ci"]),
        "checked_merge": len(grouped["pending_merge"]),
        "checked_loop_closure": len(grouped["pending_loop_closure"]),
    }
