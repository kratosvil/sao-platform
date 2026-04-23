# Extending the Digital Twin — Adding New Resource Types

The Digital Twin is a living knowledge graph. Every `terraform apply` from the client triggers the Lambda Collector, which reads the tfstate and extracts nodes (resources) and edges (relationships). This document explains how to add support for new AWS resource types.

---

## Core Concepts

### Nodes
A node represents one AWS resource. It has:
- `id` — unique identifier (usually the resource name or ID)
- `type` — AWS CloudFormation type (e.g. `AWS::Lambda::Function`)
- `tfstate_address` — full address in the tfstate file
- `properties` — relevant attributes extracted from tfstate
- `tags` — resource tags

### Edges
An edge represents a relationship between two nodes. It has:
- `head` — source node ID
- `relation` — relationship type (see table below)
- `tail` — target node ID

| Relation | Meaning | Example |
|----------|---------|---------|
| `SECURED_BY` | Resource is protected by a Security Group | Lambda → sg-0abc123 |
| `RUNS_IN` | Resource runs inside a Subnet | EC2 → subnet-0def456 |
| `BELONGS_TO` | Resource is part of a parent resource | Subnet → VPC, ECS Service → ECS Cluster |
| `EXPOSES_VIA` | Resource is exposed through a load balancer | ECS Service → ALB |
| `CONNECTS_TO` | Resource has a network dependency | Lambda → RDS |

Edges are what allow Claude to reason about **blast radius** — if resource A fails, which other resources downstream are affected?

---

## Adding a New Resource Type

The file to edit is `lambda-collector/collectors/tfstate.py`. Every new resource type requires changes in 3 places.

### Step 1 — Add attributes to `RELEVANT_ATTRS`

This dict controls which attributes are extracted from the tfstate (the tfstate is verbose — filter only what Claude needs to reason):

```python
RELEVANT_ATTRS = {
    # ... existing entries ...

    "aws_sqs_queue": [
        "name", "arn", "fifo_queue", "visibility_timeout_seconds",
        "message_retention_seconds", "delay_seconds",
    ],
}
```

Rule of thumb: include attributes that describe behavior (timeouts, size, mode) and connectivity (ARNs, IDs used by other resources). Exclude: policy documents, encryption details, and anything not relevant to incident reasoning.

### Step 2 — Add the node ID logic to `_make_node_id`

The node ID must be unique and human-readable:

```python
def _make_node_id(self, rtype: str, attrs: dict) -> str:
    # ... existing entries ...
    if rtype == "aws_sqs_queue":    return attrs.get("name", "unknown-sqs")
    # ...
```

Use the resource's primary name or identifier — not the ARN (too long) and not a generated ID (not readable).

### Step 3 — Add the type mapping to `_TF_TO_AWS`

Maps Terraform resource type → AWS CloudFormation type (used in CloudWatch namespace resolution):

```python
_TF_TO_AWS = {
    # ... existing entries ...
    "aws_sqs_queue": "AWS::SQS::Queue",
}
```

---

## Adding a New Edge Type

Edges are inferred inside `extract_edges()` by reading resource attributes that reference other resources. Add the logic inside the loop that iterates over resources.

### Example: Lambda → SQS (CONNECTS_TO via event source mapping)

If a Lambda has an SQS trigger (`aws_lambda_event_source_mapping`), there is a dependency:

```python
# Lambda → SQS (CONNECTS_TO via event source mapping)
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

### Example: API Gateway → Lambda (ROUTES_TO)

```python
# API Gateway → Lambda (ROUTES_TO via integration)
if rtype == "aws_api_gateway_integration":
    uri = attrs.get("uri", "")
    # URI format: arn:aws:apigateway:region:lambda:path/functions/arn:aws:lambda:...:function:name/invocations
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

## Complete Example: Adding SQS

Full diff for `aws_sqs_queue` support:

```python
# 1. RELEVANT_ATTRS
"aws_sqs_queue": [
    "name", "arn", "fifo_queue", "visibility_timeout_seconds",
    "message_retention_seconds", "delay_seconds",
],

# 2. _TF_TO_AWS
"aws_sqs_queue": "AWS::SQS::Queue",

# 3. _make_node_id
if rtype == "aws_sqs_queue": return attrs.get("name", "unknown-sqs")

# 4. extract_edges (optional — only if there are Lambdas triggered by this queue)
# No direct attribute on aws_sqs_queue points to Lambda.
# The relationship lives in aws_lambda_event_source_mapping (see above).
```

---

## Currently Supported Resource Types

| Terraform Type | AWS Type | Node ID |
|---------------|----------|---------|
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
| `aws_lb` | `AWS::ElasticLoadBalancingV2::LoadBalancer` | ARN segment |

## Currently Supported Edge Types

| Relation | Head | Tail | Source attribute |
|----------|------|------|-----------------|
| `SECURED_BY` | Lambda, EC2, RDS, Aurora | SecurityGroup | `vpc_config.security_group_ids`, `vpc_security_group_ids` |
| `RUNS_IN` | Lambda, EC2, EKS | Subnet | `vpc_config.subnet_ids`, `subnet_id` |
| `BELONGS_TO` | Subnet, SecurityGroup | VPC | `vpc_id` |
| `BELONGS_TO` | ECS Service | ECS Cluster | `cluster` ARN |
| `EXPOSES_VIA` | ECS Service | ALB | `load_balancer.target_group_arn` |

---

## After Adding a New Resource Type

No infrastructure changes required. The Lambda Collector already runs on every `terraform apply`. To update the Digital Twin immediately:

```bash
# 1. Rebuild the Lambda ZIP
make build-collector

# 2. Update the Lambda function code in AWS
aws lambda update-function-code \
  --function-name sao-lambda-collector \
  --zip-file fileb://lambda-collector/collector.zip \
  --region us-east-1

# 3. Invoke manually to rebuild the Digital Twin now
aws lambda invoke \
  --function-name sao-lambda-collector \
  --payload '{"source": "manual"}' \
  --region us-east-1 \
  /tmp/response.json && cat /tmp/response.json
```

The Digital Twin in S3 will be updated immediately with the new nodes and edges.
