import json
import os
import boto3
from collectors import TfstateCollector

TFSTATE_BUCKET = os.environ["TFSTATE_BUCKET"]
TFSTATE_KEY = os.environ.get("TFSTATE_KEY", "sovereign-ops/terraform.tfstate")
GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
GRAPH_KEY = os.environ.get("GRAPH_KEY", "sao/digital_twin.json")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def handler(event: dict, context) -> dict:
    """
    Dispara cuando hay un nuevo tfstate en S3.
    Actualiza el Digital Twin: topology, governance, precedents, constraints.
    dynamic_state (alarmas, metricas) lo obtiene el MCP Server en tiempo real
    al momento del incidente — no se cachea aqui.
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)

    tfstate_key = (
        event.get("key")
        or event.get("detail", {}).get("object", {}).get("key")
        or TFSTATE_KEY
    )

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
            "precedents": {"remediations": []},
            "constraints": {"maintenance_windows": [], "forbidden_ops": []},
        }

    tfcollector = TfstateCollector(TFSTATE_BUCKET, AWS_REGION)
    tfstate = tfcollector.load_tfstate(tfstate_key)
    nodes = tfcollector.extract_nodes(tfstate)
    edges = tfcollector.extract_edges(tfstate, nodes)
    existing["topology"]["nodes"] = nodes
    existing["topology"]["edges"] = edges

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
            "nodes_updated": len(nodes),
            "edges_updated": len(edges),
            "tfstate_key": tfstate_key,
        },
    }
