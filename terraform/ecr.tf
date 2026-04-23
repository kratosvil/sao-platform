module "ecr" {
  source = "github.com/kratosvil/tf-modules-forge//modules/ecr?ref=main"

  project_name    = "sao-platform"
  repository_name = "sao-mcp-server"
  scan_on_push    = true
}
