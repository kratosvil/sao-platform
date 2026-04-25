# SAO Platform — Sovereign Agentic Operations

Autonomous AWS incident response platform. A CloudWatch alarm fires, an AI agent reasons over a full infrastructure knowledge graph, proposes an exact fix, and executes it — only after a human approves via email. All AI inference stays inside your VPC via PrivateLink.

> **MVP Status:** End-to-end validated through Phase 8. Production-grade incident flow: detection → Bedrock reasoning → HITL email → boto3 execution → precedent registered in Digital Twin. Fully deployed on AWS.

---

## How It Works

```
CloudWatch Alarm fires
       │
       ▼ EventBridge rule
Lambda Dispatcher
       │ POST /incident
       ▼
MCP Server (ECS Fargate / FastAPI)
       ├── Load Digital Twin from S3
       │     ├── Topology (nodes + edges from Terraform state)
       │     ├── Governance (denied actions, compliance rules)
       │     ├── Precedents (past incidents + embeddings — RAG)
       │     └── Constraints (maintenance windows, forbidden ops)
       │
       ├── Query CloudWatch in real-time
       │     ├── Current alarm state
       │     └── Recent Lambda logs (last 5 min)
       │
       ├── Build semantic query embedding (Titan Embeddings)
       │   Retrieve similar precedents via cosine similarity
       │
       ├── Call Amazon Bedrock (Claude Sonnet — cross-region inference)
       │   → ROOT_CAUSE / FIX / RISK / REASON / ACTION
       │
       ├── Save proposal to S3 (proposals/{token}.json)
       │
       └── Publish SNS email:
             Proposal + APPROVE link + REJECT link

Operator clicks APPROVE
       │ API Gateway GET /hitl/approve?token=<uuid>
       ▼
Lambda HITL Executor
       ├── Load proposal from S3
       ├── Execute boto3 action (Lambda / ECS / RDS)
       ├── Register precedent in Digital Twin
       │     ├── Titan embedding of incident+outcome
       │     └── Written back to Digital Twin S3 JSON
       └── SNS email: confirmation of execution

Total time: alarm → fix executed < 10 minutes
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  PRIVATE VPC  (zero-egress — Bedrock via PrivateLink)           │
│                                                                  │
│  terraform apply → S3 (tfstate)                                 │
│         │                                                        │
│         ▼ S3 event                                               │
│  ┌──────────────────────┐                                       │
│  │  Lambda Collector    │  Reads tfstate → extracts nodes/edges │
│  │                      │  Updates Digital Twin topology in S3  │
│  └──────────┬───────────┘                                       │
│             │ writes sao/digital_twin.json (KMS encrypted)      │
│             ▼                                                    │
│  ┌──────────────────────┐   PrivateLink    ┌──────────────────┐ │
│  │  MCP Server          │ ◄──────────────► │  Amazon Bedrock  │ │
│  │  ECS Fargate/FastAPI │  (no internet)   │  Claude Sonnet   │ │
│  │                      │                  └──────────────────┘ │
│  │  POST /incident      │                  ┌──────────────────┐ │
│  │  GET  /debug/context │ ◄──────────────► │  Titan Embeddings│ │
│  │  POST /debug/prompt  │                  │  (RAG — Phase 8) │ │
│  └──────────┬───────────┘                  └──────────────────┘ │
│             │                                                    │
│             ▼ proposals/{token}.json                            │
│  ┌──────────────────────┐                                       │
│  │  S3 Graph Store      │  Digital Twin + Proposals             │
│  │  (KMS + Versioning)  │  <your-graph-bucket>                 │
│  └──────────────────────┘                                       │
│                                                                  │
│  SNS email → APPROVE/REJECT links → API Gateway                 │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────────────┐                                       │
│  │  Lambda HITL         │  Reads proposal → executes boto3      │
│  │  Executor            │  Registers precedent + embedding      │
│  └──────────────────────┘                                       │
│                                                                  │
│  Every action: CloudTrail → S3 WORM + KMS (immutable audit)    │
└─────────────────────────────────────────────────────────────────┘
```

---

## The Digital Twin — Core Innovation

Not a list of resources. A **living knowledge graph** with 5 layers that enables zero-hallucination AI reasoning:

