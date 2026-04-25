#!/bin/bash
# Fase 8 test RAG: falla real NoSuchKey → Bedrock recibe precedente con similarity_score
set -e

REGION="us-east-1"
ALARM="sao-collector-errors"
LAMBDA="sao-lambda-collector"

echo "==> Reseteando alarma a OK..."
aws cloudwatch set-alarm-state \
  --alarm-name "$ALARM" \
  --state-value OK \
  --state-reason "Reset pre-test RAG" \
  --region "$REGION"

echo "==> Invocando $LAMBDA con tfstate inexistente (error real)..."
aws lambda invoke \
  --function-name "$LAMBDA" \
  --payload '{"key": "fase8/nonexistent.tfstate"}' \
  --cli-binary-format raw-in-base64-out \
  --region "$REGION" \
  /tmp/sao-lambda-response.json

cat /tmp/sao-lambda-response.json
echo ""
echo "==> Lambda errored. Esperando ~60s para que CloudWatch evalúe..."
echo "    watch -n 10 'aws cloudwatch describe-alarms --alarm-names $ALARM --region $REGION --query MetricAlarms[0].StateValue --output text'"
