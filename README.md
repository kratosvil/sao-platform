# SAO Platform — Sovereign Agentic Operations

Autonomous AWS/Kubernetes incident response. A real alarm fires (CloudWatch or Prometheus), an AI agent reasons over a full infrastructure knowledge graph, proposes an exact fix, and **applies it via a Git commit — never a direct write to AWS**. All AI inference stays inside a zero-egress VPC via PrivateLink.

![SAO Platform — Sovereign Agentic Operations](images/banner.png)

> **Status:** core reasoning + GitOps remediation loop verified end-to-end against live infrastructure, including Bedrock reasoning in production (not simulated). This repo is the reasoning/execution half of **[SAGA](https://github.com/kratosvil/argocd-gitops-aws)** — the other half is the EKS/ArgoCD cluster it remediates. Full module-by-module status: [`estado.md`](https://github.com/kratosvil/contexts-repo) (private) / architecture map in `infra-map.md`.

---

## How It Works

```
Prometheus alert fires (e.g. SagaPodCrashLooping)
       │ Alertmanager webhook
       ▼
Lambda dispatcher (outside any VPC)
       │ POST /incident
       ▼
mcp_server (ECS Fargate / FastAPI, INSIDE a zero-egress VPC)
       ├── Load Digital Twin from S3 (topology, governance, precedents, constraints)
       ├── Query CloudWatch in real time
       ├── If the alarm is a known one: ask the HITL Lambda for the real git
       │   history of the manifest that controls the deployment (mcp_server
       │   cannot reach GitHub itself — see "Zero-egress" below)
       ├── Call Amazon Bedrock via PrivateLink (Claude Sonnet 4.6)
       │   → ROOT_CAUSE / FIX / RISK / REASON / ACTION
       ├── Capture real token usage from the response, compute cost
       ├── Decide decision_state BY CODE, never by the model's self-reported
       │   RISK — only argocd_rollback_via_git on the dev overlay qualifies
       │   for auto_execute; everything else escalates to a human
       └── Save the proposal to S3, notify (email + console)

If auto_execute: mcp_server invokes the HITL Lambda itself (no human click)
If escalate: a human approves from the email link OR the console
       │
       ▼
Lambda HITL (outside the VPC — this is the only component with real
internet egress; it's the GitHub proxy for the isolated reasoner)
       ├── Opens a Pull Request in the private GitOps manifests repo
       │   (NEVER writes to `main` directly)
       └── Serves a minimal review console (/hitl/pending, /hitl/review/{token})
           where a human can approve as-is or adjust a parameter first

The PR must pass CI (dry-run + OPA policy + Trivy + Gitleaks) before it can
merge. If auto_execute, a poller Lambda merges it automatically once CI is
green — otherwise a human merges by hand.

ArgoCD syncs the merge → cluster returns to healthy.

The poller then confirms against real Prometheus data (not "it merged") that
the alert actually cleared, and only then opens a NEW pull request with an
auto-generated guardrail policy that prevents the same failure from
recurring — this one ALWAYS requires human approval, no exceptions.
```

---

## Why GitOps, not direct AWS writes

The original MVP of this project executed approved fixes directly via boto3 (`lambda:UpdateFunctionConfiguration`, `ecs:UpdateService`, `rds:RebootDBInstance` — the code for these is still in `lambda-hitl/handler.py`, kept intentionally as evidence). That model has a real weakness: it has no audit trail beyond CloudTrail, no policy gate on the change's *content*, and no natural place to leave a guardrail behind.

This repo now fuses with **[SAGA](https://github.com/kratosvil/argocd-gitops-aws)**: the agent's only write path is a Pull Request against a GitOps-managed manifests repo. Every fix is reviewable diff, gated by CI (OPA/Trivy/Gitleaks) before it can merge, and ArgoCD — not the agent — is what actually touches the cluster. The IAM role that reasons about the incident has **zero AWS write permissions**, verified with a real negative test (`AccessDeniedException` captured on a direct write attempt).

---

## Zero-egress and the GitHub-proxy pattern

`mcp_server` runs in a VPC with `enable_nat_gateway = false` — no NAT, no route to the public internet, only VPC Interface Endpoints to Bedrock, ECR, CloudWatch, STS, Lambda, and an S3 Gateway endpoint. This is a real constraint, not just a diagram: when the reasoner needed the real git history of a manifest to ground its proposal (see "The context bug" below), it could not call GitHub or Secrets Manager directly — both attempts hung/failed.

The fix reuses a pattern already established for auto-approval: `mcp_server` invokes the **HITL Lambda** (which does have internet egress — it is not inside any VPC) via `lambda:InvokeFunction`, using a direct-invoke event shape distinct from its API Gateway–triggered requests. The HITL Lambda already holds the GitHub PAT and already talks to GitHub to open PRs, so it doubles as a read-only proxy for the isolated reasoner. No new secret, no new IAM grant beyond a permission that already existed.

---

## The context bug (real finding, not hypothetical)

The first time this system reasoned live about a real Kubernetes incident (every earlier test had the decision hand-simulated to avoid paying for Fargate on every run), Bedrock returned `ACTION: none` — not a bug in the model, a real gap in what it was given: CloudWatch had nothing (the alert is Prometheus-native, not a CloudWatch Alarm), the log-group lookup targeted a Lambda naming convention against a Kubernetes pod, and there were zero RAG precedents for this exact scenario yet. Without any of the three, the model correctly declined to guess a target git SHA.

Fixed by adding a fourth context source: the real commit history of the manifest path, with an explicit instruction to pick the most recent revision whose tag differs from the currently-deployed one. Verified end to end with a real incident: Bedrock proposed the exact correct SHA, the fix auto-merged, the cluster came back healthy.

---

## The Digital Twin

Not a list of resources. A **living knowledge graph** with 5 layers:

| Layer | Contents | Source | Updated |
|-------|----------|--------|---------|
| **Topology** | Nodes (resources) + edges (dependencies) | `terraform.tfstate` | Every `terraform apply` |
| **Governance** | Denied actions, compliance frameworks, mandatory tags | Static config | Manual |
| **Dynamic State** | Active alarms, CloudWatch metrics, agent locks | CloudWatch (real-time) | At incident time |
| **Precedents** | History of every remediation + outcome + Titan embedding | HITL Lambda (post-execution) | After each resolved fix |
| **Constraints** | Maintenance windows, forbidden ops by schedule | Static config | Manual |

Precedents now distinguish what the AI proposed from what a human actually approved when a reviewer adjusted a parameter before approving (see "Review console" below) — the RAG layer learns from the correction, not just the raw suggestion.

---

## Review console — approve, reject, or adjust before approving

Beyond the original single-use email links (`/hitl/approve`, `/hitl/reject` — still work exactly as before), a small console runs on the same Lambda/API Gateway/S3, with no external tool or paid platform:

| Route | What it does |
|---|---|
| `GET /hitl/pending` | Lists proposals waiting on a human, with the real cost of each |
| `GET /hitl/review/{token}` | Full Bedrock reasoning + an editable form of the proposed action's parameters |
| `POST /hitl/review/{token}` | Approves with the original values or with a human-adjusted value (e.g. overriding which git revision to revert to), or rejects |

Guarded by a bearer token (SSM SecureString, generated by Terraform — never hardcoded, never printed). The single-use email links don't need this (secure by possession); a route that lists *every* pending proposal in one URL does.

---

## Cost — captured per decision, not estimated

Every Bedrock call's real `usage.input_tokens`/`output_tokens` is captured from the response (previously discarded unread) and priced against Claude Sonnet 4.6's on-demand Bedrock rate (**$3 / $15 per million input/output tokens**, verified 2026-08-13). The result is stored on the proposal and shown in the console — a per-incident real number, not a projected estimate.

This is intentionally the *minimal* slice of cost governance: no cross-incident ledger, no dedup, no budget-enforcement gate yet — those are a documented next phase (dedup/rate-limiting is already a known gap: an alert storm can currently trigger the same proposal multiple times from unrelated alarms; a quick mitigation — scoping which alarms get grounding context — is in place, the structural fix is not).

---

## Decision gate — three states, decided by code

| `decision_state` | When | What happens |
|---|---|---|
| `auto_execute` | Action is `argocd_rollback_via_git` targeting the dev overlay specifically | PR opens and merges with zero human interaction, provided CI passes |
| `escalate` | Anything else — prod, base manifests, any other action, or the model returning `none` | Waits for a human, via email or the console |
| *(guardrails)* | Always, no exception | The eradication-phase policy PR generated after a confirmed fix never auto-merges, regardless of the risk of the fix it followed |

The model's own self-reported `RISK: LOW/MEDIUM/HIGH` is informational only — it is never what drives auto-execution. That decision is made deterministically from the action name and its parameters.

---

## Semantic RAG on Precedents

```
incident query → Titan Embeddings (amazon.titan-embed-text-v1, 1536 dims)
                       ↓
              cosine similarity against all stored precedents
                       ↓
              top-k most similar past incidents injected into Bedrock context
```

The system gets smarter with every resolved incident without retraining — and, as of the review console, learns from human corrections specifically, not just raw AI proposals.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| IaC | Terraform >= 1.5, S3 remote backend |
| Graph Store | S3 — JSON, KMS encryption + versioning |
| AI Reasoning | Amazon Bedrock — `us.anthropic.claude-sonnet-4-6` (cross-region inference) |
| RAG | Amazon Titan Embeddings v1 (1536 dims) + cosine similarity (Python) |
| AI Transport | VPC Interface Endpoint — Bedrock never touches the public internet |
| Agent Compute | ECS Fargate — serverless containers, scales to zero |
| HTTP Framework | FastAPI — async incident handler |
| Remediation Write Path | GitHub Pull Request → CI (OPA/Trivy/Gitleaks) → ArgoCD sync — never a direct AWS write |
| HITL | Email (SNS) + a minimal review console, same Lambda/API Gateway |
| Topology Source | `terraform.tfstate` auto-parsed on every apply |
| Event Trigger | Prometheus/Alertmanager (Kubernetes incidents) + CloudWatch Alarms/EventBridge (legacy AWS-native path, code intact, IAM-blocked) |
| Audit | CloudTrail — S3 WORM + KMS; every GitOps change is also a reviewable diff in Git history |
| IAM | Least-privilege — reasoning role is read-only, verified with a negative test |

---

## Repository Structure

```
sao-platform/
├── mcp-server/
│   ├── app.py                     # FastAPI HTTP server — the reasoner (deployed, ECS Fargate)
│   ├── server.py                  # A real MCP protocol server (FastMCP, 4 tools) — NOT
│   │                               # currently wired into the live pipeline, kept as an
│   │                               # earlier design; app.py is what's actually deployed
│   ├── config.py                  # Environment-based config, incl. Bedrock pricing + which
│   │                               # alarms get git-history grounding context
│   ├── context_map/                 # Digital Twin schema, S3 store, graph query + RAG
│   └── resources/                   # Legacy resource plugins (Lambda/ECS) — IAM-blocked
├── lambda-hitl/
│   └── handler.py                 # Executor + GitHub proxy for the isolated reasoner +
│                                     the review console (Módulo 10)
├── lambda-hitl-poller/
│   └── handler.py                 # EventBridge, every 1 min: merges auto_execute PRs once
│                                     CI is green, confirms loop closure against real
│                                     Prometheus data, generates guardrails
├── lambda-collector/               # Parses tfstate → Digital Twin topology on every apply
├── terraform/
│   ├── networking.tf              # Zero-egress VPC — no NAT, no IGW
│   ├── vpc_endpoints.tf           # Bedrock/ECR/CloudWatch/STS/Lambda PrivateLink + S3 Gateway
│   ├── ecs.tf                     # ECS Fargate cluster + task definition + ALB
│   ├── hitl.tf                    # API Gateway (approve/reject/pending/review routes),
│   │                               # HITL + poller Lambdas, SSM console token
│   ├── argocd_rollback.tf         # The GitOps PAT secret (value loaded manually, never in code)
│   ├── iam.tf / ecr.tf / main.tf / alarms.tf / outputs.tf / variables.tf
│   └── backend.tfbackend.example / terraform.tfvars.example
└── docs/
    ├── digital_twin_schema.json
    ├── context-map.md
    └── extending-digital-twin.md
```

---

## HTTP Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/incident` | Main incident handler — full Bedrock + decision-gate flow |
| `GET` | `/debug/context/{node_id}` | Digital Twin context for a node (no Bedrock call) |
| `POST` | `/debug/prompt` | Full prompt that would be sent to Bedrock, incl. git history (no Bedrock call) |
| `GET` | `/hitl/approve`, `/hitl/reject` | Single-use links from the email notification |
| `GET` | `/hitl/pending` | Console: list proposals waiting on a human (bearer token required) |
| `GET`/`POST` | `/hitl/review/{token}` | Console: view reasoning, approve as-is or adjusted, or reject |

---

## Security

```
Layer 1 — Network:      Zero-egress VPC for the reasoner — no NAT, no IGW, PrivateLink only
Layer 2 — IAM:          Reasoning role is read-only, verified with a real negative test
Layer 3 — Write path:   The only way to change anything is a GitOps Pull Request —
                          never a direct AWS API call
Layer 4 — CI gate:      OPA policy + Trivy (IaC misconfig) + Gitleaks (secrets) on every PR
Layer 5 — HITL:         Anything outside the single narrow auto_execute case escalates
                          to a human, via email or the console
Layer 6 — Console auth: Bearer token (SSM SecureString) gates the routes that list/act on
                          ALL pending proposals — single-use links don't need it
Layer 7 — Guardrails:   Auto-generated policies from resolved incidents NEVER auto-merge,
                          regardless of the risk of the fix that produced them
Layer 8 — Audit:        Every GitOps change is a reviewable Git diff; CloudTrail covers
                          the rest; proposals have explicit status (pending/executed/
                          rejected/failed) — one-time execution
```

**Known gaps, tracked, not hidden:** Semgrep (SAST) was planned but never wired into CI — only Trivy + Gitleaks run today. Telemetry fed into the Bedrock prompt is not sanitized against injection (mitigated by the deterministic code gate, not eliminated). Branch protection isn't enforced as a hard gate (GitHub tier limitation on private repos) — it's process discipline, documented as such. Full list: `estado.md`.

---

## AWS Resources (deployed, torn down between sessions)

| Resource | Name |
|----------|------|
| S3 (graph + proposals) | `<account-id>-sao-graph-<account-id>` |
| Lambda Collector | `sao-lambda-collector` |
| Lambda Dispatcher (Prometheus path) | `saga-alertmanager-dispatcher` |
| Lambda Dispatcher (CloudWatch path, legacy) | `sao-alarm-dispatcher` |
| Lambda HITL | `sao-lambda-hitl` |
| Lambda Poller | `sao-lambda-hitl-poller` |
| API Gateway (HITL + console) | `https://<api-id>.execute-api.<region>.amazonaws.com` |
| ECS Cluster / Service | `sao-platform-cluster` / `sao-platform-service` |
| ALB | `sao-platform-alb-<id>.<region>.elb.amazonaws.com` |
| ECR | `<account-id>.dkr.ecr.<region>.amazonaws.com/sao-mcp-server` |
| SNS Topic | `sao-platform-alarms` (KMS encrypted) |
| Secrets Manager | `saga/gitops-manifests-token` — GitHub PAT, read-only to the reasoner via a proxy call |
| SSM Parameter | `/sao/hitl/console-token` — review console bearer token, generated by Terraform |
| VPC Endpoints | Bedrock, ECR, CloudWatch, STS, Lambda (Interface) + S3 (Gateway) |

---

## Target Use Cases

| Industry | Compliance | Value |
|----------|------------|-------|
| Fintech | SOC2 / PCI-DSS | Incident response — data never leaves the regulated perimeter |
| Healthtech | HIPAA | AI-assisted ops where PHI workloads cannot use public AI endpoints |
| Government | FedRAMP | Sovereign AI operations inside isolated cloud enclaves |
| SaaS B2B | SOC2 | Reduce MTTR without manual on-call toil |

---

## Relation to other projects in this line of work

[aws-sovereign-ops](https://github.com/kratosvil/aws-sovereign-ops) was the v1 proof-of-concept that validated the direct-remediation flow. This repo is the reasoning/execution half of **[SAGA](https://github.com/kratosvil/argocd-gitops-aws)** ([manifests repo](https://github.com/kratosvil/saga-gitops-manifests), private) — the fusion that replaced direct AWS writes with a GitOps-native, policy-gated, self-guarding remediation loop.

---

## License

[Business Source License 1.1](LICENSE)

Free for internal and non-commercial use.
Commercial use requires a license — contact: kratosvill@gmail.com
