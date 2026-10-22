# One package for every function; `make build` creates ../build/lambda first.
data "archive_file" "package" {
  type        = "zip"
  source_dir  = "${path.module}/../build/lambda"
  output_path = "${path.module}/../build/lambda.zip"
}

locals {
  package_path = data.archive_file.package.output_path
  package_hash = data.archive_file.package.output_base64sha256
  param_arn    = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter"
}

# --- checker: runs the checks on a schedule ----------------------------------------

data "aws_iam_policy_document" "checker" {
  statement {
    sid       = "ReadConfigAndDsns"
    actions   = ["ssm:GetParameter"]
    resources = concat([aws_ssm_parameter.config.arn], [for p in local.postgres_dsn_parameters : "${local.param_arn}${p}"])
  }
  statement {
    sid       = "State"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    sid       = "PublishMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData has no resource-level permissions...
    condition {       # ...so restrict it to our namespace instead.
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.name]
    }
  }
}

module "checker" {
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-checker"
  handler              = "handlers.checker.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.checker.json
  timeout              = 60
  log_retention_days   = var.log_retention_days
  environment = {
    CONFIG_PARAMETER = aws_ssm_parameter.config.name
    STATE_TABLE      = aws_dynamodb_table.state.name
    METRIC_NAMESPACE = local.name
  }
}

resource "aws_cloudwatch_event_rule" "checker" {
  name                = "${local.name}-checker-schedule"
  schedule_expression = local.schedule
}

resource "aws_cloudwatch_event_target" "checker" {
  rule = aws_cloudwatch_event_rule.checker.name
  arn  = module.checker.arn
}

resource "aws_lambda_permission" "events_to_checker" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.checker.name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.checker.arn
}

# --- heartbeat: public endpoint that jobs call ---------------------------------------

data "aws_iam_policy_document" "heartbeat" {
  statement {
    sid       = "ReadConfigAndToken"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.config.arn, aws_ssm_parameter.heartbeat_token.arn]
  }
  statement {
    sid       = "RecordPings"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
}

module "heartbeat" {
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-heartbeat"
  handler              = "handlers.heartbeat.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.heartbeat.json
  timeout              = 10
  memory_size          = 128
  log_retention_days   = var.log_retention_days
  environment = {
    CONFIG_PARAMETER = aws_ssm_parameter.config.name
    TOKEN_PARAMETER  = aws_ssm_parameter.heartbeat_token.name
    STATE_TABLE      = aws_dynamodb_table.state.name
  }
}

resource "aws_lambda_function_url" "heartbeat" {
  function_name      = module.heartbeat.name
  authorization_type = "NONE" # the handler checks a bearer token itself
}

resource "aws_lambda_permission" "heartbeat_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.heartbeat.name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "heartbeat_url_invoke" {
  statement_id             = "FunctionURLInvokeAllowPublicAccess"
  action                   = "lambda:InvokeFunction"
  function_name            = module.heartbeat.name
  principal                = "*"
  invoked_via_function_url = true
}

# --- notifier: alarm state changes -> Slack -------------------------------------------

data "aws_iam_policy_document" "notifier" {
  statement {
    sid       = "ReadSlackWebhook"
    actions   = ["ssm:GetParameter"]
    resources = ["${local.param_arn}${var.slack_webhook_parameter}"]
  }
  statement {
    sid       = "ReadLastResult"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
}

module "notifier" {
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-notifier"
  handler              = "handlers.notifier.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.notifier.json
  timeout              = 15
  memory_size          = 128
  log_retention_days   = var.log_retention_days
  environment = {
    SLACK_WEBHOOK_PARAMETER = var.slack_webhook_parameter
    STATE_TABLE             = aws_dynamodb_table.state.name
    DASHBOARD_URL           = local.dashboard_url
  }
}
