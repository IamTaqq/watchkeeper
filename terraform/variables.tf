variable "project_name" {
  description = "Prefix for every resource name. Also the CloudWatch metric namespace."
  type        = string
  default     = "watchkeeper"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "Use 2-21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ca-central-1"
}

variable "checks_file" {
  description = "Path to the YAML file that defines your checks."
  type        = string
  default     = "../checks.yaml"
}

variable "check_interval_minutes" {
  description = "How often the checker runs. Also the alarm period."
  type        = number
  default     = 1

  validation {
    condition     = var.check_interval_minutes >= 1 && floor(var.check_interval_minutes) == var.check_interval_minutes
    error_message = "Must be a whole number of minutes, at least 1."
  }
}

variable "enable_demo" {
  description = "Deploy the practice app and cron job, plus checks that watch them."
  type        = bool
  default     = true
}

variable "slack_webhook_parameter" {
  description = "Name of an SSM SecureString holding the Slack incoming-webhook URL. You create it; Terraform never stores it."
  type        = string
  default     = "/watchkeeper/slack_webhook_url"
}

variable "alert_email" {
  description = "Optional email for alarm notifications and the budget alert. Leave empty to skip."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget for this account; you get an email at 80% (requires alert_email)."
  type        = number
  default     = 10
}

variable "log_retention_days" {
  description = "How long Lambda logs are kept. Short retention keeps costs near zero."
  type        = number
  default     = 14
}

# --- Phase 3: AI triage (off by default; costs money per investigation) -------------

variable "enable_triage" {
  description = "Deploy the read-only AI triage agent. Needs an Anthropic API key in SSM."
  type        = bool
  default     = false
}

variable "anthropic_api_key_parameter" {
  description = "Name of an SSM SecureString holding your Anthropic API key. You create it."
  type        = string
  default     = "/watchkeeper/anthropic_api_key"
}

variable "triage_model" {
  description = "Claude model for investigations."
  type        = string
  default     = "claude-sonnet-5-5"
}

variable "triage_max_steps" {
  description = "Maximum tool-calling turns per investigation (caps cost and time)."
  type        = number
  default     = 8
}

variable "triage_cooldown_minutes" {
  description = "Investigate a check at most once per this window, even if it flaps."
  type        = number
  default     = 30
}

variable "extra_log_sources" {
  description = "More log groups the agent may read, as { short_name = \"/log/group/name\" }."
  type        = map(string)
  default     = {}
}

variable "permissions_boundary_name" {
  description = "IAM managed policy attached as a permissions boundary to every role this project creates (created by the bootstrap stack). Empty = no boundary."
  type        = string
  default     = ""
}
