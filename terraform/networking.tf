module "networking" {
  source = "github.com/kratosvil/tf-modules-forge//modules/networking?ref=main"

  project_name        = "sao-platform"
  vpc_cidr            = var.vpc_cidr
  az_count            = 2
  enable_nat_gateway  = false  # zero-egress — Bedrock/S3/CW via PrivateLink
  enable_data_subnets = false  # no DB tier en esta fase
}
