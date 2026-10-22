output "heartbeat_url" {
  description = "Base URL jobs call: POST <url>ping/<check-name>"
  value       = aws_lambda_function_url.heartbeat.function_url
}

output "heartbeat_token_parameter" {
  description = "SSM parameter holding the heartbeat token (read with `make token`)."
  value       = aws_ssm_parameter.heartbeat_token.name
}

output "demo_app_url" {
  description = "Practice app base URL (empty when enable_demo = false)."
  value       = local.demo_base_url
}

output "dashboard_url" {
  value = local.dashboard_url
}

output "alerts_topic_arn" {
  description = "Subscribe more consumers here (e.g. the AI triage agent in phase 3)."
  value       = aws_sns_topic.alerts.arn
}

output "check_names" {
  value = keys(local.alarm_thresholds)
}
