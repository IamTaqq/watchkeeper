variable "name" {
  type = string
}

variable "handler" {
  type = string
}

variable "package_path" {
  type = string
}

variable "package_hash" {
  type = string
}

variable "policy_json" {
  description = "IAM policy for what this function may do, beyond writing its own logs."
  type        = string
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "timeout" {
  type    = number
  default = 30
}

variable "memory_size" {
  type    = number
  default = 256
}

variable "permissions_boundary" {
  description = "ARN of a managed policy used as the role's permissions boundary (optional)."
  type        = string
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 14
}

data "aws_region" "current" {}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "logs" {
  source_policy_documents = [var.policy_json]

  statement {
    sid       = "DecryptSecureStringsViaSSM"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "this" {
  name                 = "${var.name}-role"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy" "this" {
  name   = "${var.name}-policy"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

resource "aws_lambda_function" "this" {
  function_name    = var.name
  role             = aws_iam_role.this.arn
  handler          = var.handler
  runtime          = "python3.12"
  architectures    = ["arm64"]
  filename         = var.package_path
  source_code_hash = var.package_hash
  timeout          = var.timeout
  memory_size      = var.memory_size

  environment {
    variables = var.environment
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.this]
}

output "arn" {
  value = aws_lambda_function.this.arn
}

output "name" {
  value = aws_lambda_function.this.function_name
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  value = aws_cloudwatch_log_group.this.arn
}
