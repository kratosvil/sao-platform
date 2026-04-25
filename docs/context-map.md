# Context Map — Internals Reference

This document covers the full internals of the `mcp-server/context_map/` module and the `mcp-server/resources/` plugin system. It is the authoritative reference for anyone (human or LLM) working on the reasoning engine of SAO Platform.

If you are reading this to understand how the system works end-to-end, start with the README first, then come back here.

---

## Role in the System

The context_map module is the brain of the MCP Server. It has three responsibilities:

1. **Schema** (`schema.py`) — defines the complete data model of the Digital Twin as Pydantic models. Every piece of infrastructure state, governance rule, past remediation, and operational constraint is a typed Python object.

2. **Store** (`store.py`) — serializes and deserializes the Digital Twin to/from S3. The Digital Twin is a single JSON file (`sao/digital_twin.json`) that the Lambda Collector writes and the MCP Server reads at incident time.

3. **Query** (`query.py`) — performs multi-hop graph traversal and semantic search over the Digital Twin. This is what builds the structured context passed to Bedrock before reasoning.

The resource plugin system (`resources/`) is separate but closely related: it defines how each AWS resource type is read and mutated after HITL approval.

---

## Data Model — Full Schema

All models are in `mcp-server/context_map/schema.py`.

### DigitalTwin (root object)

The root object stored in S3. Contains all five layers.

```python
class DigitalTwin(BaseModel):
    digital_twin_id: str          # Unique ID for this twin — e.g. "SAO-CORE-VPC-PROD-001"
    version: str                  # Semantic version — e.g. "0.1.0"
    ontology_standard: str        # Always "Agentic-IaC-v1" for now
    topology: TopologyLayer
    governance: GovernanceLayer
    dynamic_state: DynamicStateLayer
    precedents: PrecedentsLayer
    constraints: ConstraintTopology
```

**Methods:**
- `get_node(node_id) → Node | None` — returns the node with the given ID, or None
- `get_neighbors(node_id) → list[str]` — returns all node IDs connected to this node in either direction (head or tail of any edge)
- `is_locked(node_id) → bool` — returns True if an agent currently holds a lock on this node
- `is_action_denied(tool, action, node_id) → bool` — checks governance layer; supports `fnmatch` wildcards in `denied.target` (e.g. `prod-*`)

---

### Layer 1 — TopologyLayer

What resources exist and how they connect. Written by the Lambda Collector on every `terraform apply`.

```python
class TopologyLayer(BaseModel):
    nodes: list[Node]
    edges: list[Edge]

class Node(BaseModel):
    id: str                 # Short human-readable ID — e.g. "sao-lambda-collector"
    type: str               # AWS CloudFormation type — e.g. "AWS::Lambda::Function"
    tfstate_address: str    # Full Terraform address — e.g. "root.aws_lambda_function.collector"
    properties: dict        # Filtered attributes from tfstate (see RELEVANT_ATTRS in tfstate.py)
    tags: dict[str, str]    # Resource tags from tfstate

class Edge(BaseModel):
    head: str               # Source node ID
    relation: str           # SECURED_BY | RUNS_IN | BELONGS_TO | EXPOSES_VIA | CONNECTS_TO
    tail: str               # Target node ID
    properties: dict        # Optional metadata (currently unused)
```

**Important:** `Node.id` is the short name used throughout the system — in edges, in HITL proposals, in Bedrock prompts. It is never the full ARN. See `extending-digital-twin.md` for how node IDs are assigned.

---

### Layer 2 — GovernanceLayer

Defines what the agent is and is not allowed to do. Checked before any action proposal reaches Bedrock, and again before execution.

```python
class GovernanceLayer(BaseModel):
    frameworks: list[str]              # e.g. ["ConstitutionalAI", "Guardrails-v2"]
    denied_actions: list[DeniedAction]
    mandatory_tags: dict[str, str]     # Tags every resource must have
    compliance_scope: list[str]        # e.g. ["SOC2", "HIPAA"]

class DeniedAction(BaseModel):
    tool: str        # Plugin name — e.g. "lambda", "ecs"
    action: str      # Action name — e.g. "stop_service"
    target: str      # Node ID or fnmatch pattern — e.g. "prod-*", "sao-lambda-collector"
    condition: str   # Optional condition description (informational)
```

