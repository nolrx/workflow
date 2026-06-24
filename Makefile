# AI Creative Studio — common dev & single-machine deploy tasks.
# Run `make` or `make help` to list targets.
#
# Override the compose binary on older hosts:  make up COMPOSE="docker-compose"

COMPOSE ?= docker compose

.DEFAULT_GOAL := help

# ---- Help -------------------------------------------------------------------
.PHONY: help
help: ## List available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- Docker (single-machine deploy) -----------------------------------------
.PHONY: build fe-agent up down restart deploy redeploy rebuild logs ps config destroy
.PHONY: drain undrain drain-status

# Base url + header for the deploy-control endpoints (override per environment).
CONTROL_URL ?= http://localhost:5001
DEPLOY_HDR   = -H "X-Deploy-Token: $${DEPLOY_CONTROL_TOKEN}"

build: ## Build all images incl. the fe-agent sandbox (--profile setup)
	$(COMPOSE) --profile setup build

fe-agent: ## Build ONLY the fe-agent sandbox image into the host store
	$(COMPOSE) --profile setup build fe-agent

up: ## Start the stack in the background
	$(COMPOSE) up -d

down: ## Stop and remove containers (keeps volumes/data)
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

deploy: build up ## First-time / full deploy: build everything then start

redeploy: ## Graceful backend redeploy: drain -> rebuild -> recreate -> wait ready
	COMPOSE="$(COMPOSE)" DEPLOY_BASE_URL="$(CONTROL_URL)" scripts/deploy-backend.sh

rebuild: ## Recreate from scratch: down, rebuild images, up
	$(COMPOSE) down
	$(COMPOSE) --profile setup build
	$(COMPOSE) up -d

drain: ## Stop the live backend accepting new runs (needs DEPLOY_CONTROL_TOKEN)
	@curl -fsS -X POST $(DEPLOY_HDR) $(CONTROL_URL)/api/admin/lifecycle/drain && echo

undrain: ## Let the live backend accept new runs again
	@curl -fsS -X POST $(DEPLOY_HDR) $(CONTROL_URL)/api/admin/lifecycle/undrain && echo

drain-status: ## Show the live backend's drain state
	@curl -fsS $(DEPLOY_HDR) $(CONTROL_URL)/api/admin/lifecycle/status && echo

logs: ## Tail logs from all services (scope with: make logs S=backend)
	$(COMPOSE) logs -f --tail=200 $(S)

ps: ## Show service status
	$(COMPOSE) ps

config: ## Validate and render the effective compose config
	$(COMPOSE) config

destroy: ## DESTRUCTIVE: down + remove volumes (DROPS the database!)
	$(COMPOSE) down -v

# ---- Local (bare-metal) dev -------------------------------------------------
.PHONY: env setup dev lint test

env: ## Create .env from .env.example if it does not exist
	@test -f .env && echo ".env already exists, leaving it untouched" \
	  || (cp .env.example .env && echo "created .env — now fill in the API keys")

setup: ## Install backend (uv) + frontend (npm) dependencies
	npm run setup

dev: ## Run backend + frontend locally with hot reload
	npm run dev

lint: ## Ruff (backend) + eslint (frontend)
	npm run lint

test: ## Backend unit tests only (skips live-API integration tests)
	uv run pytest -m "not integration"
