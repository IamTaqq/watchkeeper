terraform {
  required_version = ">= 1.10" # 1.10+: S3 native state locking; 1.7+: test mocks

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.28+ supports invoked_via_function_url, required for public function URLs.
      version = ">= 6.28, < 7.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
