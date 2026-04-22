import json
import os
import boto3
from collectors import TfstateCollector, CloudWatchCollector

TFSTATE_BUCKET = os.environ["TFSTATE_BUCKET"]
TFSTATE_KEY = os.environ.get("TFSTATE_KEY", "sovereign-ops/terraform.tfstate")
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
GRAPH_KEY = os.environ.get("GRAPH_KEY", "sao/digital_twin.json")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def handler(event: dict, context) -> dict:
    """
    Entry point Lambda.
    Dispara en dos casos:
    - S3 event: nuevo tfstate subido (topology update)
    - EventBridge scheduled: actualizar dynamic_state cada 5 min
    """
    source = event.get("source", "scheduled")

    s3 = boto3.client("s3", region_name=AWS_REGION)

    # Cargar grafo existente o iniciar desde cero
    try:
        existing = json.loads(
            s3.get_object(Bucket=GRAPH_BUCKET, Key=GRAPH_KEY)["Body"].read()
        )
    except s3.exceptions.NoSuchKey:
        existing = {
            "digital_twin_id": "SAO-CORE-VPC-PROD-001",
            "version": "0.1.0",
            "ontology_standard": "Agentic-IaC-v1",
            "topology": {"nodes": [], "edges": []},
            "governance": {"frameworks": ["ConstitutionalAI"], "denied_actions": [], "mandatory_tags": {}},
            "dynamic_state": {"active_alarms": [], "agent_locks": {}, "alarm_correlations": []},
            "precedents": {"remediations": []},
            "constraints": {"maintenance_windows": [], "forbidden_ops": []},
        }

    # Actualizar topologia si hay nuevo tfstate
    if source in ("s3", "local", "manual"):
        tfcollector = TfstateCollector(TFSTATE_BUCKET, AWS_REGION)
        tfstate = tfcollector.load_tfstate(TFSTATE_KEY)
        nodes = tfcollector.extract_nodes(tfstate)
        edges = tfcollector.extract_edges(tfstate, nodes)
        existing["topology"]["nodes"] = nodes
        existing["topology"]["edges"] = edges

    # Actualizar dynamic_state siempre
    cw = CloudWatchCollector(AWS_REGION)
    existing["dynamic_state"]["active_alarms"] = cw.get_active_alarms()

    # Persistir grafo actualizado
    s3.put_object(
        Bucket=GRAPH_BUCKET,
        Key=GRAPH_KEY,
        Body=json.dumps(existing, indent=2, default=str),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
    )

    return {
        "statusCode": 200,
        "body": {
            "nodes_updated": len(existing["topology"]["nodes"]),
            "edges_updated": len(existing["topology"]["edges"]),
            "active_alarms": existing["dynamic_state"]["active_alarms"],
        },
    }
