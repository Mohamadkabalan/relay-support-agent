# Relay developer commands. Run `make` or `make help` to list them.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

# Credentials and host ports come from .env when it exists (see .env.example).
ifneq (,$(wildcard .env))
include .env
endif
POSTGRES_USER ?= relay
POSTGRES_PASSWORD ?= relay-dev-only
POSTGRES_DB ?= relay
POSTGRES_HOST_PORT ?= 55432
REDIS_HOST_PORT ?= 56379
RELAY_HTTP_PORT ?= 8080
PHOENIX_HOST_PORT ?= 6006
SEED ?= 42
MCP_CUSTOMER ?= dana.whitfield@example.com
MCP_INSPECTOR := @modelcontextprotocol/inspector@2.16.0
# The stdio server Claude Desktop and the Inspector launch, inside the running stack.
MCP_STDIO = $(COMPOSE) run --rm -T --no-deps -e RELAY_MCP_CUSTOMER=$(MCP_CUSTOMER) mcp relay mcp serve --transport stdio

COMPOSE := docker compose
COMPOSE_DEV := $(COMPOSE) -f compose.yaml -f compose.dev.yaml
PRE_COMMIT := uvx pre-commit@4.7.0
export NEXT_TELEMETRY_DISABLED := 1

# Host-side tests reach the compose Postgres and Redis through their published ports.
TEST_ENV := DATABASE_URL=postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@127.0.0.1:$(POSTGRES_HOST_PORT)/$(POSTGRES_DB) \
	REDIS_URL=redis://127.0.0.1:$(REDIS_HOST_PORT)/0

.PHONY: help
help: ## List the targets
	@grep -hE '^[a-z][a-zA-Z_-]*:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-15s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack

.PHONY: up down dev logs ps clean
up: .env ## Build and start the stack, and wait until every service is healthy
	$(COMPOSE) up --detach --build --wait
	@echo "Relay: http://localhost:$(RELAY_HTTP_PORT)   Phoenix: http://localhost:$(PHOENIX_HOST_PORT)"

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) down

dev: .env ## Run the stack with hot reload, in the foreground (Ctrl-C stops it)
	$(COMPOSE_DEV) up --build

logs: ## Follow the logs of every service
	$(COMPOSE) logs --follow --tail=100

ps: ## Show service status and health
	$(COMPOSE) ps

clean: ## Stop the stack and delete its data volumes
	$(COMPOSE) down --volumes --remove-orphans

# ---------------------------------------------------------------- quality

.PHONY: test test-unit lint fmt hooks audit
test: .env ## Run the backend tests with the domain coverage gate (starts Postgres and Redis)
	$(COMPOSE) up --detach --wait postgres redis
	cd backend && $(TEST_ENV) uv run pytest --cov=relay.domain --cov-report=term --cov-fail-under=90

test-unit: ## Run only the tests that need no running services
	cd backend && uv run pytest tests/unit tests/mcp_server tests/agent

lint: frontend/node_modules ## Lint and type-check the backend and the frontend
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && npm run lint && npm run typecheck

fmt: ## Format and auto-fix the backend
	cd backend && uv run ruff format . && uv run ruff check --fix .

hooks: ## Install the pre-commit git hooks
	$(PRE_COMMIT) install

audit: frontend/node_modules ## Known vulnerabilities in the locked Python and npm dependencies (CI runs it too)
	cd backend && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
		| uvx pip-audit@2.10.1 --strict --disable-pip --no-deps -r /dev/stdin
	cd frontend && npm audit --omit=dev --audit-level=high

# ---------------------------------------------------------------- data

.PHONY: seed eval-retrieval
seed: .env ## Migrate and seed the database. Idempotent; SEED=42, RESET=1 starts over
	$(COMPOSE) run --rm migrate
	$(COMPOSE) run --rm migrate relay seed --seed $(SEED) $(if $(RESET),--reset) --sample 5

eval-retrieval: ## Score help-center search on the labeled queries; writes evals/retrieval/report.md
	$(COMPOSE) run --rm --no-deps -T -v "$(CURDIR)/evals:/evals:ro" migrate \
		relay kb eval --queries /evals/retrieval/queries.yaml | tee evals/retrieval/report.md

