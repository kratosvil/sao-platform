#!/bin/bash
# Extrae el token de la consola HITL (Modulo 10) y prueba /hitl/pending con
# el header correcto -- el 401 es normal si se abre la URL directo en el
# navegador, porque el auth va en header Authorization: Bearer, no en query.
set -euo pipefail

cd "$(dirname "$0")/terraform"

echo "== hitl_api_url =="
HITL_URL=$(terraform output -raw hitl_api_url)
echo "$HITL_URL"

echo ""
echo "== hitl_console_token =="
TOKEN=$(terraform output -raw hitl_console_token)
echo "$TOKEN"

echo ""
echo "== Probando GET /hitl/pending CON el header (deberia dar 200) =="
curl -s -i "$HITL_URL/hitl/pending" -H "Authorization: Bearer $TOKEN" | head -20

echo ""
echo "== Probando GET /hitl/pending SIN el header (para comparar -- este SI da 401, es el fail-closed esperado) =="
curl -s -i "$HITL_URL/hitl/pending" | head -5