**How it works:** `DigitalTwin.is_action_denied(tool, action, node_id)` iterates `denied_actions` and matches `node_id` against `target` using `fnmatch`. This means `prod-*` blocks any action on any node whose ID starts with `prod-`.

---

### Layer 3 — DynamicStateLayer

Live operational state. The Lambda Collector does NOT write this layer — it is populated by the MCP Server at incident time by querying CloudWatch directly.

```python
class DynamicStateLayer(BaseModel):
    active_alarms: list[str]              # Names of currently firing CloudWatch alarms
    agent_locks: dict[str, str]           # {node_id: agent_id} — prevents concurrent operations
    alarm_correlations: list[AlarmCorrelation]
    last_updated: datetime

class AlarmCorrelation(BaseModel):
    alarm_name: str
    impact_nodes: list[str]    # Nodes known to be affected by this alarm
    causal_hints: list[str]    # Human-authored hints for Bedrock — e.g. "Check VPC endpoint status"
    severity: RiskLevel
```

**Agent locks:** before executing an action, the MCP Server sets `agent_locks[node_id] = "sao-agent"` and saves the twin. On completion or failure, the lock is released. This prevents two concurrent incidents from modifying the same resource simultaneously.

**Alarm correlations** are optional manual hints that can be pre-loaded into the twin to guide Bedrock reasoning for known alarm patterns. Currently empty in the MVP — populated manually as operational knowledge accumulates.

---

### Layer 4 — PrecedentsLayer

History of every executed remediation, with Titan Embeddings for semantic search. Written by `lambda-hitl/handler.py` after every approved HITL action.

```python
class PrecedentsLayer(BaseModel):
    remediations: list[Precedent]

class Precedent(BaseModel):
    timestamp: datetime
    agent: str              # Always "sao-hitl-executor" for HITL-executed actions
    intent: str             # Alarm name that triggered the incident
    action: str             # Action that was executed — e.g. "lambda_update_memory"
    outcome: str            # "Success" | "Failed" | "Partial"
    confidence: float       # 1.0 for executed actions, lower for partial/inferred
    nodes_affected: list[str]
    embedding: list[float]  # Titan Embeddings vector (1536 dims) — empty list if not yet embedded
```

**The embedding field** is computed by `lambda-hitl/handler.py` using `amazon.titan-embed-text-v1`. The text vectorized is: `"alarm:{intent} action:{action} outcome:{outcome} nodes:{nodes_affected}"`. This vector is used at query time to retrieve similar past incidents.

---

### Layer 5 — ConstraintTopology

Operational constraints that override governance. Currently used for maintenance windows.

```python
class ConstraintTopology(BaseModel):
    maintenance_windows: list[dict]   # {name, start, end, affected_nodes}
    forbidden_ops: list[str]          # Operation names globally forbidden
```

---

## Complete Digital Twin JSON Example

This is what `sao/digital_twin.json` looks like in S3 for a minimal deployment:

```json
{
  "digital_twin_id": "SAO-CORE-VPC-PROD-001",
  "version": "0.1.0",
  "ontology_standard": "Agentic-IaC-v1",
  "topology": {
    "nodes": [
      {
        "id": "sao-lambda-collector",
        "type": "AWS::Lambda::Function",
        "tfstate_address": "root.aws_lambda_function.collector",
        "properties": {
          "function_name": "sao-lambda-collector",
          "runtime": "python3.12",
          "memory_size": 512,
          "timeout": 300,
          "arn": "arn:aws:lambda:us-east-1:<account-id>:function:sao-lambda-collector"
        },
        "tags": { "Project": "sao-platform" }
      },
      {
        "id": "sg-0abc123456",
        "type": "AWS::EC2::SecurityGroup",
        "tfstate_address": "root.aws_security_group.app",
        "properties": { "id": "sg-0abc123456", "name": "sao-app-sg", "vpc_id": "vpc-0def789" },
        "tags": {}
      }
    ],
    "edges": [
      {
        "head": "sao-lambda-collector",
        "relation": "SECURED_BY",
        "tail": "sg-0abc123456",
        "properties": {}
      }
    ]
  },
  "governance": {
    "frameworks": ["ConstitutionalAI"],
    "denied_actions": [
      {
        "tool": "ecs",
        "action": "stop_service",
        "target": "sao-platform-service",
        "condition": "Never stop the SAO platform itself"
      }
    ],
    "mandatory_tags": {},
    "compliance_scope": []
  },
  "dynamic_state": {
    "active_alarms": [],
    "agent_locks": {},
    "alarm_correlations": [],
    "last_updated": "2026-04-25T18:00:00"
  },
  "precedents": {
    "remediations": [
      {
        "timestamp": "2026-04-24T18:30:00",
        "agent": "sao-hitl-executor",
        "intent": "sao-collector-errors",
        "action": "lambda_update_memory",
        "outcome": "Success",
        "confidence": 1.0,
        "nodes_affected": ["sao-lambda-collector"],
        "embedding": [0.023, -0.041, 0.078]
      }
    ]
  },
  "constraints": {
    "maintenance_windows": [],
    "forbidden_ops": []
  }
}
```

---

## GraphStore — S3 Read/Write

`mcp-server/context_map/store.py`

Thin wrapper around S3 that handles serialization. Uses `DigitalTwin.model_validate()` (Pydantic v2) to deserialize, and `model_dump_json()` to serialize.

```python
store = GraphStore()

# Load — raises if the key does not exist
twin = store.load()

# Load or initialize empty twin (used by app.py at incident time)
twin = store.load_or_empty("SAO-CORE-VPC-PROD-001")

# Save — always updates dynamic_state.last_updated to utcnow()
# Always writes with ServerSideEncryption=aws:kms
store.save(twin)
```

**S3 coordinates** come from `config.py`:
- `GRAPH_BUCKET` → env var `GRAPH_BUCKET`
- `GRAPH_KEY` → env var `GRAPH_KEY` (default: `sao/digital_twin.json`)

---

## GraphQuery — Traversal and RAG

`mcp-server/context_map/query.py`

All reasoning context for Bedrock is built here. The entry point is `context_for_agent()`.

### impact_radius(node_id, depth=2)

BFS traversal over edges (both directions) up to `depth` hops. Returns the list of node IDs that would be affected if `node_id` fails.

```python
query = GraphQuery(twin)
affected = query.impact_radius("sao-lambda-collector", depth=2)
# → ["sg-0abc123456", "subnet-0def456", "vpc-0abc789"]
```

Used in `context_for_agent()` to populate `"impact_radius"` in the Bedrock context.

### similar_precedents(node_type, query_embedding, limit=5)

Retrieves the most relevant past remediations. Two modes:

**Semantic mode** (used when `query_embedding` is provided and precedents have embeddings):
- Computes cosine similarity between `query_embedding` and each precedent's stored embedding
- Returns top-k precedents sorted by similarity score descending
- Each result includes `similarity_score` (0.0–1.0)

**Fallback mode** (no embedding or no embedded precedents):
- Filters precedents by `node_type` — returns only precedents that affected nodes of the same AWS type
- Sorted by timestamp descending (most recent first)

```python
# Called from context_for_agent() — embedding computed in app.py before this
precedents = query.similar_precedents(
    node_type="AWS::Lambda::Function",
    query_embedding=[0.023, -0.041, ...],  # from Titan Embeddings
    limit=5
)
# → [{"intent": "...", "action": "lambda_update_memory", "similarity_score": 0.8484, ...}]
```

### context_for_agent(alarm_name, node_id, query_embedding=None)

The main method. Assembles the full structured context dict that gets serialized and injected into the Bedrock prompt. Called from `app.py` at incident time.

```python
context = query.context_for_agent(
    alarm_name="sao-collector-errors",
    node_id="sao-lambda-collector",
    query_embedding=[...],   # optional — enables semantic RAG
)
```

Returns:

