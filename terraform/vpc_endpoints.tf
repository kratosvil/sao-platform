# S3 Gateway endpoint — gratuito, necesario para que ECS Fargate
# pueda descargar capas de imagen desde ECR (almacenadas en S3)
# sin salida a internet.
data "aws_route_tables" "vpc" {
  vpc_id = module.networking.vpc_id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.networking.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.vpc.ids

  tags = { Name = "sao-platform-s3" }
}

# Bedrock PrivateLink + ECR + CloudWatch + STS + Lambda
# Reutiliza el modulo de aws-sovereign-ops (ya validado en v1)
module "bedrock_privatelink" {
  source = "github.com/kratosvil/aws-sovereign-ops//modules/bedrock-privatelink?ref=main"

  project_name   = "sao-platform"
  vpc_id         = module.networking.vpc_id
  subnet_ids     = module.networking.subnet_private_ids
  allowed_sg_ids = [module.networking.sg_app_id]

  depends_on = [module.networking]
}
