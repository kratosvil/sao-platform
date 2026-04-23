import json
import boto3

# Mapa CloudWatch namespace → tipo de nodo en el grafo
CW_NAMESPACE_MAP = {
    "AWS/Lambda":            "AWS::Lambda::Function",
    "AWS/ECS":               "AWS::ECS::Service",
    "AWS/RDS":               "AWS::RDS::DBInstance",
    "AWS/ApplicationELB":    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS/EKS":               "AWS::EKS::Cluster",
    "AWS/EC2":               "AWS::EC2::Instance",
}

# Atributos relevantes por tipo de recurso (filtrar tfstate verboso)
RELEVANT_ATTRS = {
    "aws_lambda_function": [
        "function_name", "runtime", "memory_size", "timeout", "arn", "vpc_config",
    ],
    "aws_ecs_cluster": ["name", "arn", "capacity_providers"],
    "aws_ecs_service": [
        "name", "cluster", "desired_count", "launch_type", "task_definition",
    ],
    "aws_db_instance": [
        "identifier", "engine", "engine_version", "instance_class",
        "multi_az", "storage_encrypted", "vpc_security_group_ids",
    ],
    "aws_rds_cluster": [
        "cluster_identifier", "engine", "engine_version",
        "database_name", "master_username", "vpc_security_group_ids",
    ],
    "aws_instance": [
        "instance_id", "instance_type", "ami", "subnet_id",
        "vpc_security_group_ids", "private_ip", "public_ip",
    ],
    "aws_eks_cluster": ["name", "version", "endpoint", "role_arn", "vpc_config"],
    "aws_vpc":            ["id", "cidr_block", "tags"],
    "aws_security_group": ["id", "name", "vpc_id"],
    "aws_subnet":         ["id", "vpc_id", "cidr_block", "availability_zone"],
    "aws_lb":             ["arn", "dns_name", "load_balancer_type", "vpc_id"],
}