```python
{
    "alarm": "sao-collector-errors",
    "affected_node": {
        "id": "sao-lambda-collector",
        "type": "AWS::Lambda::Function",
        "properties": {...},
        "tags": {...}
    },
    "dependency_graph": [
        # Full node objects for all direct neighbors (1 hop)
    ],
    "impact_radius": ["sg-0abc123", "subnet-0def456"],  # 2-hop BFS
    "causal_hints": [],           # From AlarmCorrelation if pre-loaded
    "governance": {...},          # Full GovernanceLayer dict
    "constraints": {...},         # Full ConstraintTopology dict
    "similar_precedents": [...],  # Top-k from similar_precedents()
    "active_alarms": [],          # From dynamic_state
    "agent_locks": {}             # From dynamic_state
}
```

This dict is serialized to JSON and embedded directly in the Bedrock prompt by `app.py:_build_prompt()`.

---

## RAG Flow — End to End

```
Incident arrives at POST /incident
          │
          ▼
app.py: query_text = f"alarm:{alarm_name} node:{node_id} type:{resource_type}"
          │
          ▼
_compute_embedding(query_text)
  → boto3 bedrock-runtime InvokeModel(amazon.titan-embed-text-v1)
  → returns list[float] of 1536 dimensions
          │
          ▼
GraphQuery.context_for_agent(alarm_name, node_id, query_embedding)
  → similar_precedents() computes cosine similarity against all stored embeddings
  → returns top-5 precedents with similarity_score
          │
          ▼
_build_prompt(event, graph_context, cw_context)
  → "## Infrastructure context (from Digital Twin)" includes similar_precedents
          │
          ▼
Bedrock sees: "Similar past incidents: [lambda_update_memory — Success — score 0.8484]"
  → reasons: "same type of resource, same alarm, same fix worked before"
  → proposes: ACTION: lambda_update_memory function_name=X memory_size=1024
          │
          ▼
Operator approves → lambda-hitl/handler.py executes
          │
          ▼
_register_precedent():
  embed_text = f"alarm:{intent} action:{action} outcome:Success nodes:{nodes}"
  embedding = _compute_embedding(embed_text)  # Titan again
  precedent["embedding"] = embedding
  → append to twin.precedents.remediations
  → s3.put_object(digital_twin.json, SSE-KMS)

Next incident of same type → similarity_score will be even higher
```

**Cosine similarity** is computed in pure Python (no numpy) in `query.py:_cosine()`. It handles zero-magnitude vectors safely (returns 0.0).

---

## Resource Plugin System

`mcp-server/resources/`

Plugins enable the MCP Server (`server.py`) to execute actions on AWS resources via the `sao_execute_action` tool. They are separate from the HITL executor — the Lambda HITL executor (`lambda-hitl/handler.py`) has its own boto3 calls that mirror the plugin actions.

### ResourcePlugin interface (`base.py`)

```python
class ResourcePlugin(ABC):
    def __init__(self, region: str = "us-east-1")

    def get_state(self, resource_id: str) -> dict:
        # Returns current live state from AWS

    def available_actions(self) -> list[str]:
        # Lists actions this plugin can execute

    def execute_action(self, action: str, resource_id: str, params: dict) -> dict:
        # Executes the action and returns result dict

    def risk_level(self, action: str) -> str:
        # Returns "LOW" | "MEDIUM" | "HIGH"
        # Determines whether HITL approval is required before execution
```

### Plugin registry (`__init__.py`)

Maps AWS CloudFormation types to plugin classes:

```python
RESOURCE_REGISTRY: dict[str, type[ResourcePlugin]] = {
    "AWS::Lambda::Function": LambdaPlugin,
    "AWS::ECS::Service":     ECSPlugin,
}

def get_plugin(resource_type: str) -> type[ResourcePlugin] | None:
    return RESOURCE_REGISTRY.get(resource_type)
```

`sao_validate_action` and `sao_execute_action` in `server.py` call `get_plugin(node.type)` to resolve the correct plugin for a given node.

### LambdaPlugin (`lambda_.py`)

