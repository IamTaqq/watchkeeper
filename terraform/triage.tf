# Phase 3: read-only AI triage agent, subscribed to the same alerts topic.
# Enable with enable_triage = true after storing your API key:
#   aws ssm put-parameter --type SecureString --name /watchkeeper/anthropic_api_key --value 'sk-ant-...'

locals {
  core_log_sources = {
    checker   = module.checker.log_group_name
    heartbeat = module.heartbeat.log_group_name
    notifier  = module.notifier.log_group_name
  }
  demo_log_sources = var.enable_demo ? {
    "demo-app"  = module.demo_app[0].log_group_name
    "demo-cron" = module.demo_cron[0].log_group_name
  } : {}
  triage_log_sources = merge(local.core_log_sources, local.demo_log_sources, var.extra_log_sources)
  triage_schedules   = var.enable_demo ? [aws_cloudwatch_event_rule.demo_cron[0].name] : []
  log_group_arn      = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group"
}

data "aws_iam_policy_document" "triage" {
  count = var.enable_triage ? 1 : 0

  statement {
    sid     = "ReadOwnConfigAndSecrets"
    actions = ["ssm:GetParameter"]
    resources = [
      aws_ssm_parameter.config.arn,
      "${local.param_arn}${var.anthropic_api_key_parameter}",
      "${local.param_arn}${var.slack_webhook_parameter}",
    ]
  }
  statement {
    sid       = "StateAndTriageRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:GetMetricStatistics"] # no resource-level permissions exist
    resources = ["*"]
  }
  statement {
    sid       = "ReadAllowlistedLogs"
    actions   = ["logs:FilterLogEvents"]
    resources = [for name in values(local.triage_log_sources) : "${local.log_group_arn}:${name}:*"]
  }
  statement {
    sid       = "ReadRecentChanges"
    actions   = ["cloudtrail:LookupEvents"] # read-only; no resource-level permissions exist
    resources = ["*"]
  }
}

module "triage" {
  count                = var.enable_triage ? 1 : 0
  source               = "./modules/lambda_function"
  permissions_boundary = local.boundary_arn
  name                 = "${local.name}-triage"
  handler              = "handlers.triage.handler"
  package_path         = local.package_path
  package_hash         = local.package_hash
  policy_json          = data.aws_iam_policy_document.triage[0].json
  timeout              = 180
  log_retention_days   = var.log_retention_days
  environment = {
    ANTHROPIC_KEY_PARAMETER = var.anthropic_api_key_parameter
    SLACK_WEBHOOK_PARAMETER = var.slack_webhook_parameter
    CONFIG_PARAMETER        = aws_ssm_parameter.config.name
    STATE_TABLE             = aws_dynamodb_table.state.name
    METRIC_NAMESPACE        = local.name
    TRIAGE_MODEL            = var.triage_model
    MAX_STEPS               = tostring(var.triage_max_steps)
    COOLDOWN_SECONDS        = tostring(var.triage_cooldown_minutes * 60)
    LOG_SOURCES             = jsonencode(local.triage_log_sources)
    SCHEDULES               = jsonencode(local.triage_schedules)
  }
}

resource "aws_sns_topic_subscription" "triage" {
  count     = var.enable_triage ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = module.triage[0].arn
}

resource "aws_lambda_permission" "sns_to_triage" {
  count         = var.enable_triage ? 1 : 0
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.triage[0].name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts.arn
}
