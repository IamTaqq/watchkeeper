data "aws_caller_identity" "current" {}

locals {
  name   = var.project_name
  prefix = "/${var.project_name}"

  # checks.yaml must contain a `checks:` list (use `checks: []` for none).
  user_checks = yamldecode(file(var.checks_file)).checks

  # "" when the demo is disabled (join of an empty list).
  demo_base_url = trimsuffix(join("", aws_lambda_function_url.demo_app[*].function_url), "/")

  # Checks that watch the practice target. They exist only when enable_demo = true.
  demo_definitions = [
    {
      name              = "demo-app-health"
      type              = "http"
      description       = "Practice app /health endpoint"
      url               = "${local.demo_base_url}/health"
      max_latency_ms    = 3000
      failure_threshold = 2
    },
    {
      name              = "demo-data-freshness"
      type              = "http_json_freshness"
      description       = "Practice app data must be updated within 5 minutes"
      url               = "${local.demo_base_url}/status"
      field             = "last_updated"
      max_age_seconds   = 300
      failure_threshold = 2
    },
    {
      name              = "demo-cron"
      type              = "heartbeat"
      description       = "Practice cron job runs every 5 minutes"
      interval_seconds  = 300
      grace_seconds     = 120
      failure_threshold = 1
    },
  ]
  demo_checks = [for c in local.demo_definitions : c if var.enable_demo]

  # Watch the watcher: when AI triage is on, verify its API key hourly (free call),
  # so an expired key is reported before the next incident needs it.
  triage_definitions = [
    {
      name                = "ai-triage-api-key"
      type                = "anthropic_api_key"
      description         = "AI triage API key is still accepted"
      key_parameter       = var.anthropic_api_key_parameter
      check_every_seconds = 3600
      failure_threshold   = 2
    },
  ]
  triage_checks = [for c in local.triage_definitions : c if var.enable_triage]

  all_checks = concat(local.user_checks, local.demo_checks, local.triage_checks)

  supported_types = ["http", "heartbeat", "http_json_freshness", "postgres", "anthropic_api_key"]

  # Only names and thresholds are needed to build alarms; keep the map type uniform.
  # Grouping (...) tolerates duplicate names here so the precondition on
  # aws_ssm_parameter.config can report them with a clear message.
  alarm_thresholds = { for name, v in { for c in local.all_checks : c.name => try(c.failure_threshold, 2)... } : name => v[0] }
  descriptions     = { for name, v in { for c in local.all_checks : c.name => try(c.description, "")... } : name => v[0] }

  schedule = var.check_interval_minutes == 1 ? "rate(1 minute)" : "rate(${var.check_interval_minutes} minutes)"

  dashboard_url = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${local.name}"

  boundary_arn = var.permissions_boundary_name == "" ? null : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${var.permissions_boundary_name}"

  # SSM parameters the checker may read for checks that need a secret.
  postgres_dsn_parameters = distinct([for c in local.all_checks : try(c.dsn_parameter, "") if try(c.type, "") == "postgres"])
  check_secret_parameters = distinct(concat(
    local.postgres_dsn_parameters,
    [for c in local.all_checks : try(c.key_parameter, "/watchkeeper/anthropic_api_key") if try(c.type, "") == "anthropic_api_key"],
    [for c in local.all_checks : try(c.bearer_token_parameter, "") if try(c.bearer_token_parameter, "") != ""],
  ))
}

# --- Shared state ----------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Triage claims (one investigation per incident window) expire on their own.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# The merged check list (yours + demo). The checker reads it at runtime, so
# editing checks.yaml and running `terraform apply` needs no code redeploy.
resource "aws_ssm_parameter" "config" {
  name  = "${local.prefix}/config"
  type  = "String"
  tier  = "Intelligent-Tiering"
  value = jsonencode({ checks = local.all_checks })

  lifecycle {
    precondition {
      condition     = length(local.all_checks) == length(distinct([for c in local.all_checks : c.name]))
      error_message = "Check names must be unique (including the demo checks)."
    }
    precondition {
      condition     = alltrue([for c in local.all_checks : contains(local.supported_types, try(c.type, ""))])
      error_message = "Every check needs a type: http, heartbeat, http_json_freshness, postgres or anthropic_api_key. Run `make validate` for details."
    }
  }
}

resource "random_password" "heartbeat_token" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "heartbeat_token" {
  name  = "${local.prefix}/heartbeat_token"
  type  = "SecureString"
  value = random_password.heartbeat_token.result
}

# --- Alert fan-out -------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "notifier" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = module.notifier.arn
}

resource "aws_lambda_permission" "sns_to_notifier" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.notifier.name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts.arn
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --- Cost guardrail ----------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  count        = var.alert_email == "" ? 0 : 1
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