| Layer | Contents | Source | Updated |
|-------|----------|--------|---------|
| **Topology** | Nodes (resources) + edges (dependencies) | `terraform.tfstate` | Every `terraform apply` |
| **Governance** | Denied actions, compliance frameworks, mandatory tags | Static config | Manual |
| **Dynamic State** | Active alarms, CloudWatch metrics, agent locks | CloudWatch (real-time) | At incident time |
| **Precedents** | History of every remediation + outcome + Titan embedding | Lambda HITL (post-execution) | After each approved fix |
| **Constraints** | Maintenance windows, forbidden ops by schedule | Static config | Manual |

**Why this matters:** when Bedrock proposes a fix, it sees the exact network topology, knows which actions are governance-blocked, and retrieves semantically similar past incidents via RAG. Impossible or dangerous proposals are structurally prevented, not prompt-engineered away.

---

## Semantic RAG on Precedents (Phase 8)

After each approved and executed fix, the Lambda HITL Executor registers a precedent with a vector embedding:

```
incident query → Titan Embeddings (amazon.titan-embed-text-v1, 1536 dims)
                       ↓
              cosine similarity against all stored precedents
                       ↓
              top-k most similar past incidents injected into Bedrock context
```

Validated: `similarity_score=0.8484` on same-type incident replay. The system gets smarter with every resolved incident without retraining.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| IaC | Terraform >= 1.5, S3 remote backend |
| Graph Store | S3 — JSON-LD, KMS encryption + versioning |
| AI Reasoning | Amazon Bedrock — `us.anthropic.claude-sonnet-4-6` (cross-region inference) |
| RAG | Amazon Titan Embeddings v1 (1536 dims) + cosine similarity (Python) |
| AI Transport | VPC Interface Endpoint — Bedrock never touches internet |
| Agent Compute | ECS Fargate — serverless containers, scales to zero |
| HTTP Framework | FastAPI — async incident handler |
| HITL Gateway | SNS email + API Gateway + Lambda executor |
| Topology Source | `terraform.tfstate` auto-parsed on every apply |
| Event Trigger | CloudWatch Alarms + EventBridge |
| Audit | CloudTrail — S3 WORM + KMS |
| IAM | Least-privilege — no IAM write, no billing, no root |

---

## Repository Structure

```
sao-platform/
├── mcp-server/
│   ├── app.py                     # FastAPI HTTP server — incident handler, Bedrock, HITL flow
│   ├── server.py                  # MCP server — 4 tools (sao_load_context, etc.)
│   ├── config.py                  # Environment-based config
│   ├── context_map/
│   │   ├── schema.py              # DigitalTwin + all layer models (Pydantic)
│   │   ├── store.py               # S3 read/write for the graph
│   └── └── query.py               # Topology traversal + semantic precedent retrieval
│   └── resources/
│       ├── base.py                # ResourcePlugin interface
│       ├── lambda_.py             # LambdaPlugin — timeout / memory / concurrency
│       └── ecs.py                 # ECSPlugin — scale / force-deploy
├── lambda-collector/
│   ├── handler.py                 # Lambda entry point — fires on S3 event (new tfstate)
│   └── collectors/
│       ├── tfstate.py             # Parses tfstate → nodes + edges
│       └── cloudwatch.py         # Fetches metrics + active alarms
├── lambda-hitl/
│   └── handler.py                 # HITL executor — approve/reject, boto3, precedent registration
├── terraform/
│   ├── versions.tf                # Provider + remote backend
│   ├── variables.tf               # All inputs (no hardcoded values)
│   ├── main.tf                    # S3 + Lambda Collector + EventBridge
│   ├── ecs.tf                     # ECS Fargate cluster + task definition + ALB
│   ├── hitl.tf                    # API Gateway + Lambda HITL
│   ├── iam.tf                     # IAM roles + least-privilege policies
│   ├── networking.tf              # VPC + subnets + security groups
│   ├── vpc_endpoints.tf           # 8 VPC Interface endpoints (Bedrock, ECR, S3, etc.)
│   ├── alarms.tf                  # CloudWatch alarms + EventBridge rules
│   ├── ecr.tf                     # ECR repository
│   ├── outputs.tf
│   ├── backend.tfbackend.example  # Copy → backend.tfbackend (gitignored)
│   └── terraform.tfvars.example   # Copy → terraform.tfvars (gitignored)
└── docs/
    ├── digital_twin_schema.json   # Full Digital Twin schema reference
    └── extending-digital-twin.md  # Guide: adding new AWS resource types
```

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `sao_load_context` | Loads full Digital Twin context for an incident node |
| `sao_validate_action` | Checks governance + node lock before executing any action |
| `sao_execute_action` | Executes approved action via resource plugin, writes precedent |
| `sao_graph_status` | Current Digital Twin summary (nodes, edges, locks, precedent count) |