# ---------------------------------------------------------------- MCP server

.PHONY: mcp-inspect mcp-call
mcp-inspect: ## Open the MCP Inspector on the stdio server as MCP_CUSTOMER (Node 22.23+/24.22+)
	MCP_INSPECTOR_SECRET_STORE=memory npx -y $(MCP_INSPECTOR) --web -- $(MCP_STDIO)

mcp-call: ## Call one tool over HTTP as MCP_CUSTOMER: make mcp-call TOOL=get_customer [ARGS='{...}']
	$(COMPOSE) exec -T mcp relay mcp call $(TOOL) '$(or $(ARGS),{})' --as $(MCP_CUSTOMER)

# ---------------------------------------------------------------- agent

.PHONY: chat smoke
chat: ## Chat with the agent in the terminal as MCP_CUSTOMER (needs ANTHROPIC_API_KEY; run make up first)
	$(COMPOSE) run --rm -it --no-deps api relay chat --as $(MCP_CUSTOMER)

# The key reaches the command through the environment, never its command line.
smoke: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
smoke: .env ## One live refund conversation with Claude, checked end to end (needs ANTHROPIC_API_KEY, a few cents)
	$(COMPOSE) up --detach --wait postgres redis
	cd backend && $(TEST_ENV) uv run relay smoke

# ---------------------------------------------------------------- API

.PHONY: approval-flow
approval-flow: ## The approval flow through the HTTP API with curl (needs make up, make seed, ANTHROPIC_API_KEY)
	./docs/examples/approval-flow.sh

# ---------------------------------------------------------------- frontend

PLAYWRIGHT_IMAGE := mcr.microsoft.com/playwright:v1.65.1-noble@sha256:1507cbfc550a63a6b7396791ca4f00df17beb56342d2447b3f106c306f525579

.PHONY: api-types e2e
api-types: ## Regenerate the frontend's typed API client from the backend's OpenAPI document
	cd backend && uv run relay api openapi > ../frontend/lib/api/openapi.json
	cd frontend && npx --yes openapi-typescript@7.13.0 lib/api/openapi.json -o lib/api/schema.d.ts

# The browser smoke test needs no key: the stack runs the scripted demo agent. Playwright runs in
# its official image, which has the browsers and their system libraries. Afterwards `make up`
# puts the stack back on the provider in .env.
e2e: .env frontend/node_modules ## Browser smoke test on the key-free demo agent (run make seed once first; restarts the stack with RELAY_PROVIDER=scripted)
	RELAY_PROVIDER=scripted RELAY_LOGINS_PER_MINUTE=120 $(COMPOSE) up --detach --build --wait
	docker run --rm --network host --ipc=host --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e CI \
		-e E2E_BASE_URL=http://localhost:$(RELAY_HTTP_PORT) -v "$(CURDIR)/frontend:/work" -w /work \
		$(PLAYWRIGHT_IMAGE) npx playwright test

# ---------------------------------------------------------------- evals

# The harness runs on the host, like the tests, against the compose Postgres. Live targets print
# a cost estimate and ask first (YES=1 skips the question). The key travels in the environment.
EVAL := cd backend && $(TEST_ENV) uv run relay eval
EVAL_FLAGS = $(if $(YES),--yes) $(if $(RESUME),--resume $(RESUME))
# The effort sweep and the model comparison run on the regular scenarios unless SUBSET says.
SWEEP_EFFORTS ?= low high
COMPARE_MODELS ?= claude-sonnet-5-5 claude-haiku-5-5

.PHONY: eval eval-quick eval-selftest eval-mechanism eval-scenarios eval-import-drafts
eval: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval: .env ## Run the eval suite with the live agent and the judge: SUBSET=all K=3 [YES=1] [RESUME=dir]
	$(COMPOSE) up --detach --wait postgres redis
	$(EVAL) run --subset $(or $(SUBSET),all) --k $(or $(K),3) $(EVAL_FLAGS)

eval-quick: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-quick: .env ## The CI subset, one trial each, without the retry rule [YES=1]
	$(COMPOSE) up --detach --wait postgres redis
	$(EVAL) run --subset ci --k 1 $(EVAL_FLAGS)

