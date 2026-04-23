import json
import boto3
from datetime import datetime
from .schema import DigitalTwin
from config import GRAPH_BUCKET, GRAPH_KEY, AWS_REGION


class GraphStore:
    """Lee y escribe el Digital Twin en S3."""

    def __init__(self):
        self.s3 = boto3.client("s3", region_name=AWS_REGION)
        self.bucket = GRAPH_BUCKET
        self.key = GRAPH_KEY

    def load(self) -> DigitalTwin:
        response = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        data = json.loads(response["Body"].read())
        return DigitalTwin.model_validate(data)

    def save(self, twin: DigitalTwin) -> None:
        twin.dynamic_state.last_updated = datetime.utcnow()
        body = twin.model_dump_json(indent=2)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="aws:kms",
        )

    def load_or_empty(self, twin_id: str) -> DigitalTwin:
        try:
            return self.load()
        except self.s3.exceptions.NoSuchKey:
            return DigitalTwin(digital_twin_id=twin_id, version="0.1.0")