---

## HTTP Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/incident` | Main incident handler — full Bedrock + HITL flow |
| `GET` | `/debug/context/{node_id}` | Digital Twin context for a node (no Bedrock call) |
| `POST` | `/debug/prompt` | Full prompt that would be sent to Bedrock (no Bedrock call) |

---

## HITL Actions

All actions executed by the Lambda HITL Executor after operator approval:

| Action | Parameters | AWS Call |
|--------|------------|----------|
| `lambda_update_timeout` | `function_name`, `timeout` | `update_function_configuration` |
| `lambda_update_memory` | `function_name`, `memory_size` | `update_function_configuration` |
| `lambda_update_reserved_concurrency` | `function_name`, `reserved_concurrent_executions` | `put_function_concurrency` |
| `ecs_restart_service` | `cluster`, `service` | `update_service(forceNewDeployment=True)` |
| `ecs_update_desired_count` | `cluster`, `service`, `desired_count` | `update_service` |
| `rds_reboot_instance` | `db_instance_identifier` | `reboot_db_instance` |
| `none` | `reason` | No action — log only |

---

## Risk Policy

| Risk Level | Examples | Approval |
|------------|----------|---------|
| `LOW` | Lambda timeout/memory update | Auto-approved |
| `MEDIUM` | ECS force-new-deployment, Lambda concurrency | On-call engineer |
| `HIGH` | RDS operations, service stop | Manager approval |

---

## Security

```
Layer 1 — Network:      Zero-egress VPC, Bedrock + ECR via PrivateLink — no NAT, no IGW
Layer 2 — IAM:          Least privilege — no IAM write, no billing, no root access
Layer 3 — Governance:   Denied actions in Digital Twin — agent cannot override policy
Layer 4 — HITL:         MEDIUM + HIGH risk → human approval required before execution
Layer 5 — Audit:        CloudTrail WORM — immutable, every action tied to proposal token
Layer 6 — Agent Locks:  Digital Twin locks node during execution — no concurrent agents
Layer 7 — Secrets:      Credentials via SSM Parameter Store — never in code or tfvars
Layer 8 — Idempotency:  Proposals have status (pending/executed/rejected/failed) — one-time execution
```

---

## AWS Resources (Deployed MVP)

| Resource | Name |
|----------|------|
| S3 (graph + proposals) | `<account-id>-sao-graph-<account-id>` (set in `terraform.tfvars`) |
| Lambda Collector | `sao-lambda-collector` |
| Lambda Dispatcher | `sao-alarm-dispatcher` |
| Lambda HITL | `sao-lambda-hitl` |
| API Gateway | `https://<api-id>.execute-api.<region>.amazonaws.com` |
| EventBridge Rule | `sao-cw-alarm-trigger` |
| ECS Cluster | `sao-platform-cluster` |
| ECS Service | `sao-platform-service` |
| ALB | `sao-platform-alb-<id>.<region>.elb.amazonaws.com` |
| ECR | `<account-id>.dkr.ecr.<region>.amazonaws.com/sao-mcp-server` |
| SNS Topic | `sao-platform-alarms` (KMS encrypted) |
| VPC Endpoints | 8 Interface endpoints + S3 Gateway |

---

## Estimated Cost per Incident

| Component | Tokens | Cost |
|-----------|--------|------|
| Digital Twin context (static, prompt cache eligible) | ~22,000 | ~$0.007 |
| Dynamic state + CloudWatch context | ~11,000 | ~$0.033 |
| Claude Sonnet response | ~3,500 | ~$0.053 |
| **Total per incident** | **~36,500** | **~$0.093** |

