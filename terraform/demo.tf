# Practice target: an app and a cron job you can break on purpose.
# Disable with enable_demo = false once you monitor real services.

resource "aws_ssm_parameter" "fault_mode" {
  count = var.enable_demo ? 1 : 0
  name  = "${local.prefix}/demo/fault_mode"
  type  = "String"
  value = "none"

  lifecycle {
    ignore_changes = [value] # changed by `make fault`, not by Terraform
  }
}

data "aws_iam_policy_document" "demo_app" {
  count = var.enable_demo ? 1 : 0
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.fault_mode[0].arn]
  }
}

module "demo_app" {
  count                = var.enable_demo ? 1 : 0
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-demo-app"
  handler              = "handlers.demo_app.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.demo_app[0].json
  timeout              = 15
  memory_size          = 128
  log_retention_days   = var.log_retention_days
  environment = {
    FAULT_PARAMETER = aws_ssm_parameter.fault_mode[0].name
  }
}

resource "aws_lambda_function_url" "demo_app" {
  count              = var.enable_demo ? 1 : 0
  function_name      = module.demo_app[0].name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "demo_app_url" {
  count                  = var.enable_demo ? 1 : 0
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.demo_app[0].name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "demo_app_url_invoke" {
  count                    = var.enable_demo ? 1 : 0
  statement_id             = "FunctionURLInvokeAllowPublicAccess"
  action                   = "lambda:InvokeFunction"
  function_name            = module.demo_app[0].name
  principal                = "*"
  invoked_via_function_url = true
}

data "aws_iam_policy_document" "demo_cron" {
  count = var.enable_demo ? 1 : 0
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.fault_mode[0].arn, aws_ssm_parameter.heartbeat_token.arn]
  }
}

module "demo_cron" {
  count                = var.enable_demo ? 1 : 0
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-demo-cron"
  handler              = "handlers.demo_cron.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.demo_cron[0].json
  timeout              = 15
  memory_size          = 128
  log_retention_days   = var.log_retention_days
  environment = {
    FAULT_PARAMETER = aws_ssm_parameter.fault_mode[0].name
    TOKEN_PARAMETER = aws_ssm_parameter.heartbeat_token.name
    HEARTBEAT_URL   = aws_lambda_function_url.heartbeat.function_url
    CHECK_NAME      = "demo-cron"
  }
}

resource "aws_cloudwatch_event_rule" "demo_cron" {
  count               = var.enable_demo ? 1 : 0
  name                = "${local.name}-demo-cron"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "demo_cron" {
  count = var.enable_demo ? 1 : 0
  rule  = aws_cloudwatch_event_rule.demo_cron[0].name
  arn   = module.demo_cron[0].arn
}

resource "aws_lambda_permission" "events_to_demo_cron" {
  count         = var.enable_demo ? 1 : 0
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.demo_cron[0].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.demo_cron[0].arn
}
