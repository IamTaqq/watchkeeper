PROJECT ?= watchkeeper
REGION  ?= ca-central-1
OWNER   ?= IamTaqq
PYTHON  ?= python3
TF      ?= terraform
VENV    := .venv
PY      := $(VENV)/bin/python

.PHONY: help setup test validate local build plan deploy destroy outputs token \
        fault pause-cron resume-cron run-checker logs clean simulate tools eval lint bootstrap backend-config

help: ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv with test dependencies
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install -q -r requirements-dev.txt

test: ## Run unit tests (Python) and plan tests (Terraform, no AWS needed)
	$(PY) -m pytest -q
	cd terraform && $(TF) init -backend=false -input=false >/dev/null && $(TF) test

validate: ## Check checks.yaml for mistakes
	$(PY) scripts/validate_config.py checks.yaml

local: ## Run your checks once from this machine (no AWS)
	$(PY) scripts/run_local.py checks.yaml

build: ## Assemble the Lambda package in build/lambda
	rm -rf build && mkdir -p build/lambda
	cp -r src/watchkeeper src/handlers build/lambda/
	$(PY) -m pip install -q -r requirements.txt -t build/lambda --no-compile
	find build/lambda -name '__pycache__' -prune -exec rm -rf {} +

lint: ## Lint the bootstrap template and GitHub workflows
	$(VENV)/bin/cfn-lint bootstrap/bootstrap.yaml
	$(VENV)/bin/actionlint .github/workflows/*.yml

bootstrap: ## One-time AWS setup (OIDC role, state bucket, boundary). Usually run in CloudShell instead.
	aws cloudformation deploy --region $(REGION) --stack-name $(PROJECT)-bootstrap \
		--template-file bootstrap/bootstrap.yaml --capabilities CAPABILITY_NAMED_IAM \
		--parameter-overrides GitHubOwner=$(OWNER) RepoName=$(PROJECT) ProjectName=$(PROJECT)
	aws cloudformation describe-stacks --region $(REGION) --stack-name $(PROJECT)-bootstrap \
		--query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

backend-config: ## Write terraform/backend.hcl from the bootstrap stack (for local deploys)
	@BUCKET=$$(aws cloudformation describe-stacks --region $(REGION) --stack-name $(PROJECT)-bootstrap \
		--query "Stacks[0].Outputs[?OutputKey=='StateBucketName'].OutputValue" --output text); \
	printf 'bucket       = "%s"\nkey          = "%s/terraform.tfstate"\nregion       = "%s"\nencrypt      = true\nuse_lockfile = true\n' \
		"$$BUCKET" "$(PROJECT)" "$(REGION)" > terraform/backend.hcl && cat terraform/backend.hcl

plan: validate build ## Preview changes (needs terraform/backend.hcl)
	cd terraform && $(TF) init -input=false -backend-config=backend.hcl >/dev/null && $(TF) plan

deploy: validate build ## Create/update everything in AWS from this machine (CI does this on push)
	cd terraform && $(TF) init -input=false -backend-config=backend.hcl >/dev/null && $(TF) apply

destroy: ## Delete everything this project created
	cd terraform && $(TF) destroy

outputs: ## Show URLs and names
	cd terraform && $(TF) output

token: ## Print the heartbeat token
	@aws ssm get-parameter --name /$(PROJECT)/heartbeat_token --with-decryption --query Parameter.Value --output text

fault: ## Break the demo app: make fault MODE=none|error|slow|stale|down|cron_fail
	@test -n "$(MODE)" || (echo "usage: make fault MODE=error" && exit 1)
	aws ssm put-parameter --name /$(PROJECT)/demo/fault_mode --value $(MODE) --overwrite >/dev/null
	@echo "demo fault mode is now '$(MODE)' (takes effect within ~10s)"

pause-cron: ## Stop the demo cron job (its heartbeat goes overdue)
	aws events disable-rule --name $(PROJECT)-demo-cron

resume-cron: ## Start the demo cron job again
	aws events enable-rule --name $(PROJECT)-demo-cron

run-checker: ## Run the checker once now instead of waiting for the schedule
	aws lambda invoke --function-name $(PROJECT)-checker --cli-binary-format raw-in-base64-out \
		--payload '{"source":"manual"}' /dev/stdout

logs: ## Tail the checker's logs
	aws logs tail /aws/lambda/$(PROJECT)-checker --follow --since 10m

SCENARIO ?= bad-deploy
MODEL    ?= claude-sonnet-5-5
MODELS   ?= $(MODEL)
REPEAT   ?= 1

tools: ## Show what the agent's tools return for a scenario (free): make tools SCENARIO=cron-disabled
	$(PY) scripts/simulate.py scenarios/$(SCENARIO).yaml --tools-only

simulate: ## AI-investigate a recorded incident (needs ANTHROPIC_API_KEY): make simulate SCENARIO=bad-deploy
	$(PY) scripts/simulate.py scenarios/$(SCENARIO).yaml --model $(MODEL) --verbose

eval: ## Grade the agent on all scenarios: make eval MODELS="claude-haiku-5-5 claude-sonnet-5-5" REPEAT=3
	$(PY) scripts/evaluate.py --models $(MODELS) --repeat $(REPEAT)

clean: ## Remove build output
	rm -rf build
