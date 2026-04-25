# Extending the Digital Twin — Adding New AWS Resource Types

The Digital Twin is a living knowledge graph. Every `terraform apply` triggers the Lambda Collector, which reads the tfstate and extracts nodes (resources) and edges (relationships). This document explains the full mechanics and how to add support for new AWS resource types.

---

## Architecture Overview

```
terraform apply
      │
      ▼ S3 event (new tfstate)
Lambda Collector (lambda-collector/handler.py)
      │
      ├── TfstateCollector.load_tfstate()   ← reads S3
      ├── TfstateCollector.extract_nodes()  ← RELEVANT_ATTRS + _TF_TO_AWS
      └── TfstateCollector.extract_edges()  ← dependency inference
              │
              ▼ writes sao/digital_twin.json (KMS)
        S3 Graph Store
              │
              ▼ loaded at incident time
        MCP Server (app.py)
              │
              ├── GraphStore.load()
              ├── GraphQuery.context_for_agent()  ← topology traversal
              └── Titan embeddings → cosine similarity → top-k precedents
```

The key file is `lambda-collector/collectors/tfstate.py`. All resource type support lives there.

---

## Data Model

### Node

A node represents one AWS resource instance:

```json
{
  "id": "sao-lambda-collector",
  "type": "AWS::Lambda::Function",
  "tfstate_address": "root.aws_lambda_function.collector",
  "properties": {
    "function_name": "sao-lambda-collector",
    "runtime": "python3.12",
    "memory_size": 512,
    "timeout": 30,
    "arn": "arn:aws:lambda:us-east-1:<account-id>:function:sao-lambda-collector"
  },
  "tags": { "Project": "sao-platform" }
}
```

- `id` — short, human-readable, unique. Used in edges, alarms, and RAG queries. Never use ARNs as IDs (too verbose for context windows).
- `type` — AWS CloudFormation type. Used by resource plugins and CloudWatch namespace resolution.
- `properties` — only the attributes Claude needs to reason about incidents. Exclude IAM policies, encryption details, and anything not relevant to operational decisions.

### Edge

An edge represents a directed relationship between two nodes:

```json
{
  "head": "sao-lambda-collector",
  "relation": "SECURED_BY",
  "tail": "sg-0abc123456"
}
```

Edges enable **blast radius analysis**: if resource A fails, which downstream resources are affected?

### Supported Relation Types

| Relation | Meaning | Example |
|----------|---------|---------|
| `SECURED_BY` | Resource is protected by a Security Group | Lambda → sg-0abc123 |
| `RUNS_IN` | Resource runs inside a Subnet | EC2 → subnet-0def456 |
| `BELONGS_TO` | Resource is part of a parent resource | Subnet → VPC, ECS Service → ECS Cluster |
| `EXPOSES_VIA` | Resource is exposed through a load balancer | ECS Service → ALB |
| `CONNECTS_TO` | Resource has a network dependency | Lambda → RDS |
| `ROUTES_TO` | Traffic is routed to a target | API Gateway → Lambda |

---

## Currently Supported Resource Types

### Nodes

| Terraform Type | AWS Type | Node ID Source |
|----------------|----------|----------------|
| `aws_lambda_function` | `AWS::Lambda::Function` | `function_name` |
| `aws_ecs_cluster` | `AWS::ECS::Cluster` | `name` |
| `aws_ecs_service` | `AWS::ECS::Service` | `name` |
| `aws_db_instance` | `AWS::RDS::DBInstance` | `identifier` |
| `aws_rds_cluster` | `AWS::RDS::DBCluster` | `cluster_identifier` |
| `aws_instance` | `AWS::EC2::Instance` | `instance_id` |
| `aws_eks_cluster` | `AWS::EKS::Cluster` | `name` |
| `aws_vpc` | `AWS::EC2::VPC` | `id` |
| `aws_security_group` | `AWS::EC2::SecurityGroup` | `id` |
| `aws_subnet` | `AWS::EC2::Subnet` | `id` |
| `aws_lb` | `AWS::ElasticLoadBalancingV2::LoadBalancer` | ARN segment (second-to-last `/`) |