# Mapa terraform type → AWS CloudFormation type
_TF_TO_AWS = {
    "aws_lambda_function":  "AWS::Lambda::Function",
    "aws_ecs_cluster":      "AWS::ECS::Cluster",
    "aws_ecs_service":      "AWS::ECS::Service",
    "aws_db_instance":      "AWS::RDS::DBInstance",
    "aws_rds_cluster":      "AWS::RDS::DBCluster",
    "aws_instance":         "AWS::EC2::Instance",
    "aws_eks_cluster":      "AWS::EKS::Cluster",
    "aws_vpc":              "AWS::EC2::VPC",
    "aws_security_group":   "AWS::EC2::SecurityGroup",
    "aws_subnet":           "AWS::EC2::Subnet",
    "aws_lb":               "AWS::ElasticLoadBalancingV2::LoadBalancer",
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
        nodes = []
        for resource in tfstate.get("resources", []):
            rtype = resource.get("type", "")
            if rtype not in RELEVANT_ATTRS:
                continue
            for instance in resource.get("instances", []):
                attrs = instance.get("attributes", {})
                node_id  = self._make_node_id(rtype, attrs)
                aws_type = _TF_TO_AWS.get(rtype, f"AWS::Unknown::{rtype}")
                address  = f"{resource.get('module', 'root')}.{rtype}.{resource['name']}"
                filtered = {k: attrs[k] for k in RELEVANT_ATTRS[rtype] if k in attrs}
                nodes.append({
                    "id":              node_id,
                    "type":            aws_type,
                    "tfstate_address": address,
                    "properties":      filtered,
                    "tags":            attrs.get("tags", {}),
                })
        return nodes

    def extract_edges(self, tfstate: dict, nodes: list[dict]) -> list[dict]:
        edges = []
        node_ids = {n["id"] for n in nodes}

        for resource in tfstate.get("resources", []):
            rtype = resource.get("type", "")
            for instance in resource.get("instances", []):
                attrs   = instance.get("attributes", {})
                head_id = self._make_node_id(rtype, attrs)
                if head_id not in node_ids:
                    continue

                # Lambda → SecurityGroup (SECURED_BY) + Lambda → Subnet (RUNS_IN)
                if rtype == "aws_lambda_function":
                    vpc_cfg = attrs.get("vpc_config") or []
                    if isinstance(vpc_cfg, list) and vpc_cfg:
                        for sg_id in vpc_cfg[0].get("security_group_ids", []):
                            if sg_id in node_ids:
                                edges.append({"head": head_id, "relation": "SECURED_BY", "tail": sg_id})
                        for sn_id in vpc_cfg[0].get("subnet_ids", []):
                            if sn_id in node_ids:
                                edges.append({"head": head_id, "relation": "RUNS_IN", "tail": sn_id})

                # ECS Service → Cluster (BELONGS_TO) + ECS Service → ALB (EXPOSES_VIA)
                if rtype == "aws_ecs_service":
                    cluster_ref = attrs.get("cluster", "")
                    cluster_name = cluster_ref.split("/")[-1] if cluster_ref else ""
                    if cluster_name in node_ids:
                        edges.append({"head": head_id, "relation": "BELONGS_TO", "tail": cluster_name})
                    for lb in attrs.get("load_balancer", []):
                        tg_tail = lb.get("target_group_arn", "").split(":")[-1]
                        if tg_tail in node_ids:
                            edges.append({"head": head_id, "relation": "EXPOSES_VIA", "tail": tg_tail})

                # RDS Instance → SecurityGroup (SECURED_BY)
                if rtype in ("aws_db_instance", "aws_rds_cluster"):
                    for sg_id in attrs.get("vpc_security_group_ids", []):
                        if sg_id in node_ids:
                            edges.append({"head": head_id, "relation": "SECURED_BY", "tail": sg_id})

                # EC2 → SecurityGroup (SECURED_BY) + EC2 → Subnet (RUNS_IN)
                if rtype == "aws_instance":
                    for sg_id in attrs.get("vpc_security_group_ids", []):
                        if sg_id in node_ids:
                            edges.append({"head": head_id, "relation": "SECURED_BY", "tail": sg_id})
                    sn_id = attrs.get("subnet_id", "")
                    if sn_id in node_ids:
                        edges.append({"head": head_id, "relation": "RUNS_IN", "tail": sn_id})

                # EKS → Subnet (RUNS_IN)
                if rtype == "aws_eks_cluster":
                    vpc_cfg = attrs.get("vpc_config") or []
                    if isinstance(vpc_cfg, list) and vpc_cfg:
                        for sn_id in vpc_cfg[0].get("subnet_ids", []):
                            if sn_id in node_ids:
                                edges.append({"head": head_id, "relation": "RUNS_IN", "tail": sn_id})

                # Subnet → VPC (BELONGS_TO)
                if rtype == "aws_subnet":
                    vpc_id = attrs.get("vpc_id", "")
                    if vpc_id in node_ids:
                        edges.append({"head": head_id, "relation": "BELONGS_TO", "tail": vpc_id})

                # SecurityGroup → VPC (BELONGS_TO)
                if rtype == "aws_security_group":
                    vpc_id = attrs.get("vpc_id", "")
                    if vpc_id in node_ids:
                        edges.append({"head": head_id, "relation": "BELONGS_TO", "tail": vpc_id})

        return edges

    def _make_node_id(self, rtype: str, attrs: dict) -> str:
        if rtype == "aws_lambda_function":  return attrs.get("function_name", "unknown-lambda")
        if rtype == "aws_ecs_cluster":      return attrs.get("name", "unknown-ecs-cluster")
        if rtype == "aws_ecs_service":      return attrs.get("name", "unknown-ecs-service")
        if rtype == "aws_db_instance":      return attrs.get("identifier", "unknown-rds")
        if rtype == "aws_rds_cluster":      return attrs.get("cluster_identifier", "unknown-aurora")
        if rtype == "aws_instance":         return attrs.get("instance_id", "unknown-ec2")
        if rtype == "aws_eks_cluster":      return attrs.get("name", "unknown-eks")
        if rtype == "aws_vpc":              return attrs.get("id", "unknown-vpc")
        if rtype == "aws_security_group":   return attrs.get("id", "unknown-sg")
        if rtype == "aws_subnet":           return attrs.get("id", "unknown-subnet")
        if rtype == "aws_lb":               return attrs.get("arn", "unknown-alb").split("/")[-2]
        return f"{rtype}-unknown"
