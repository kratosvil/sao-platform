from .schema import DigitalTwin, Node


class GraphQuery:
    """Traversal multi-hop sobre el Digital Twin."""

    def __init__(self, twin: DigitalTwin):
        self.twin = twin

    def impact_radius(self, node_id: str, depth: int = 2) -> list[str]:
        """Nodos afectados si falla node_id — traversal por dependencias."""
        visited = set()
        queue = [(node_id, 0)]
        while queue:
            current, d = queue.pop(0)
            if current in visited or d > depth:
                continue
            visited.add(current)
            for neighbor in self.twin.get_neighbors(current):
                queue.append((neighbor, d + 1))
        visited.discard(node_id)
        return list(visited)

    def causal_hints_for_alarm(self, alarm_name: str) -> list[str]:
        for corr in self.twin.dynamic_state.alarm_correlations:
            if corr.alarm_name == alarm_name:
                return corr.causal_hints
        return []

    def similar_precedents(self, node_type: str, limit: int = 5) -> list[dict]:
        relevant = [
            p for p in self.twin.precedents.remediations
            if any(
                self.twin.get_node(nid) and self.twin.get_node(nid).type == node_type
                for nid in p.nodes_affected
            )
        ]
        relevant.sort(key=lambda p: p.timestamp, reverse=True)
        return [p.model_dump() for p in relevant[:limit]]

    def context_for_agent(self, alarm_name: str, node_id: str) -> dict:
        """Contexto completo listo para pasar a Bedrock."""
        node = self.twin.get_node(node_id)
        neighbors = self.twin.get_neighbors(node_id)
        impact = self.impact_radius(node_id)
        hints = self.causal_hints_for_alarm(alarm_name)
        precedents = self.similar_precedents(node.type if node else "")

        return {
            "alarm": alarm_name,
            "affected_node": node.model_dump() if node else {},
            "dependency_graph": [
                self.twin.get_node(n).model_dump()
                for n in neighbors
                if self.twin.get_node(n)
            ],
            "impact_radius": impact,
            "causal_hints": hints,
            "governance": self.twin.governance.model_dump(),
            "constraints": self.twin.constraints.model_dump(),
            "similar_precedents": precedents,
            "active_alarms": self.twin.dynamic_state.active_alarms,
            "agent_locks": self.twin.dynamic_state.agent_locks,
        }