eval-selftest: .env ## Eval harness self-tests: schema, policy, oracle, null agent, errors (no model calls)
	$(COMPOSE) up --detach --wait postgres redis
	$(EVAL) selftest

eval-mechanism: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-mechanism: .env ## Mechanism check: without the confirmation rule, blocked commits must rise [YES=1]
	$(COMPOSE) up --detach --wait postgres redis
	$(EVAL) mechanism $(if $(YES),--yes)

eval-scenarios: ## Regenerate evals/SCENARIOS.md from the scenario files
	cd backend && uv run relay eval list --markdown > ../evals/SCENARIOS.md

eval-import-drafts: .env ## Write the console's valid scenario drafts to evals/scenarios/drafts/ for review
	$(COMPOSE) up --detach --wait postgres
	$(EVAL) import-drafts

.PHONY: eval-sweep eval-models eval-compare eval-gate eval-baseline eval-triage eval-publish
eval-sweep: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-sweep: .env ## Effort sweep: one k=3 run per SWEEP_EFFORTS level (default low high) [SUBSET=regular]
	$(COMPOSE) up --detach --wait postgres redis
	for effort in $(SWEEP_EFFORTS); do \
		$(EVAL) run --subset $(or $(SUBSET),regular) --k $(or $(K),3) --agent-effort $$effort $(EVAL_FLAGS); \
	done

eval-models: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-models: .env ## Model comparison: one k=3 run per COMPARE_MODELS model [SUBSET=regular]
	$(COMPOSE) up --detach --wait postgres redis
	for model in $(COMPARE_MODELS); do \
		$(EVAL) run --subset $(or $(SUBSET),regular) --k $(or $(K),3) --agent-model $$model $(EVAL_FLAGS); \
	done

eval-compare: ## Runs side by side, into docs/EVALS.md: make eval-compare RUNS="evals/reports/A evals/reports/B"
	$(EVAL) compare $(addprefix ../,$(RUNS)) --publish

eval-gate: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-gate: .env ## The CI gate: the CI subset with retries, against evals/baseline.json [YES=1]
	$(COMPOSE) up --detach --wait postgres redis
	$(EVAL) gate $(if $(YES),--yes)

eval-baseline: ## Make a full k=3 run the CI baseline: make eval-baseline RUN=evals/reports/<run>
	$(EVAL) baseline ../$(RUN)

eval-triage: ## Label each failure of a run with its root cause: make eval-triage RUN=evals/reports/<run>
	$(EVAL) triage ../$(RUN)

eval-publish: ## Rewrite the README table and docs/EVALS.md from a run: make eval-publish RUN=evals/reports/<run>
	$(EVAL) publish ../$(RUN)

.PHONY: eval-label eval-agreement eval-judge-check
eval-label: ## Hand-label 30 transcripts of a run for the judge check: make eval-label RUN=evals/reports/<run>
	$(EVAL) label ../$(RUN)

eval-agreement: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-agreement: ## Judge against the hand labels: make eval-agreement LABELS=evals/labels/<run> [REJUDGE=1] [YES=1]
	$(EVAL) agreement ../$(LABELS) --publish $(if $(REJUDGE),--rejudge) $(if $(YES),--yes)

eval-judge-check: export ANTHROPIC_API_KEY := $(ANTHROPIC_API_KEY)
eval-judge-check: .env ## Known negatives the judge must fail (5 model calls) [YES=1]
	$(EVAL) judge-check $(if $(YES),--yes)

# ---------------------------------------------------------------- demo

.PHONY: demo-reset
demo-reset: .env ## What the nightly timer runs: wipe, re-seed for today, sign everyone out (deletes all data)
	$(COMPOSE) run --rm migrate relay demo reset --yes

# ---------------------------------------------------------------- files

.env:
	cp .env.example .env
	@echo "Created .env from .env.example. Add ANTHROPIC_API_KEY for make chat and make smoke."

frontend/node_modules: frontend/package-lock.json
	cd frontend && npm ci --no-audit --no-fund
	@touch $@
