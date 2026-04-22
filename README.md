# SAO Platform — Sovereign Agentic Operations

Zero-egress AI platform that autonomously resolves AWS infrastructure incidents — powered by a living Knowledge Graph built from your Terraform state.

> An AI agent reads your entire infrastructure graph, reasons with full context, proposes an exact fix, and executes it — only after a human approves via Slack. Data never leaves your VPC.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  PRIVATE VPC  (zero-egress — Bedrock via PrivateLink)                │
│                                                                       │
│  CloudWatch Alarm                                                     │
│         │                                                             │
│         ▼ EventBridge                                                 │
│  ┌──────────────────────┐                                            │
│  │  Lambda Collector    │ ← fires on S3 event (every terraform apply) │
│  │                      │   updates topology in the Digital Twin      │
│  │  Builds / Updates    │                                             │
│  │  Digital Twin        │                                             │
│  └──────────┬───────────┘                                            │
│             │ writes JSON-LD                                          │
│             ▼                                                         │
│  ┌──────────────────────┐     PrivateLink      ┌──────────────────┐  │
│  │  MCP Server          │ ◄──────────────────► │  Amazon Bedrock  │  │
│  │  (ECS Fargate)       │   (never touches      │  Claude Sonnet   │  │
│  │                      │    internet)          └──────────────────┘  │
│  │  sao_load_context    │                                             │
│  │  sao_validate_action │                                             │
│  │  sao_execute_action  │ ← Human APPROVE token (Slack button)       │
│  │  sao_graph_status    │                                             │
│  └──────────┬───────────┘                                            │
│             │ boto3 / kubectl / terraform                             │
│             ▼                                                         │
│  Lambda / ECS / RDS / EKS ──► CloudTrail (S3 WORM + KMS)            │
│                                immutable audit log                   │
└──────────────────────────────────────────────────────────────────────┘
                       │ Slack HITL Gateway
                       ▼
            Operator receives:
            • Alarm + affected resource (from graph)
            • Root cause (multi-hop graph traversal)
            • Proposed fix with exact parameters
            • Risk level: LOW / MEDIUM / HIGH
            • Similar past incidents + outcomes
            Decides: APPROVE / REJECT
```

---

## How It Works — Full Incident Flow

```
1. terraform apply runs → tfstate uploaded to S3
        ↓
2. S3 event → Lambda Collector fires
   Reads tfstate → extracts nodes + edges (topology)
   Updates Digital Twin JSON in S3 (topology only)
        ↓
3. CloudWatch alarm triggers → EventBridge → MCP Server
        ↓
4. MCP Server loads Digital Twin from S3:
   - Topology: what resources exist and how they connect
   - Governance: what actions are forbidden
   - Dynamic state: active alarms, metrics, agent locks
   - Precedents: similar incidents resolved before (with outcomes)
   - Constraints: maintenance windows, forbidden ops
        ↓
5. MCP Server calls Bedrock via PrivateLink (zero-egress)
   Claude reasons with full structured graph context:
   - Multi-hop traversal to find root cause
   - Validates proposed fix against governance layer
   - Checks precedents for similar incidents
   - Assigns risk level
        ↓
6. HITL Gateway sends Slack message with APPROVE / REJECT buttons
        ↓
7. Operator approves → MCP Server executes via resource plugin:
   - LambdaPlugin: update timeout / memory / concurrency
   - ECSPlugin: scale desired / force-new-deployment
   - (RDSPlugin, EC2Plugin, EKSPlugin — Fase 4)
   Every action recorded in CloudTrail + precedents layer
        ↓
8. Digital Twin updated with new precedent (confidence score)
   System gets smarter with each resolved incident

   Total time: detection → fix executed < 10 minutes
   Without SAO: 30–90 minutes with on-call engineer