### Edges

| Relation | Head | Tail | Inferred from |
|----------|------|------|---------------|
| `SECURED_BY` | Lambda | SecurityGroup | `vpc_config[0].security_group_ids` |
| `RUNS_IN` | Lambda | Subnet | `vpc_config[0].subnet_ids` |
| `BELONGS_TO` | ECS Service | ECS Cluster | `cluster` ARN (split on `/`) |
| `EXPOSES_VIA` | ECS Service | ALB | `load_balancer[].target_group_arn` |
| `SECURED_BY` | RDS Instance / Aurora Cluster | SecurityGroup | `vpc_security_group_ids` |
| `SECURED_BY` | EC2 Instance | SecurityGroup | `vpc_security_group_ids` |
| `RUNS_IN` | EC2 Instance | Subnet | `subnet_id` |
| `RUNS_IN` | EKS Cluster | Subnet | `vpc_config[0].subnet_ids` |
| `BELONGS_TO` | Subnet | VPC | `vpc_id` |
| `BELONGS_TO` | SecurityGroup | VPC | `vpc_id` |

---

## Adding a New Resource Type — Step by Step

All changes are in `lambda-collector/collectors/tfstate.py`.

### Step 1 — Add relevant attributes to `RELEVANT_ATTRS`

This dict controls which attributes are extracted from the verbose tfstate. Include what Claude needs to reason operationally; exclude policy documents, encryption keys, and derived values.

```python
RELEVANT_ATTRS = {
    # ... existing entries ...

    "aws_sqs_queue": [
        "name", "arn", "fifo_queue",
        "visibility_timeout_seconds", "message_retention_seconds",
    ],
}
```

**Rule of thumb:** include attributes that describe behavior (timeouts, capacity, mode), connectivity (ARNs referenced by other resources), and operational state. Exclude: KMS key ARNs, policy JSON, S3 bucket notification configs.

### Step 2 — Map Terraform type to AWS type in `_TF_TO_AWS`

Used by resource plugins and CloudWatch namespace resolution:

```python
_TF_TO_AWS = {
    # ... existing entries ...
    "aws_sqs_queue": "AWS::SQS::Queue",
}
```

The AWS type must match the CloudFormation resource type exactly. When unsure, check the [AWS CloudFormation resource type reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-template-resource-type-ref.html).

### Step 3 — Add node ID logic to `_make_node_id`

The node ID must be short, unique, and human-readable:

```python
def _make_node_id(self, rtype: str, attrs: dict) -> str:
    # ... existing entries ...
    if rtype == "aws_sqs_queue": return attrs.get("name", "unknown-sqs")
    # ...
```

Use the primary name or logical identifier:
- Prefer: `function_name`, `name`, `identifier`, `cluster_identifier`
- Avoid: full ARNs (too long for reasoning), generated IDs like `i-0abcd1234` unless no name exists

### Step 4 — Add edge inference to `extract_edges` (if applicable)

If the resource has relationships to other nodes (network, ownership, routing), add inference logic inside the `for resource in tfstate.get("resources", [])` loop:

```python
# SQS Queue triggered by Lambda (CONNECTS_TO via event source mapping)
if rtype == "aws_lambda_event_source_mapping":
    function_name = attrs.get("function_name", "").split(":")[-1]
    queue_arn = attrs.get("event_source_arn", "")
    queue_name = queue_arn.split(":")[-1]
    if function_name in node_ids and queue_name in node_ids:
        edges.append({
            "head": function_name,
            "relation": "CONNECTS_TO",
            "tail": queue_name,
        })
```

Note: edges can be inferred from a different resource type than the node itself. Here the `aws_lambda_event_source_mapping` resource links Lambda → SQS even though `aws_sqs_queue` is the node type.

---

## Adding a New Edge Type

