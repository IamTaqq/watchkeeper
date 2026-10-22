#!/usr/bin/env bash
# Run Watchkeeper from AWS CloudShell.
#
# No AWS keys are created or stored: every command uses the credentials of
# your signed-in console session. Use this where account guardrails forbid the
# GitHub OIDC deploy path (see docs/deploy-cloudshell-ja.md).
#
#   ./scripts/cloudshell.sh setup        install Terraform into ~/.local/bin (once)
#   ./scripts/cloudshell.sh bootstrap    state bucket + permissions boundary (once)
#   ./scripts/cloudshell.sh deploy       build, plan, ask, apply
#   ./scripts/cloudshell.sh status       alarms and demo state
#   ./scripts/cloudshell.sh fault MODE   none|error|slow|stale|down|cron_fail
#   ./scripts/cloudshell.sh pause-cron | resume-cron | run-checker | test-alert | logs | outputs
#   ./scripts/cloudshell.sh destroy      delete everything Terraform created
set -euo pipefail

PROJECT="watchkeeper"
REGION="${AWS_REGION:-us-east-2}"
TF_VERSION="1.12.2"
STACK="${PROJECT}-bootstrap"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
export AWS_REGION="$REGION"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true
}

cmd_setup() {
  if command -v terraform >/dev/null && terraform version | head -1 | grep -q "v${TF_VERSION}"; then
    say "Terraform ${TF_VERSION} is already installed."; return
  fi
  local arch
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) die "unsupported CPU $(uname -m)" ;;
  esac
  local tmp zip base
  tmp="$(mktemp -d)"
  zip="terraform_${TF_VERSION}_linux_${arch}.zip"
  base="https://releases.hashicorp.com/terraform/${TF_VERSION}"
  say "Downloading Terraform ${TF_VERSION} (${arch})..."
  curl -fsSL -o "$tmp/$zip" "$base/$zip"
  curl -fsSL -o "$tmp/SHA256SUMS" "$base/terraform_${TF_VERSION}_SHA256SUMS"
  (cd "$tmp" && grep " ${zip}\$" SHA256SUMS | sha256sum -c -) || die "checksum mismatch"
  mkdir -p "$HOME/.local/bin"
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract("terraform", sys.argv[2])' "$tmp/$zip" "$HOME/.local/bin"
  chmod +x "$HOME/.local/bin/terraform"
  rm -rf "$tmp"
  terraform version | head -1
}

cmd_bootstrap() {
  say "Creating the state bucket and permissions boundary in ${REGION}..."
  aws cloudformation deploy --stack-name "$STACK" \
    --template-file "$ROOT/bootstrap/bootstrap.yaml" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides EnableGitHubDeploy=false ProjectName="$PROJECT"
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
}

build() {
  say "Building the Lambda package..."
  rm -rf "$ROOT/build" && mkdir -p "$ROOT/build/lambda"
  cp -r "$ROOT/src/watchkeeper" "$ROOT/src/handlers" "$ROOT/build/lambda/"
  python3 -m pip install -q -r "$ROOT/requirements.txt" -t "$ROOT/build/lambda" --no-compile \
    || die "pip failed; check that python3 -m pip works in this shell"
  find "$ROOT/build/lambda" -name '__pycache__' -prune -exec rm -rf {} +
}

tf_init() {
  command -v terraform >/dev/null || die "run './scripts/cloudshell.sh setup' first"
  local bucket
  bucket="$(stack_output StateBucketName)"
  [ -n "$bucket" ] && [ "$bucket" != "None" ] || die "run './scripts/cloudshell.sh bootstrap' first"
  export TF_IN_AUTOMATION=1
  export TF_VAR_aws_region="$REGION"
  export TF_VAR_permissions_boundary_name="${PROJECT}-lambda-boundary"
  export TF_VAR_alert_email="${ALERT_EMAIL:-}"
  export TF_VAR_enable_demo="${ENABLE_DEMO:-true}"
  export TF_VAR_enable_triage="${ENABLE_TRIAGE:-false}"
  (cd "$ROOT/terraform" && terraform init -input=false -reconfigure \
    -backend-config="bucket=${bucket}" \
    -backend-config="key=${PROJECT}/terraform.tfstate" \
    -backend-config="region=${REGION}" \
    -backend-config="encrypt=true" \
    -backend-config="use_lockfile=true" >/dev/null)
}

