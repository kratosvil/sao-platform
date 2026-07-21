#!/usr/bin/env bash
set -euo pipefail

# Módulo 1 (SAGA): carga el mismo PAT de saga-gitops-manifests en Secrets
# Manager para que el Lambda HITL lo use en argocd_rollback_via_git.
# El token nunca se imprime ni queda en ningún archivo de este repo.

read -rsp "Pegá el mismo PAT de saga-gitops-manifests (Contents+PRs Read&Write): " TOKEN
echo ""

if [ -z "$TOKEN" ]; then
  echo "Token vacío, cancelado."
  exit 1
fi

aws secretsmanager put-secret-value \
  --secret-id saga/gitops-manifests-token \
  --secret-string "$TOKEN" \
  --region us-east-1 > /dev/null

unset TOKEN
echo "Listo — token cargado en Secrets Manager (saga/gitops-manifests-token)."
