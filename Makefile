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

.PHONY: test test-unit lint fmt hooks
test: .env ## Run the backend tests with the domain coverage gate (starts Postgres and Redis)
	$(COMPOSE) up --detach --wait postgres redis
	cd backend && $(TEST_ENV) uv run pytest --cov=relay.domain --cov-report=term --cov-fail-under=90

test-unit: ## Run only the tests that need no running services
	cd backend && uv run pytest tests/unit tests/mcp_server

lint: frontend/node_modules ## Lint and type-check the backend and the frontend
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && npm run lint && npm run typecheck

fmt: ## Format and auto-fix the backend
	cd backend && uv run ruff format . && uv run ruff check --fix .

hooks: ## Install the pre-commit git hooks
	$(PRE_COMMIT) install

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

# ---------------------------------------------------------------- later phases

.PHONY: smoke eval eval-quick eval-selftest eval-label demo-reset
smoke: ## Live end-to-end check with a real API key (Phase 3)
	@$(call not_yet,3)
eval: ## Full eval suite, k=3, Markdown + JSON report (Phase 4)
	@$(call not_yet,4)
eval-quick: ## The CI eval subset, one trial each (Phase 4)
	@$(call not_yet,4)
eval-selftest: ## Eval harness self-tests, no model calls (Phase 4)
	@$(call not_yet,4)
eval-label: ## Hand-label transcripts to validate the judge (Phase 7)
	@$(call not_yet,7)
demo-reset: ## What the nightly demo reset runs (Phase 8)
	@$(call not_yet,8)

define not_yet
echo "make $@ is not implemented yet: it arrives in Phase $(1) (PLAN.md §13)." >&2; exit 1
endef

# ---------------------------------------------------------------- files

.env:
	cp .env.example .env
	@echo "Created .env from .env.example. Add ANTHROPIC_API_KEY when Phase 3 needs it."

frontend/node_modules: frontend/package-lock.json
	cd frontend && npm ci --no-audit --no-fund
	@touch $@
