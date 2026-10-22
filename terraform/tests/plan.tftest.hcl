# Plan-only tests with mocked providers: no AWS account or credentials needed.
#   cd terraform && terraform test

mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ca-central-1:123456789012:function:mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ca-central-1:123456789012:mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:ca-central-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:ca-central-1:123456789012:table/mock" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:ca-central-1:123456789012:parameter/mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:ca-central-1:123456789012:rule/mock" }
  }
  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = { arn = "arn:aws:cloudwatch:ca-central-1:123456789012:alarm:mock" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

mock_provider "archive" {
  mock_data "archive_file" {
    defaults = { output_base64sha256 = "dGVzdA==" }
  }
}

mock_provider "random" {}

run "demo_and_user_checks" {
  command = plan

  variables {
    checks_file = "./tests/fixtures/two_checks.yaml"
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.check) == 6
    error_message = "expected 3 user checks + 3 demo checks"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.check["nightly"].evaluation_periods == 1
    error_message = "failure_threshold should map to evaluation_periods"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.check["site"].evaluation_periods == 2
    error_message = "default failure_threshold should be 2"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.check["site"].treat_missing_data == "breaching"
    error_message = "a dead checker must raise alarms"
  }
  assert {
    condition     = aws_cloudwatch_event_rule.checker.schedule_expression == "rate(1 minute)"
    error_message = "default schedule should be every minute"
  }
  assert {
    condition     = length(module.demo_app) == 1 && length(aws_budgets_budget.monthly) == 0
    error_message = "demo on, budget off without email"
  }
  assert {
    condition     = contains(local.postgres_dsn_parameters, "/watchkeeper/dsn/app")
    error_message = "checker must be allowed to read the postgres DSN parameter"
  }
}

run "no_demo_with_email" {
  command = plan

  variables {
    checks_file            = "./tests/fixtures/two_checks.yaml"
    enable_demo            = false
    alert_email            = "me@example.com"
    check_interval_minutes = 5
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.check) == 3 && length(module.demo_app) == 0
    error_message = "demo resources should be gone"
  }
  assert {
    condition     = aws_cloudwatch_event_rule.checker.schedule_expression == "rate(5 minutes)"
    error_message = "plural schedule expression"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.check["site"].period == 300
    error_message = "alarm period must follow the check interval"
  }
  assert {
    condition     = length(aws_budgets_budget.monthly) == 1 && length(aws_sns_topic_subscription.email) == 1
    error_message = "email enables the budget and the email subscription"
  }
}

run "duplicate_names_fail" {
  command = plan
  variables {
    checks_file = "./tests/fixtures/duplicate.yaml"
  }
  expect_failures = [aws_ssm_parameter.config]
}

run "unknown_type_fails" {
  command = plan
  variables {
    checks_file = "./tests/fixtures/bad_type.yaml"
  }
  expect_failures = [aws_ssm_parameter.config]
}

run "nothing_to_monitor_fails" {
  command = plan
  variables {
    checks_file = "./tests/fixtures/empty.yaml"
    enable_demo = false
  }
  expect_failures = [aws_cloudwatch_dashboard.main]
}

run "triage_enabled" {
  command = plan
  variables {
    checks_file   = "./tests/fixtures/two_checks.yaml"
    enable_triage = true
    extra_log_sources = {
      ingest = "/aws/lambda/my-ingest"
    }
  }

  assert {
    condition     = length(module.triage) == 1 && length(aws_sns_topic_subscription.triage) == 1
    error_message = "triage function and subscription should exist"
  }
  assert {
    condition     = contains(keys(local.triage_log_sources), "demo-app") && local.triage_log_sources["ingest"] == "/aws/lambda/my-ingest"
    error_message = "agent should read demo logs and extra log sources"
  }
  assert {
    condition     = aws_dynamodb_table.state.ttl[0].enabled
    error_message = "triage claims rely on TTL"
  }
  assert {
    condition     = contains(keys(aws_cloudwatch_metric_alarm.check), "ai-triage-api-key")
    error_message = "triage should add a check that its API key still works"
  }
  assert {
    condition     = contains(local.check_secret_parameters, "/watchkeeper/anthropic_api_key")
    error_message = "the checker must be allowed to read the API key for that check"
  }
}

run "triage_off_by_default" {
  command = plan
  variables {
    checks_file = "./tests/fixtures/two_checks.yaml"
  }
  assert {
    condition     = length(module.triage) == 0 && !contains(keys(aws_cloudwatch_metric_alarm.check), "ai-triage-api-key")
    error_message = "triage (and its key check) must be opt-in"
  }
}

run "bearer_token_secret_is_readable" {
  command = plan
  variables {
    checks_file = "../checks.yaml"
    enable_demo = false
  }
  assert {
    condition     = contains(local.check_secret_parameters, "/watchkeeper/tutorhub_health_token")
    error_message = "the checker must be allowed to read tokens used by http checks"
  }
}
