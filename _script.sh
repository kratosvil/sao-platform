#!/bin/bash
# Reset a OK primero, luego dispara ALARM para forzar el cambio de estado
aws cloudwatch set-alarm-state \
  --alarm-name sao-collector-errors \
  --state-value OK \
  --state-reason "Reset manual antes de test" \
  --region us-east-1

sleep 2

aws cloudwatch set-alarm-state \
  --alarm-name sao-collector-errors \
  --state-value ALARM \
  --state-reason "Test manual Fase 3 — SAO Platform end-to-end" \
  --region us-east-1

echo "Alarma disparada OK→ALARM. Espera ~15s y revisa:"
echo "  - CloudWatch Logs: /aws/lambda/sao-alarm-dispatcher"
echo "  - CloudWatch Logs: /ecs/sao-platform"
echo "  - Email SNS en kratosvill@gmail.com"