| Action | Params | Risk | AWS Call |
|--------|--------|------|----------|
| `update_timeout` | `timeout: int` (seconds) | LOW | `update_function_configuration` |
| `update_memory` | `memory_size: int` (MB) | LOW | `update_function_configuration` |
| `update_concurrency` | `concurrency: int` | MEDIUM | `put_function_concurrency` |

`resource_id` = Lambda function name (not ARN).

### ECSPlugin (`ecs.py`)

| Action | Params | Risk | AWS Call |
|--------|--------|------|----------|
| `scale_desired` | `desired_count: int` | LOW | `update_service` |
| `force_new_deployment` | _(none)_ | MEDIUM | `update_service(forceNewDeployment=True)` |
| `stop_service` | _(none)_ | HIGH | _(not implemented — governance blocks it)_ |

`resource_id` for ECS = `"<cluster>/<service>"` (slash-separated).

### Adding a new plugin

1. Create `mcp-server/resources/newservice.py` implementing `ResourcePlugin`
2. Add the AWS type → class mapping to `RESOURCE_REGISTRY` in `__init__.py`
3. Mirror the actions in `lambda-hitl/handler.py:_execute_action()` — the HITL executor does not use the plugin system, it has its own boto3 calls
4. Add the action names to the Bedrock prompt in `app.py:_build_prompt()`

---

## How app.py Orchestrates Everything

The full incident flow through the context_map and plugins:

```
POST /incident  (AlarmEvent: alarm_name, node_id, resource_type, region)
      │
      ├── GraphStore.load_or_empty()        → DigitalTwin
      ├── twin.is_locked(node_id)           → 409 if locked
      ├── _compute_embedding(query_text)    → list[float] via Titan
      ├── GraphQuery.context_for_agent()    → structured context dict
      ├── _get_cloudwatch_context()         → real-time alarm state + logs
      ├── _build_prompt()                   → full prompt string
      ├── bedrock.invoke_model()            → proposal text
      ├── _extract_risk(proposal)           → "LOW"|"MEDIUM"|"HIGH"
      ├── _parse_action(proposal)           → (action_name, params_dict)
      ├── _save_proposal(token, data)       → S3: proposals/{uuid}.json  status=pending
      └── sns.publish()                     → email with APPROVE/REJECT links

GET /hitl/approve?token=<uuid>  (Lambda HITL executor)
      │
      ├── _load_proposal(token)             → dict from S3
      ├── proposal["status"] != "pending"  → 409 if already processed
      ├── _execute_action(action, params)  → boto3 call
      ├── _save_proposal(token, {..., status="executed"})
      ├── _register_precedent()            → Titan embed + append to Digital Twin
      └── sns.publish()                    → confirmation email
```

---

## Key Design Decisions

**Why the Digital Twin is a single JSON file, not a graph database:**
S3 is the MVP storage. The entire twin is loaded into memory on each incident — acceptable because the twin for a typical deployment is < 1MB. For deployments with > 500 resources, migrating to Amazon Neptune (native multi-hop traversal, no full-load required) is the planned upgrade path.

**Why HITL uses Lambda + API Gateway instead of the MCP Server:**
The HITL executor is a separate Lambda so it can be invoked directly by API Gateway with a simple GET URL (the APPROVE/REJECT links in the email). The MCP Server runs on ECS Fargate behind an ALB — making it handle stateless webhook callbacks from email clients would complicate the architecture unnecessarily.

**Why the plugin system and HITL executor are separate:**
The MCP Server plugins (`resources/`) are for the `sao_execute_action` MCP tool, which requires a running MCP session (Claude reasoning loop). The HITL Lambda executor (`lambda-hitl/handler.py`) is for direct human-triggered execution via a URL click — no Claude session is active at that point. Both must stay in sync when new actions are added.

**Why cosine similarity is computed in pure Python:**
Lambda and ECS environments do not have numpy by default. The Digital Twin is small enough (< 1000 precedents in any realistic deployment) that pure Python cosine similarity over 1536-dim vectors is fast enough. If the precedent count grows into the thousands, switching to `numpy` or a vector store (e.g. OpenSearch Serverless) would be the next step.
