from abc import ABC, abstractmethod
import boto3


class ResourcePlugin(ABC):
    """Interfaz base para plugins de resource_type."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    @abstractmethod
    def get_state(self, resource_id: str) -> dict:
        """Estado actual del recurso desde AWS."""

    @abstractmethod
    def available_actions(self) -> list[str]:
        """Acciones que este plugin puede ejecutar."""

    @abstractmethod
    def execute_action(self, action: str, resource_id: str, params: dict) -> dict:
        """Ejecuta una accion y retorna resultado."""

    def risk_level(self, action: str) -> str:
        """LOW / MEDIUM / HIGH — define quien aprueba via HITL."""
        return "MEDIUM"