```

---

## The Digital Twin Context Map

The core innovation. Not a list of resources — a **living Knowledge Graph** with 5 layers:

| Layer | What it contains | Source |
|-------|-----------------|--------|
| **Topology** | Nodes (resources) + edges (dependencies) | Parsed from `terraform.tfstate` |
| **Governance** | Denied actions, compliance frameworks, mandatory tags | Static config (SOC2 / HIPAA rules) |
| **Dynamic State** | Active alarms, CloudWatch metrics, agent locks | MCP Server queries CloudWatch in real-time at incident time |
| **Precedents** | History of every remediation + outcome + confidence | Written by MCP Server after each action |
| **Constraints** | Maintenance windows, forbidden ops by schedule | Static config |

**Why this eliminates hallucination:** the agent validates every hypothesis against the graph structure.
If Claude thinks the issue is the database, the graph immediately shows the RDS instance is in an isolated subnet with a specific SG — no impossible network commands get proposed.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| IaC | Terraform >= 1.5 — modular, S3 remote backend |
| Graph Store (MVP) | S3 — JSON-LD with KMS encryption + versioning |
| Graph Store (Prod) | Amazon Neptune — native multi-hop traversal |
| Topology Source | `terraform.tfstate` — auto-updated on every apply |
| Agent Compute | ECS Fargate — no nodes, scales to zero |
| AI Brain | Amazon Bedrock (Claude Sonnet) |
| AI Transport | VPC Interface Endpoint — Bedrock never touches internet |
| Agent Framework | MCP (Model Context Protocol) — FastMCP / Python |
| Context Collector | AWS Lambda — parses tfstate + CloudWatch |
| Event Trigger | CloudWatch Alarms + EventBridge |
| HITL Gateway | Slack (inline approve/reject buttons) |
| Audit | CloudTrail — S3 WORM bucket + KMS |
| IAM | Least-privilege — no IAM write, no billing, no root |

---

## Repository Structure

```
sao-platform/
├── mcp-server/
│   ├── server.py                  # MCP Server — 4 tools
│   ├── config.py                  # Environment-based config
│   ├── context_map/
│   │   ├── schema.py              # DigitalTwin + all layer models (Pydantic)
│   │   ├── store.py               # S3 read/write for the graph
│   │   └── query.py               # Multi-hop traversal + context builder
│   └── resources/
│       ├── base.py                # ResourcePlugin interface
│       ├── lambda_.py             # LambdaPlugin — timeout / memory / concurrency
│       └── ecs.py                 # ECSPlugin — scale / force-deploy
├── lambda-collector/
│   ├── handler.py                 # Lambda entry point — triggers on S3 event (new tfstate)
│   └── collectors/
│       ├── tfstate.py             # Parses tfstate → nodes + edges
│       └── cloudwatch.py         # Fetches metrics + active alarms
├── terraform/
│   ├── versions.tf                # Provider + backend (via -backend-config)
│   ├── variables.tf               # All inputs (no hardcoded values)
│   ├── main.tf                    # S3 graph store + Lambda Collector + EventBridge
│   ├── outputs.tf
│   ├── backend.tfbackend.example  # Copy → backend.tfbackend (gitignored)
│   └── terraform.tfvars.example   # Copy → terraform.tfvars (gitignored)
└── docs/
    └── digital_twin_schema.json   # Full Digital Twin schema — Fase 1
