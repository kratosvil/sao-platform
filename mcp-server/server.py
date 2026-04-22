import json
import asyncio
from fastmcp import FastMCP
from context_map import GraphStore, GraphQuery
from context_map.schema import Precedent, RiskLevel
from resources import get_plugin
from config import MCP_SERVER_NAME, MCP_SERVER_VERSION, HITL_TIMEOUT_SECONDS
from datetime import datetime

mcp = FastMCP(MCP_SERVER_NAME, version=MCP_SERVER_VERSION)
store = GraphStore()


@mcp.tool()
async def sao_load_context(alarm_name: str, node_id: str) -> str:
    """
    Carga contexto completo del Digital Twin para un incidente.
    Retorna grafo, dependencias, precedentes y restricciones listo para razonamiento.
    """
    twin = store.load()
    query = GraphQuery(twin)

    if twin.is_locked(node_id):
        return json.dumps({"error": f"Node {node_id} is locked by another agent"})

    context = query.context_for_agent(alarm_name, node_id)
    return json.dumps(context, indent=2, default=str)


@mcp.tool()
async def sao_validate_action(tool: str, action: str, node_id: str) -> str:
    """
    Valida si una accion esta permitida por governance antes de ejecutar.
    Retorna: allowed, risk_level, requires_hitl.
    """
    twin = store.load()

    if twin.is_action_denied(tool, action, node_id):
        return json.dumps({"allowed": False, "reason": "Denied by governance policy"})

    if twin.is_locked(node_id):
        return json.dumps({"allowed": False, "reason": f"Node {node_id} is locked"})

    plugin_cls = get_plugin(
        next((n.type for n in twin.topology.nodes if n.id == node_id), "")
    )
    risk = plugin_cls(twin.topology.nodes[0].properties.get("region", "us-east-1")).risk_level(action) if plugin_cls else "HIGH"

    return json.dumps({
        "allowed": True,
        "risk_level": risk,
        "requires_hitl": risk in ("MEDIUM", "HIGH"),
    })


@mcp.tool()
async def sao_execute_action(
    tool: str,
    action: str,
    node_id: str,
    params: str,
    approved: bool = False,
) -> str:
    """
    Ejecuta una accion sobre un recurso AWS.
    Si requires_hitl=True y approved=False, retorna pending_approval.
    """
    twin = store.load()
    params_dict = json.loads(params)

    if twin.is_action_denied(tool, action, node_id):
        return json.dumps({"status": "denied", "reason": "Governance policy"})

    node = twin.get_node(node_id)
    if not node:
        return json.dumps({"status": "error", "reason": f"Node {node_id} not found in graph"})

    plugin_cls = get_plugin(node.type)
    if not plugin_cls:
        return json.dumps({"status": "error", "reason": f"No plugin for {node.type}"})

    plugin = plugin_cls()
    risk = plugin.risk_level(action)

    if risk in ("MEDIUM", "HIGH") and not approved:
        return json.dumps({
            "status": "pending_approval",
            "risk_level": risk,
            "action": action,
            "node_id": node_id,
            "params": params_dict,
            "message": "Send to HITL Gateway for approval before re-calling with approved=True",
        })

    twin.dynamic_state.agent_locks[node_id] = "sao-agent"
    store.save(twin)

    try:
        result = plugin.execute_action(action, node_id, params_dict)

        twin = store.load()
        twin.dynamic_state.agent_locks.pop(node_id, None)
        twin.precedents.remediations.append(Precedent(
            timestamp=datetime.utcnow(),
            agent="sao-agent",
            intent=f"{action} on {node_id}",
            action=f"{tool}.{action}({params_dict})",
            outcome="Success",
            confidence=1.0,
            nodes_affected=[node_id],
        ))
        store.save(twin)
        return json.dumps({"status": "success", "result": result})

    except Exception as e:
        twin = store.load()
        twin.dynamic_state.agent_locks.pop(node_id, None)
        store.save(twin)
        return json.dumps({"status": "error", "reason": str(e)})


@mcp.tool()
async def sao_graph_status() -> str:
    """Resumen del estado actual del Digital Twin."""
    twin = store.load()
    return json.dumps({
        "twin_id": twin.digital_twin_id,
        "version": twin.version,
        "nodes": len(twin.topology.nodes),
        "edges": len(twin.topology.edges),
        "active_alarms": twin.dynamic_state.active_alarms,
        "agent_locks": twin.dynamic_state.agent_locks,
        "last_updated": twin.dynamic_state.last_updated.isoformat(),
        "precedents_count": len(twin.precedents.remediations),
    }, default=str)


if __name__ == "__main__":
    mcp.run()