Infrastructure (ECS Fargate + VPC Endpoints): ~$0.19/hr — **tear down when not in demo**.

50 incidents/month ≈ **$4.65 in AI tokens**.

---

## Development Operations

```bash
# Build and deploy MCP Server image
make docker-deploy

# Rebuild Lambda Collector ZIP
make build-collector

# Fix ECS service pointing to wrong task definition (run after terraform apply)
make fix-taskdef

# Trigger a test alarm (OK → ALARM)
make run_script

# View proposals in S3
make list-proposals
make show-proposal TOKEN=<uuid>

# View logs
make logs-dispatcher
make logs-mcp

# Validate RAG mode and precedents
make debug-rag
```

---

## Deploying from Scratch

### Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | >= 3.12 | For local dev and Lambda packaging |
| Terraform | >= 1.5 | Remote backend required (S3 + DynamoDB) |
| AWS CLI v2 | latest | Configured with credentials for the target account |
| Docker | latest | For building and pushing the MCP Server image |
| Amazon Bedrock | — | Must request access to: Claude Sonnet (`us.anthropic.claude-sonnet-4-6`) and Titan Embeddings (`amazon.titan-embed-text-v1`) in us-east-1 |

> Bedrock model access can take up to 24h. Request it before starting the deploy.

### Step 1 — Configure

```bash
git clone https://github.com/kratosvil/sao-platform.git
cd sao-platform

cp terraform/backend.tfbackend.example terraform/backend.tfbackend
cp terraform/terraform.tfvars.example  terraform/terraform.tfvars
```

Edit `terraform/backend.tfbackend` — set your S3 bucket, DynamoDB table, and region for Terraform state storage.

Edit `terraform/terraform.tfvars` — all values are documented inline. The critical ones:

| Variable | What to set |
|----------|-------------|
| `graph_bucket_name` | New unique S3 bucket name (Terraform creates it) |
| `tfstate_bucket_name` | Existing S3 bucket where your tfstate lives |
| `operator_email` | Email that receives APPROVE/REJECT HITL links |
| `bedrock_model_id` | Must be `us.anthropic.claude-sonnet-4-6` — see note below |
| `tfstate_kms_key_arn` | Only if your tfstate bucket uses SSE-KMS |

### Step 2 — Deploy infrastructure

```bash
cd terraform
terraform init -backend-config=backend.tfbackend
terraform plan -var-file=terraform.tfvars   # review before applying
terraform apply -var-file=terraform.tfvars
cd ..
```

After `terraform apply`, check the outputs — you will need the ALB DNS name and API Gateway URL.

### Step 3 — Confirm SNS email subscription

AWS sends a confirmation email to `operator_email` after the SNS topic is created.
**You must click "Confirm subscription" in that email before HITL works.**
Without this step, no APPROVE/REJECT notifications will be delivered.

### Step 4 — Build and push the MCP Server image

```bash
make docker-deploy
```

This builds the Docker image, pushes it to ECR, and forces a new ECS deployment.

### Step 5 — Point ECS to the correct task definition

```bash
make fix-taskdef
```

The ECS module has `ignore_changes = [task_definition]` — the service starts pointing to the first revision (which has no env vars). This command updates it to the latest revision with all env vars injected. **This step is mandatory after every `terraform apply` that changes env vars.**

### Step 6 — Build and deploy the Lambda Collector

```bash
make build-collector

aws lambda update-function-code \
  --function-name sao-lambda-collector \
  --zip-file fileb://lambda-collector/collector.zip \
  --region us-east-1
```

### Step 7 — Enable EventBridge notifications on the tfstate bucket

The Lambda Collector fires on S3 events from the tfstate bucket. This requires EventBridge notifications to be enabled on that bucket (it is not enabled by default):

```bash
aws s3api put-bucket-notification-configuration \
  --bucket <your-tfstate-bucket> \
  --notification-configuration '{"EventBridgeConfiguration":{}}'
```

This is a non-destructive operation — it does not touch any existing Lambda/SNS/SQS notifications.

### Step 8 — Build the initial Digital Twin

Trigger the collector manually to create the Digital Twin JSON before the first incident:

```bash
aws lambda invoke \
  --function-name sao-lambda-collector \
  --payload '{"source":"manual"}' \
  --region us-east-1 \
  /tmp/out.json && cat /tmp/out.json
```

Expected output: `{"statusCode": 200, "body": {"nodes_updated": N, "edges_updated": M, ...}}`

### Step 9 — Verify and test

```bash
# Verify the Digital Twin was built
make debug-rag

# Verify the MCP Server is healthy
curl http://<your-alb-dns>/health

# Run a full end-to-end test (triggers a demo alarm)
make run_script
```

---

## Operations Runbook

### After every `terraform apply`

```bash
# Always run this — ECS module ignores task_definition changes
make fix-taskdef

# If Lambda Collector code changed
make build-collector
aws lambda update-function-code \
  --function-name sao-lambda-collector \
  --zip-file fileb://lambda-collector/collector.zip \
  --region us-east-1
```

### After a HITL-approved fix executes

The HITL executor changes AWS resources directly via boto3 (e.g., Lambda memory size). This creates **IaC drift** — Terraform does not know about the change. To fix:

1. Check what changed in the executed proposal: `make show-proposal TOKEN=<uuid>`
2. Update the corresponding value in `terraform.tfvars` and the resource block in `main.tf`
3. Run `terraform plan` — it should show zero changes if the values match
4. Commit the updated tfvars and tf files to keep IaC in sync

### Demo cycle (cost control)

Infrastructure costs ~$0.19/hr (~$137/month) dominated by VPC Interface Endpoints. Tear down between demos:

```bash
# Tear down (keeps S3 data — Digital Twin and proposals are preserved)
cd terraform && terraform destroy -var-file=terraform.tfvars

# Restore for next demo (full redeploy — ~15 min)
terraform apply -var-file=terraform.tfvars
make docker-deploy
make fix-taskdef
```

The Digital Twin JSON in S3 survives `terraform destroy` because the S3 bucket has versioning enabled and Terraform does not empty it on destroy by default.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Bedrock `ValidationException: on-demand throughput not supported` | Wrong model ID | Set `bedrock_model_id = "us.anthropic.claude-sonnet-4-6"` in tfvars |
| HITL email never arrives | SNS subscription not confirmed | Check email inbox for AWS confirmation email and click the link |
| HITL links in email show `N/A` | ECS task using old task definition (no env vars) | Run `make fix-taskdef` |
| ECS task exits immediately | Missing env vars in task definition | Run `make fix-taskdef`, then check `make logs-mcp` |
| Lambda Collector returns `NoSuchKey` on tfstate | Wrong `tfstate_key` in tfvars | Verify the exact S3 key with `aws s3 ls s3://<bucket> --recursive` |
| Digital Twin has 0 nodes | tfstate bucket EventBridge notifications not enabled | Run Step 7 above |
| `make docker-deploy` fails on ECR login | AWS credentials expired or wrong region | Run `aws sts get-caller-identity` to verify credentials |
| Proposals not appearing in S3 | MCP Server cannot write to graph bucket | Check IAM policy `sao-mcp-server-policy` — `s3:PutObject` on bucket |

---

## Target Use Cases

| Industry | Compliance | Value |
|----------|------------|-------|
| Fintech | SOC2 / PCI-DSS | Incident response — data never leaves the regulated perimeter |
| Healthtech | HIPAA | AI-assisted ops where PHI workloads cannot use public AI endpoints |
| Government | FedRAMP | Sovereign AI operations inside isolated cloud enclaves |
| SaaS B2B | SOC2 | Reduce MTTR without manual on-call toil |

---

## Relation to aws-sovereign-ops

[aws-sovereign-ops](https://github.com/kratosvil/aws-sovereign-ops) is the v1 proof-of-concept that validated the Lambda remediation flow (4/4 e2e scenarios passed). SAO Platform is the full architectural evolution: the Digital Twin Context Map replaces manual context injection and enables structured, auditable, zero-hallucination reasoning at scale.

---

## License

[Business Source License 1.1](LICENSE)

Free for internal and non-commercial use.  
Commercial use requires a license — contact: kratosvill@gmail.com  
Converts to Apache License 2.0 on 2030-01-01.