```

---

## MCP Server Tools

| Tool | Description | Requires HITL |
|------|-------------|---------------|
| `sao_load_context` | Loads full Digital Twin context for an incident | No |
| `sao_validate_action` | Checks governance before executing any action | No |
| `sao_execute_action` | Executes approved action via resource plugin | Yes (MEDIUM / HIGH risk) |
| `sao_graph_status` | Current Digital Twin summary | No |

---

## Resource Plugins

Adding support for a new AWS service = one new file in `mcp-server/resources/`.

| Plugin | Actions | Risk |
|--------|---------|------|
| `LambdaPlugin` | update_timeout, update_memory, update_concurrency | LOW / MEDIUM |
| `ECSPlugin` | scale_desired, force_new_deployment, stop_service | LOW / MEDIUM / HIGH |
| `RDSPlugin` *(Fase 4)* | resize, parameter_group, snapshot_restore | HIGH |
| `EC2Plugin` *(Fase 4)* | reboot, resize, AMI rollback | HIGH |
| `EKSPlugin` *(Fase 4)* | scale_deployment, restart_pod, cordon_node | MEDIUM / HIGH |

---

## Security Layers

```
Layer 1 — Network:      Zero-egress VPC, Bedrock via PrivateLink — no NAT, no IGW
Layer 2 — IAM:          Least privilege — no IAM write, no billing, no root access
Layer 3 — Governance:   Denied actions in graph — agent cannot override policy
Layer 4 — HITL:         MEDIUM + HIGH risk → human approval required before execution
Layer 5 — Audit:        CloudTrail WORM — immutable, every action tied to task_id
Layer 6 — Agent Locks:  Graph locks node during execution — no concurrent agents on same resource
Layer 7 — Secrets:      All credentials via SSM Parameter Store — never in code or tfvars
```

---

## Risk Policy — HITL Routing

| Risk Level | Examples | Approval |
|------------|---------|---------|
| `LOW` | Lambda timeout/memory update, ECS scale up | Auto-approved |
| `MEDIUM` | ECS force-new-deployment, Lambda concurrency | On-call engineer |
| `HIGH` | RDS resize, EKS cordon, stop_service | Manager approval |

---

## Estimated Cost per Incident (Claude Sonnet on Bedrock)

| Component | Tokens | Cost |
|-----------|--------|------|
| Digital Twin context (static, cached) | ~22,000 | ~$0.007 (cache read) |
| Dynamic state + alarm context | ~11,000 | ~$0.033 |
| Claude response (proposed fix) | ~3,500 | ~$0.053 |
| **Total per incident** | **~36,500** | **~$0.093** |

50 incidents/month ≈ **$4.65 in AI tokens**.

---

## Roadmap

| Phase | Description | Status |
|-------|-------------|--------|
| Fase 0 | Scaffold — MCP Server, Lambda Collector, Terraform skeleton, Digital Twin schema | **Complete** |
| Fase 1 | IAM roles + S3 graph store deploy + Lambda Collector live | Pending |
| Fase 2 | MCP Server on ECS + Bedrock PrivateLink integration | Pending |
| Fase 3 | HITL Gateway — Slack approve/reject buttons | Pending |
| Fase 4 | Additional plugins: RDS, EC2, EKS | Pending |
| Fase 5 | RAG over precedents + Neptune migration | Pending |
| Fase 6 | Multi-tenant + dashboard + SaaS model | Pending |

---

## Relation to aws-sovereign-ops

[aws-sovereign-ops](https://github.com/kratosvil/aws-sovereign-ops) is the v1 demo that validated the concept (4/4 Lambda e2e scenarios passed). SAO Platform is the architectural evolution — the Digital Twin Context Map replaces manual context collection and enables zero-hallucination reasoning at scale.

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | >= 3.12 |
| Terraform | >= 1.5 |
| AWS CLI v2 | latest |
| Amazon Bedrock | Claude Sonnet model access enabled |

---

## Quick Start

```bash
git clone https://github.com/kratosvil/sao-platform.git
cd sao-platform

# Install dependencies
make install

# Configure backend (copy and fill in your values)
cp terraform/backend.tfbackend.example terraform/backend.tfbackend
cp terraform/terraform.tfvars.example terraform/terraform.tfvars

# Set required environment variables
export TFSTATE_BUCKET=your-tfstate-bucket
export GRAPH_BUCKET=your-graph-bucket
export AWS_REGION=us-east-1

# Deploy infrastructure (Fase 1+)
cd terraform
terraform init -backend-config=backend.tfbackend
terraform apply -var-file=terraform.tfvars

# Start MCP Server locally
make run-mcp
```

---

## Target Use Cases

| Industry | Compliance | Use Case |
|----------|------------|---------|
| Fintech | SOC2 / PCI-DSS | Autonomous incident response — data never leaves regulated perimeter |
| Healthtech | HIPAA | AI-assisted ops where PHI workloads cannot use public AI endpoints |
| Government | FedRAMP | Sovereign AI operations inside isolated cloud enclaves |
| SaaS B2B | SOC2 | Platform reliability — reduce MTTR without manual on-call toil |

---

## License

[Business Source License 1.1](LICENSE)

Free for internal and non-commercial use.
Commercial use requires a license — contact: kratosvill@gmail.com

After 2030-01-01 this project converts to Apache License 2.0.
