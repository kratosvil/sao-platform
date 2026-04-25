from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Node(BaseModel):
    id: str
    type: str                          # AWS::Lambda::Function, AWS::RDS::DBInstance, etc.
    tfstate_address: str               # module.lambda.aws_lambda_function.main
    properties: dict[str, Any] = {}
    tags: dict[str, str] = {}


class Edge(BaseModel):
    head: str                          # node id
    relation: str                      # DEPENDS_ON, EXPOSES_VIA, WRITES_TO, READS_FROM
    tail: str                          # node id
    properties: dict[str, Any] = {}


class DeniedAction(BaseModel):
    tool: str
    action: str
    target: str
    condition: str = ""


class GovernanceLayer(BaseModel):
    frameworks: list[str] = ["ConstitutionalAI", "Guardrails-v2"]
    denied_actions: list[DeniedAction] = []
    mandatory_tags: dict[str, str] = {}
    compliance_scope: list[str] = []


class AlarmCorrelation(BaseModel):
    alarm_name: str
    impact_nodes: list[str]
    causal_hints: list[str]
    severity: RiskLevel = RiskLevel.MEDIUM


class DynamicStateLayer(BaseModel):
    active_alarms: list[str] = []
    agent_locks: dict[str, str] = {}   # node_id → agent_id holding the lock
    alarm_correlations: list[AlarmCorrelation] = []
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class Precedent(BaseModel):
    timestamp: datetime
    agent: str
    intent: str
    action: str
    outcome: str                       # Success | Failed | Partial
    confidence: float
    nodes_affected: list[str] = []
    embedding: list[float] = Field(default_factory=list)


class PrecedentsLayer(BaseModel):
    remediations: list[Precedent] = []


class ConstraintTopology(BaseModel):
    maintenance_windows: list[dict[str, Any]] = []
    forbidden_ops: list[str] = []


class TopologyLayer(BaseModel):
    nodes: list[Node] = []
    edges: list[Edge] = []


class DigitalTwin(BaseModel):
    digital_twin_id: str
    version: str
    ontology_standard: str = "Agentic-IaC-v1"
    topology: TopologyLayer = Field(default_factory=TopologyLayer)
    governance: GovernanceLayer = Field(default_factory=GovernanceLayer)
    dynamic_state: DynamicStateLayer = Field(default_factory=DynamicStateLayer)
    precedents: PrecedentsLayer = Field(default_factory=PrecedentsLayer)
    constraints: ConstraintTopology = Field(default_factory=ConstraintTopology)

    def get_node(self, node_id: str) -> Node | None:
        return next((n for n in self.topology.nodes if n.id == node_id), None)

    def get_neighbors(self, node_id: str) -> list[str]:
        return [
            e.tail for e in self.topology.edges if e.head == node_id
        ] + [
            e.head for e in self.topology.edges if e.tail == node_id
        ]

    def is_locked(self, node_id: str) -> bool:
        return node_id in self.dynamic_state.agent_locks

    def is_action_denied(self, tool: str, action: str, node_id: str) -> bool:
        for denied in self.governance.denied_actions:
            if denied.tool == tool and denied.action == action:
                import fnmatch
                if fnmatch.fnmatch(node_id, denied.target):
                    return True
        return False
