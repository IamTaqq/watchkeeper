# One alarm per check. The alarm, not the checker, decides when to alert:
#   - fires after `failure_threshold` consecutive failed runs (filters blips)
#   - missing data counts as failure, so a dead checker also pages you
#   - OK actions send a "recovered" message
resource "aws_cloudwatch_metric_alarm" "check" {
  for_each = local.alarm_thresholds

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = local.descriptions[each.key] == "" ? null : local.descriptions[each.key]
  namespace           = local.name
  metric_name         = "CheckSuccess"
  dimensions          = { Check = each.key }
  statistic           = "Minimum"
  period              = var.check_interval_minutes * 60
  evaluation_periods  = each.value
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  # Publish a first datapoint before alarms exist, so a fresh deploy doesn't
  # page you for "missing data".
  depends_on = [aws_lambda_invocation.checker_warmup]
}

resource "aws_lambda_invocation" "checker_warmup" {
  function_name = module.checker.name
  # Re-runs whenever the check config changes.
  input = jsonencode({ source = "terraform-warmup", config_version = aws_ssm_parameter.config.version })
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name

  lifecycle {
    precondition {
      condition     = length(local.alarm_thresholds) > 0
      error_message = "Nothing to monitor: add a check to checks.yaml or set enable_demo = true."
    }
  }
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Check success (1 = passing)"
          region  = var.aws_region
          stat    = "Minimum"
          period  = var.check_interval_minutes * 60
          view    = "timeSeries"
          yAxis   = { left = { min = 0, max = 1 } }
          metrics = [for name in keys(local.alarm_thresholds) : [local.name, "CheckSuccess", "Check", name]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Check latency (ms)"
          region  = var.aws_region
          stat    = "Average"
          period  = var.check_interval_minutes * 60
          view    = "timeSeries"
          metrics = [for name in keys(local.alarm_thresholds) : [local.name, "LatencyMs", "Check", name]]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Monitoring pipeline health"
          region = var.aws_region
          stat   = "Sum"
          period = 300
          view   = "timeSeries"
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", module.checker.name],
            ["AWS/Lambda", "Errors", "FunctionName", module.checker.name],
            ["AWS/Lambda", "Errors", "FunctionName", module.notifier.name],
          ]
        }
      },
      {
        type   = "alarm"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Alarms"
          alarms = [for a in aws_cloudwatch_metric_alarm.check : a.arn]
        }
      },
    ]
  })
}
