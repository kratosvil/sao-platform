import json
import os
import urllib.request

MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]

NAMESPACE_TO_TYPE = {
    "AWS/Lambda": "Lambda",
    "AWS/EC2": "EC2Instance",
    "AWS/RDS": "RDSInstance",
    "AWS/ECS": "ECSService",
    "AWS/S3": "S3Bucket",
}


def handler(event, context):
    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName", "unknown")
    region = event.get("region", os.environ.get("AWS_REGION", "us-east-1"))
    account_id = event.get("account", "")

    node_id = alarm_name
    resource_type = "Unknown"

    metrics = detail.get("configuration", {}).get("metrics", [])
    if metrics:
        metric = metrics[0].get("metricStat", {}).get("metric", {})
        namespace = metric.get("namespace", "")
        resource_type = NAMESPACE_TO_TYPE.get(namespace, namespace)
        dimensions = metric.get("dimensions", {})
        if dimensions:
            node_id = list(dimensions.values())[0]

    payload = json.dumps({
        "alarm_name": alarm_name,
        "node_id": node_id,
        "resource_type": resource_type,
        "region": region,
        "account_id": account_id,
    }).encode()

    req = urllib.request.Request(
        f"{MCP_SERVER_URL}/incident",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=29) as resp:
            body = resp.read()
        print(f"alarm={alarm_name} node={node_id} mcp_status={resp.status} response={body[:200]}")
        return {"statusCode": resp.status}
    except urllib.error.HTTPError as e:
        body = e.read()[:200]
        print(f"alarm={alarm_name} node={node_id} mcp_error={e.code} response={body}")
        return {"statusCode": e.code}
