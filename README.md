# SAO Platform — Sovereign Agentic Operations

AI-powered autonomous incident response for AWS infrastructure.
Zero-egress. Knowledge Graph context. Human always in the loop.

## What it does

When a CloudWatch alarm fires, SAO:
1. Reads the **Digital Twin Context Map** (your infra graph, built from Terraform state)
2. Reasons with full context — topology, metrics, logs, history, constraints
3. Proposes a fix with exact commands
4. Waits for human approval via Slack
5. Executes the approved action and updates the graph

**Cost:** ~$0.09–$0.19/incident. **Time:** detection to fix in <10 min.

## Architecture

```
CloudWatch Alarm
  → Lambda Collector   (builds context from tfstate + CloudWatch)
  → MCP Server         (queries graph + calls Bedrock)
  → Claude Sonnet      (reasons, proposes fix)
  → HITL Gateway       (Slack approval)
  → MCP Server         (executes via boto3/kubectl/terraform)
  → Graph Store        (updates precedents)
```

## Modules

| Module | Description |
|--------|-------------|
| `mcp-server/` | FastMCP server — the orchestrator |
| `lambda-collector/` | Populates the Digital Twin from tfstate + CloudWatch |
| `terraform/` | Infrastructure for the SAO platform itself |
| `docs/` | Digital Twin schema and architecture docs |

## License

[Business Source License 1.1](LICENSE) — free for internal/non-commercial use.
Commercial use requires a license: kratosvill@gmail.com
