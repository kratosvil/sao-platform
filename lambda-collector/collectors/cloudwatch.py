import boto3
from datetime import datetime, timedelta
from typing import Any


class CloudWatchCollector:
    """Recolecta metricas y logs de CloudWatch para el dynamic_state del grafo."""

    def __init__(self, region: str = "us-east-1"):
        self.cw = boto3.client("cloudwatch", region_name=region)
        self.logs = boto3.client("logs", region_name=region)

    def get_metrics(self, namespace: str, metric_name: str, dimensions: list[dict],
                    period_minutes: int = 60) -> list[dict]:
        end = datetime.utcnow()
        start = end - timedelta(minutes=period_minutes)

        response = self.cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=300,
            Statistics=["Average", "Maximum", "Sum"],
        )
        return sorted(response.get("Datapoints", []), key=lambda x: x["Timestamp"])

    def get_active_alarms(self, prefix: str = "sovereign-aiops-") -> list[str]:
        response = self.cw.describe_alarms(
            AlarmNamePrefix=prefix,
            StateValue="ALARM",
        )
        return [a["AlarmName"] for a in response.get("MetricAlarms", [])]

    def get_recent_logs(self, log_group: str, lines: int = 500,
                        minutes: int = 30) -> list[str]:
        end_ms = int(datetime.utcnow().timestamp() * 1000)
        start_ms = end_ms - (minutes * 60 * 1000)

        try:
            response = self.logs.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                endTime=end_ms,
                limit=lines,
            )
            return [e["message"] for e in response.get("events", [])]
        except self.logs.exceptions.ResourceNotFoundException:
            return []

    def get_lambda_metrics(self, function_name: str) -> dict:
        dims = [{"Name": "FunctionName", "Value": function_name}]
        return {
            "errors": self.get_metrics("AWS/Lambda", "Errors", dims),
            "duration": self.get_metrics("AWS/Lambda", "Duration", dims),
            "throttles": self.get_metrics("AWS/Lambda", "Throttles", dims),
            "concurrent_executions": self.get_metrics("AWS/Lambda", "ConcurrentExecutions", dims),
        }

    def get_ecs_metrics(self, cluster: str, service: str) -> dict:
        dims = [
            {"Name": "ClusterName", "Value": cluster},
            {"Name": "ServiceName", "Value": service},
        ]
        return {
            "cpu_utilization": self.get_metrics("AWS/ECS", "CPUUtilization", dims),
            "memory_utilization": self.get_metrics("AWS/ECS", "MemoryUtilization", dims),
        }
