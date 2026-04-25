#!/bin/bash
# Fase 7 — falla real: Lambda error genuino → alarm natural (sin set-alarm-state)
set -e

REGION="us-east-1"
ALARM="sao-collector-errors"
LAMBDA="sao-lambda-collector"

echo "==> Reseteando alarma a OK..."
aws cloudwatch set-alarm-state \
  --alarm-name "$ALARM" \
  --state-value OK \
  --state-reason "Reset pre-test Fase 7" \
  --region "$REGION"

echo "==> Invocando $LAMBDA con tfstate inexistente (provoca error real)..."
aws lambda invoke \
  --function-name "$LAMBDA" \
  --payload '{"key": "fase7/nonexistent.tfstate"}' \
  --cli-binary-format raw-in-base64-out \
  --region "$REGION" \
  /tmp/sao-lambda-response.json

echo ""
echo "Respuesta Lambda:"
cat /tmp/sao-lambda-response.json
echo ""
echo "==> Lambda invocada. CloudWatch necesita ~60s para evaluar la métrica Errors."
echo "    Monitorea el estado de la alarma con:"
echo "      watch -n 10 'aws cloudwatch describe-alarms --alarm-names $ALARM --region $REGION --query MetricAlarms[0].StateValue --output text'"
echo ""
echo "    Cuando la alarma pase a ALARM, revisa email SNS en kratosvill@gmail.com"
