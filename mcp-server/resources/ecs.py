import boto3
from .base import ResourcePlugin


class ECSPlugin(ResourcePlugin):

    def __init__(self, region: str = "us-east-1"):
        super().__init__(region)
        self.client = boto3.client("ecs", region_name=region)

    def get_state(self, resource_id: str) -> dict:
        cluster, service = resource_id.split("/", 1)
        response = self.client.describe_services(cluster=cluster, services=[service])
        svc = response["services"][0]
        return {
            "service_name": svc["serviceName"],
            "desired_count": svc["desiredCount"],
            "running_count": svc["runningCount"],
            "pending_count": svc["pendingCount"],
            "status": svc["status"],
        }

    def available_actions(self) -> list[str]:
        return ["scale_desired", "force_new_deployment", "stop_service"]

    def execute_action(self, action: str, resource_id: str, params: dict) -> dict:
        cluster, service = resource_id.split("/", 1)

        if action == "scale_desired":
            self.client.update_service(
                cluster=cluster,
                service=service,
                desiredCount=params["desired_count"],
            )
            return {"status": "success", "action": action, "resource": resource_id}

        if action == "force_new_deployment":
            self.client.update_service(
                cluster=cluster,
                service=service,
                forceNewDeployment=True,
            )
            return {"status": "success", "action": action, "resource": resource_id}

        raise ValueError(f"Unknown action: {action}")

    def risk_level(self, action: str) -> str:
        if action == "scale_desired":
            return "LOW"
        if action == "force_new_deployment":
            return "MEDIUM"
        if action == "stop_service":
            return "HIGH"
        return "HIGH"