cmd_deploy() {
  build
  tf_init
  say "Planning (nothing changes yet)..."
  (cd "$ROOT/terraform" && terraform plan -input=false -out=tfplan)
  read -r -p $'\nApply this plan? Type yes to continue: ' answer
  [ "$answer" = "yes" ] || { echo "Cancelled. Nothing was changed."; return; }
  (cd "$ROOT/terraform" && terraform apply -input=false tfplan && rm -f tfplan && terraform output)
}

cmd_destroy() {
  build
  tf_init
  (cd "$ROOT/terraform" && terraform destroy -input=false)
}

cmd_outputs() { tf_init; (cd "$ROOT/terraform" && terraform output); }

cmd_status() {
  say "Alarms"
  aws cloudwatch describe-alarms --alarm-name-prefix "${PROJECT}-" \
    --query 'MetricAlarms[].[AlarmName,StateValue,StateUpdatedTimestamp]' --output table
  say "Demo"
  printf 'fault mode: %s\n' "$(aws ssm get-parameter --name "/${PROJECT}/demo/fault_mode" --query Parameter.Value --output text 2>/dev/null || echo '(demo off)')"
  printf 'demo cron:  %s\n' "$(aws events describe-rule --name "${PROJECT}-demo-cron" --query State --output text 2>/dev/null || echo '(demo off)')"
}

cmd_fault() {
  local mode="${1:-}"
  case "$mode" in
    none|error|slow|stale|down|cron_fail) ;;
    *) die "usage: fault none|error|slow|stale|down|cron_fail" ;;
  esac
  aws ssm put-parameter --name "/${PROJECT}/demo/fault_mode" --value "$mode" --overwrite >/dev/null
  echo "Demo fault mode is now '${mode}' (takes effect within about 10 seconds)."
}

cmd_test_alert() {
  local topic msg
  topic="$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':${PROJECT}-alerts')].TopicArn" --output text)"
  [ -n "$topic" ] || die "alerts topic not found; deploy first"
  msg='{"AlarmName":"'"$PROJECT"'-test","AlarmDescription":"Test alert from CloudShell","NewStateValue":"ALARM","OldStateValue":"OK","NewStateReason":"manual test","Trigger":{"Dimensions":[{"name":"Check","value":"test"}]}}'
  aws sns publish --topic-arn "$topic" --message "$msg" >/dev/null
  echo "Test alert sent. Check Slack (or the notifier logs if Slack is not set up)."
}

case "${1:-help}" in
  setup) cmd_setup ;;
  bootstrap) cmd_bootstrap ;;
  deploy) cmd_deploy ;;
  destroy) cmd_destroy ;;
  outputs) cmd_outputs ;;
  status) cmd_status ;;
  fault) cmd_fault "${2:-}" ;;
  pause-cron) aws events disable-rule --name "${PROJECT}-demo-cron" && echo "Demo cron paused." ;;
  resume-cron) aws events enable-rule --name "${PROJECT}-demo-cron" && echo "Demo cron resumed." ;;
  run-checker)
    aws lambda invoke --function-name "${PROJECT}-checker" --cli-binary-format raw-in-base64-out \
      --payload '{"source":"cloudshell"}' /tmp/checker-out.json >/dev/null && cat /tmp/checker-out.json && echo ;;
  test-alert) cmd_test_alert ;;
  logs) aws logs tail "/aws/lambda/${PROJECT}-checker" --follow --since 10m ;;
  *) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
