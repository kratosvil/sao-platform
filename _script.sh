#!/bin/bash
set -e

aws lambda invoke \
  --function-name sao-lambda-collector \
  --region us-east-1 \
  --payload '{"source":"manual","key":"sao-platform/terraform.tfstate"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/sao-response.json

cat /tmp/sao-response.json
