terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Backend config via -backend-config flag or terraform.tfbackend (never hardcode)
  # terraform init -backend-config=backend.tfbackend
  # See terraform/backend.tfbackend.example for the required format
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "sao-platform"
      ManagedBy   = "SAO"
      Owner       = "SamirVilla"
      Environment = var.environment
    }
  }
}
