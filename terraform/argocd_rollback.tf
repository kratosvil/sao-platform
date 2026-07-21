# Módulo 1 de SAGA: acción argocd_rollback_via_git en el Lambda HITL.
# El PAT de saga-gitops-manifests vive en Secrets Manager, nunca en código/
# Terraform/env vars en texto plano — el valor se carga a mano (mismo PAT
# ya generado para ArgoCD/CI, scope Contents+PRs Read&Write sobre ese repo).
resource "aws_secretsmanager_secret" "gitops_manifests_token" {
  name        = "saga/gitops-manifests-token"
  description = "PAT de grano fino para kratosvil/saga-gitops-manifests (Contents+PRs Read&Write) — usado por argocd_rollback_via_git"
}

# El HITL solo puede LEER este secret — nunca lo modifica ni lo rota.
resource "aws_iam_role_policy" "hitl_gitops_token" {
  name = "sao-hitl-gitops-token"
  role = aws_iam_role.hitl.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadGitOpsToken"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.gitops_manifests_token.arn
      }
    ]
  })
}
