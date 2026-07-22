"""
Lambda poller HITL — SV-AOP-012 Modulo 3.
Disparado por EventBridge cada 1 minuto. Revisa las propuestas en S3 con
status=pending_ci (PRs abiertos por decision_state=auto_execute, ver
lambda-hitl/handler.py) y consulta el estado real de los checks del PR en
GitHub: si todos pasaron, mergea solo; si alguno fallo o se agoto el
timeout, marca auto_reject. Nunca fuerza un merge sin checks verdes.
"""
import json
import os
import urllib.request
import urllib.error
import boto3
from datetime import datetime, timezone

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
GRAPH_BUCKET = os.getenv("GRAPH_BUCKET", "")
HITL_SNS_TOPIC = os.getenv("HITL_SNS_TOPIC", "")
GITOPS_TOKEN_SECRET = os.getenv("GITOPS_TOKEN_SECRET", "")
GITOPS_MANIFESTS_REPO = os.getenv("GITOPS_MANIFESTS_REPO", "")
CI_TIMEOUT_MINUTES = int(os.getenv("CI_TIMEOUT_MINUTES", "15"))
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


def _list_pending_ci_proposals() -> list:
    """Escanea proposals/ en S3 y devuelve (key, data) de las que están pending_ci."""
    found = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix=PROPOSALS_PREFIX):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=GRAPH_BUCKET, Key=obj["Key"])["Body"].read()
            data = json.loads(body)
            if data.get("status") == "pending_ci":
                found.append((obj["Key"], data))
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


def handler(event, context):
    token = _get_gitops_token()
    pending = _list_pending_ci_proposals()
    print(f"Poller: {len(pending)} propuestas en pending_ci")
    now = datetime.now(tz=timezone.utc)

    for key, proposal in pending:
        pr_number = proposal["pr_number"]
        head_sha = proposal["head_sha"]
        alarm_name = proposal.get("alarm_name", "unknown")
        node_id = proposal.get("node_id", "unknown")
        opened_at = datetime.fromisoformat(proposal["pr_opened_at"])
        age_minutes = (now - opened_at).total_seconds() / 60

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
            proposal["status"] = "executed"
            proposal["resolved_at"] = now.isoformat()
            _save_proposal(key, proposal)
            _notify(
                f"[SAO] Auto-merge completado — {alarm_name}",
                f"CI paso, PR #{pr_number} mergeado automaticamente sin intervencion humana "
                f"(decision_state=auto_execute).\nAlarma: {alarm_name}\nRecurso: {node_id}",
            )
            print(f"PR #{pr_number} mergeado (auto_execute)")

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

    return {"checked": len(pending)}