If the existing relation types are not sufficient, define a new one. Add it to the relation table in this document and use it consistently:

```python
# API Gateway → Lambda (ROUTES_TO via integration)
if rtype == "aws_api_gateway_integration":
    uri = attrs.get("uri", "")
    if "lambda" in uri:
        function_name = uri.split("function:")[-1].split("/")[0]
        api_id = attrs.get("rest_api_id", "")
        if api_id in node_ids and function_name in node_ids:
            edges.append({
                "head": api_id,
                "relation": "ROUTES_TO",
                "tail": function_name,
            })
```

---

## Complete Example: Adding SQS Support

Full change set for `aws_sqs_queue`:

```python
# 1. RELEVANT_ATTRS — add before the closing brace
"aws_sqs_queue": [
    "name", "arn", "fifo_queue",
    "visibility_timeout_seconds", "message_retention_seconds",
],

# 2. _TF_TO_AWS
"aws_sqs_queue": "AWS::SQS::Queue",

# 3. _make_node_id — add before the final return
if rtype == "aws_sqs_queue": return attrs.get("name", "unknown-sqs")

# 4. extract_edges — add inside the resource loop
# aws_sqs_queue itself has no outbound references; relationships come
# from aws_lambda_event_source_mapping (separate resource type).
# Add this block to infer Lambda → SQS edges:
if rtype == "aws_lambda_event_source_mapping":
    fn = attrs.get("function_name", "").split(":")[-1]
    q  = attrs.get("event_source_arn", "").split(":")[-1]
    if fn in node_ids and q in node_ids:
        edges.append({"head": fn, "relation": "CONNECTS_TO", "tail": q})
```

---

## Adding a Resource Plugin (Execution Support)

A resource plugin enables SAO to **execute actions** on the new resource type via the HITL flow. Plugins live in `mcp-server/resources/`.

### Create the plugin file

```python
# mcp-server/resources/sqs.py
from .base import ResourcePlugin

class SQSPlugin(ResourcePlugin):
    SUPPORTED_ACTIONS = {
        "sqs_purge_queue":          "MEDIUM",
        "sqs_update_visibility":    "LOW",
    }

    def risk_level(self, action: str) -> str:
        return self.SUPPORTED_ACTIONS.get(action, "HIGH")

    def execute_action(self, action: str, node_id: str, params: dict) -> str:
        import boto3
        sqs = boto3.client("sqs", region_name=self.region)

        if action == "sqs_purge_queue":
            queue_url = params["queue_url"]
            sqs.purge_queue(QueueUrl=queue_url)
            return f"SQS {node_id}: queue purged"

        if action == "sqs_update_visibility":
            queue_url = params["queue_url"]
            timeout = int(params["visibility_timeout_seconds"])
            sqs.set_queue_attributes(
                QueueUrl=queue_url,
                Attributes={"VisibilityTimeout": str(timeout)},
            )
            return f"SQS {node_id}: visibility timeout set to {timeout}s"

        raise ValueError(f"Unknown action: {action}")
```

### Register the plugin

In `mcp-server/resources/__init__.py`, add the mapping from AWS type to plugin class:

```python
from .sqs import SQSPlugin

_REGISTRY = {
    # ... existing entries ...
    "AWS::SQS::Queue": SQSPlugin,
}
```

### Add actions to the Lambda HITL Executor

The Lambda HITL Executor (`lambda-hitl/handler.py`) executes actions directly via boto3, independently of the MCP plugin system. Add the new actions to `_execute_action()`:

```python
if action == "sqs_purge_queue":
    queue_url = params["queue_url"]
    sqs = boto3.client("sqs", region_name=AWS_REGION)
    sqs.purge_queue(QueueUrl=queue_url)
    return f"SQS: queue {queue_url} purged"
```

And add the new action to the Bedrock prompt template in `mcp-server/app.py` (`_build_prompt`):

```
- sqs_purge_queue queue_url=<url>
- sqs_update_visibility queue_url=<url> visibility_timeout_seconds=<n_int>
```

