import json
import boto3
from typing import Any

# Mapa CloudWatch namespace → tipo de nodo en el grafo
CW_NAMESPACE_MAP = {
    "AWS/Lambda": "AWS::Lambda::Function",
    "AWS/ECS": "AWS::ECS::Service",
    "AWS/RDS": "AWS::RDS::DBInstance",
    "AWS/ApplicationELB": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS/EKS": "AWS::EKS::Cluster",
    "AWS/EC2": "AWS::EC2::Instance",
}

# Atributos relevantes por tipo de recurso (filtrar tfstate verboso)
RELEVANT_ATTRS = {
    "aws_lambda_function": ["function_name", "runtime", "memory_size", "timeout", "arn", "vpc_config"],
    "aws_ecs_service": ["name", "cluster", "desired_count", "launch_type", "task_definition"],
    "aws_db_instance": ["identifier", "engine", "engine_version", "instance_class", "multi_az", "storage_encrypted"],
    "aws_vpc": ["id", "cidr_block", "tags"],
    "aws_security_group": ["id", "name", "vpc_id", "ingress", "egress"],
    "aws_subnet": ["id", "vpc_id", "cidr_block", "availability_zone"],
    "aws_lb": ["arn", "dns_name", "load_balancer_type", "vpc_id"],
}


class TfstateCollector:
    """Parsea terraform.tfstate desde S3 y extrae nodes + edges para el Digital Twin."""

    def __init__(self, tfstate_bucket: str, region: str = "us-east-1"):
        self.s3 = boto3.client("s3", region_name=region)
        self.bucket = tfstate_bucket

    def load_tfstate(self, key: str) -> dict:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(response["Body"].read())

    def extract_nodes(self, tfstate: dict) -> list[dict]:
        """Extrae nodos del grafo desde recursos de tfstate."""
        nodes = []
        for resource in tfstate.get("resources", []):
            rtype = resource.get("type", "")
            if rtype not in RELEVANT_ATTRS:
                continue

            for instance in resource.get("instances", []):
                attrs = instance.get("attributes", {})
                node_id = self._make_node_id(rtype, attrs)
                aws_type = self._tftype_to_aws(rtype)
                tfstate_address = f"{resource.get('module', 'root')}.{rtype}.{resource['name']}"

                filtered_attrs = {
                    k: attrs[k]
                    for k in RELEVANT_ATTRS.get(rtype, [])
                    if k in attrs
                }

                nodes.append({
                    "id": node_id,
                    "type": aws_type,
                    "tfstate_address": tfstate_address,
                    "properties": filtered_attrs,
                    "tags": attrs.get("tags", {}),
                })
        return nodes

    def extract_edges(self, tfstate: dict, nodes: list[dict]) -> list[dict]:
        """Infiere edges desde referencias entre recursos en tfstate."""
        edges = []
        node_ids = {n["id"] for n in nodes}

        for resource in tfstate.get("resources", []):
            rtype = resource.get("type", "")
            for instance in resource.get("instances", []):
                attrs = instance.get("attributes", {})
                head_id = self._make_node_id(rtype, attrs)
                if head_id not in node_ids:
                    continue

                # Lambda → Security Group
                vpc_config = attrs.get("vpc_config", [{}])
                if vpc_config and isinstance(vpc_config, list):
                    for sg_id in vpc_config[0].get("security_group_ids", []):
                        tail_id = f"sg-{sg_id.split('-')[-1]}" if sg_id else None
                        if tail_id and tail_id in node_ids:
                            edges.append({
                                "head": head_id,
                                "relation": "SECURED_BY",
                                "tail": tail_id,
                            })

                # ECS → ALB (via load_balancers)
                for lb in attrs.get("load_balancer", []):
                    tail_id = lb.get("target_group_arn", "").split(":")[-1]
                    if tail_id in node_ids:
                        edges.append({
                            "head": head_id,
                            "relation": "EXPOSES_VIA",
                            "tail": tail_id,
                        })

        return edges

    def _make_node_id(self, rtype: str, attrs: dict) -> str:
        if rtype == "aws_lambda_function":
            return attrs.get("function_name", "unknown-lambda")
        if rtype == "aws_ecs_service":
            return f"{attrs.get('cluster', 'unknown')}/{attrs.get('name', 'unknown')}"
        if rtype == "aws_db_instance":
            return attrs.get("identifier", "unknown-rds")
        if rtype == "aws_vpc":
            return attrs.get("id", "unknown-vpc")
        if rtype == "aws_security_group":
            return attrs.get("id", "unknown-sg")
        if rtype == "aws_subnet":
            return attrs.get("id", "unknown-subnet")
        if rtype == "aws_lb":
            return attrs.get("arn", "unknown-alb").split("/")[-2]
        return f"{rtype}-unknown"

    def _tftype_to_aws(self, rtype: str) -> str:
        mapping = {
            "aws_lambda_function": "AWS::Lambda::Function",
            "aws_ecs_service": "AWS::ECS::Service",
            "aws_db_instance": "AWS::RDS::DBInstance",
            "aws_vpc": "AWS::EC2::VPC",
            "aws_security_group": "AWS::EC2::SecurityGroup",
            "aws_subnet": "AWS::EC2::Subnet",
            "aws_lb": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        }
        return mapping.get(rtype, f"AWS::Unknown::{rtype}")
