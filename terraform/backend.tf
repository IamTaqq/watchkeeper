# Remote state in S3 (bucket created by bootstrap/bootstrap.yaml).
# Settings are supplied at init time so this file holds no account details:
#   terraform init -backend-config=backend.hcl        (local; see backend.hcl.example)
#   CI passes the same values with -backend-config flags.
# Plan tests (`terraform test`) and `init -backend=false` do not use it.
terraform {
  backend "s3" {}
}