---

## Updating the Digital Twin After Changes

No infrastructure changes needed. The Lambda Collector already runs on every `terraform apply`. To apply changes immediately:

```bash
# 1. Rebuild the Lambda ZIP
make build-collector

# 2. Upload the new code to AWS
aws lambda update-function-code \
  --function-name sao-lambda-collector \
  --zip-file fileb://lambda-collector/collector.zip \
  --region us-east-1

# 3. Invoke manually to rebuild the Digital Twin now
aws lambda invoke \
  --function-name sao-lambda-collector \
  --payload '{"source":"manual"}' \
  --region us-east-1 \
  /tmp/response.json && cat /tmp/response.json

# 4. Verify the new nodes/edges appeared
make debug-rag
# or check the raw JSON:
aws s3 cp s3://<your-graph-bucket>/sao/digital_twin.json - \
  | python3 -m json.tool | grep -A5 '"type": "AWS::SQS'
```

---

## Governance Layer — Denying Actions

To prevent SAO from ever proposing a specific action on a resource, add it to the `governance.denied_actions` array in the Digital Twin JSON. This is enforced at the MCP layer before Bedrock is called.

Edit `sao/digital_twin.json` in S3 directly, or update the governance config in the Lambda Collector:

```json
"governance": {
  "frameworks": ["ConstitutionalAI"],
  "denied_actions": [
    { "tool": "sqs", "action": "sqs_purge_queue", "node_id": "prod-orders-queue" }
  ],
  "mandatory_tags": { "Environment": "required" }
}
```

The `node_id` field is optional — omit it to deny the action on all nodes of that type.

---

## Constraints Layer — Maintenance Windows

To prevent any automated action during a maintenance window, add to `constraints.forbidden_ops`:

```json
"constraints": {
  "maintenance_windows": [
    {
      "name": "weekly-maintenance",
      "start": "2026-05-04T02:00:00Z",
      "end":   "2026-05-04T04:00:00Z",
      "affected_nodes": ["prod-aurora-cluster"]
    }
  ],
  "forbidden_ops": []
}
```

The MCP Server checks this layer in `sao_validate_action` before any execution.

---

## Precedents Layer — RAG

Precedents are written automatically by the Lambda HITL Executor after every approved fix. Each precedent includes a Titan Embeddings vector (1536 dims) of the incident+outcome text:

```json
{
  "timestamp": "2026-04-24T18:30:00Z",
  "agent": "sao-hitl-executor",
  "intent": "sao-collector-errors",
  "action": "lambda_update_memory",
  "outcome": "Success",
  "confidence": 1.0,
  "nodes_affected": ["sao-lambda-collector"],
  "embedding": [0.023, -0.041, ...]
}
```

At incident time, the MCP Server (`app.py`) computes an embedding of the current incident query and retrieves the top-k most similar past incidents via cosine similarity. These are injected into the Bedrock context before reasoning.

You do not need to manage precedents manually. They accumulate automatically as the system resolves incidents.

---

## Checklist: Adding a New Resource Type

- [ ] Add to `RELEVANT_ATTRS` in `tfstate.py` — only operationally relevant attributes
- [ ] Add to `_TF_TO_AWS` in `tfstate.py` — correct AWS CloudFormation type
- [ ] Add to `_make_node_id` in `tfstate.py` — short, human-readable, unique
- [ ] Add edge inference to `extract_edges` if the resource has relationships
- [ ] Rebuild and upload Lambda Collector ZIP (`make build-collector`)
- [ ] Invoke collector manually to verify new nodes appear in Digital Twin
- [ ] (Optional) Add plugin in `mcp-server/resources/` if execution support is needed
- [ ] (Optional) Add actions to `lambda-hitl/handler.py` `_execute_action()`
- [ ] (Optional) Add action names to Bedrock prompt in `app.py` `_build_prompt()`
- [ ] Update this document with the new resource type in the supported types tables
