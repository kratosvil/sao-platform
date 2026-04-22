import boto3
from .base import ResourcePlugin


class LambdaPlugin(ResourcePlugin):

    def __init__(self, region: str = "us-east-1"):
        super().__init__(region)
        self.client = boto3.client("lambda", region_name=region)

    def get_state(self, resource_id: str) -> dict:
        fn = self.client.get_function_configuration(FunctionName=resource_id)
        return {
            "function_name": fn["FunctionName"],
            "runtime": fn["Runtime"],
            "memory_size": fn["MemorySize"],
            "timeout": fn["Timeout"],
            "last_modified": fn["LastModified"],
        }

    def available_actions(self) -> list[str]:
        return ["update_timeout", "update_memory", "update_concurrency", "invoke_test"]

    def execute_action(self, action: str, resource_id: str, params: dict) -> dict:
        if action == "update_timeout":
            self.client.update_function_configuration(
                FunctionName=resource_id,
                Timeout=params["timeout"],
            )
            return {"status": "success", "action": action, "resource": resource_id}

        if action == "update_memory":
            self.client.update_function_configuration(
                FunctionName=resource_id,
                MemorySize=params["memory_size"],
            )
            return {"status": "success", "action": action, "resource": resource_id}

        if action == "update_concurrency":
            self.client.put_function_concurrency(
                FunctionName=resource_id,
                ReservedConcurrentExecutions=params["concurrency"],
            )
            return {"status": "success", "action": action, "resource": resource_id}

        raise ValueError(f"Unknown action: {action}")

    def risk_level(self, action: str) -> str:
        if action in ("update_timeout", "update_memory"):
            return "LOW"
        if action == "update_concurrency":
            return "MEDIUM"
        return "HIGH"
