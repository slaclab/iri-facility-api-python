PYTHON      := python3.13
VENV        := .venv
BIN         := $(VENV)/bin
UV          := uv
PIP         := $(BIN)/pip
LOG_FILE    := runtime-logs.log
IRI_LOG_FILE ?= $(LOG_FILE)
LOG_ROTATION_DAYS := 5
IRI_LOG_ROTATION_DAYS ?= $(LOG_ROTATION_DAYS)

STAMP_VENV  := $(VENV)/.created
STAMP_DEPS  := $(VENV)/.deps

.DEFAULT_GOAL := dev

$(STAMP_VENV):
	$(UV) venv $(VENV)
	touch $(STAMP_VENV)

.venv: $(STAMP_VENV)

$(STAMP_DEPS): $(STAMP_VENV) pyproject.toml
	$(UV) pip install --python $(BIN)/python -e .
	$(UV) pip install --python $(BIN)/python \
		ruff \
		pylint \
		bandit \
		pytest
	touch $(STAMP_DEPS)

deps: $(STAMP_DEPS)

dev: deps
	@source $(BIN)/activate && \
	[ -f local.env ] && source local.env || true && \
	IRI_API_ADAPTER_facility=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_status=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_account=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_compute=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_filesystem=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_storage=app.demo_adapter.DemoAdapter \
	IRI_API_ADAPTER_task=app.demo_adapter.DemoAdapter \
	IRI_LOG_FILE="$${IRI_LOG_FILE:-$${LOG_FILE:-$(IRI_LOG_FILE)}}" \
	IRI_LOG_ROTATION_DAYS="$${IRI_LOG_ROTATION_DAYS:-$${LOG_ROTATION_DAYS:-$(IRI_LOG_ROTATION_DAYS)}}" \
	DEMO_QUEUE_UPDATE_SECS=2 \
	OPENTELEMETRY_ENABLED=true \
	API_URL_ROOT='http://localhost:8000' fastapi dev

dev-s3df: deps
	@source $(BIN)/activate && \
	IRI_API_ADAPTER_account=app.s3df.account_adapter.S3DFAccountAdapter \
	IRI_API_ADAPTER_status=app.s3df.status_adapter.S3DFStatusAdapter \
	COACT_API_URL='https://coact-dev.slac.stanford.edu/graphql-service-dev' \
	IRI_SHOW_MISSING_ROUTES='true' \
	API_URL_ROOT='http://127.0.0.1:8000' fastapi dev

# --- Docker / GHCR targets ---
GHCR_USERNAME ?= slaclab
GHCR_IMAGE ?= ghcr.io/$(GHCR_USERNAME)/iri-s3df
IMAGE_TAG  ?= prod-07132026

docker-build:
	docker build --platform linux/amd64 -t $(GHCR_IMAGE):$(IMAGE_TAG) .

docker-push: docker-build
	docker push $(GHCR_IMAGE):$(IMAGE_TAG)

docker-test-coact:
	@docker run --rm -it \
		-e COACT_SERVICE_PASSWORD \
		$(GHCR_IMAGE):$(IMAGE_TAG) \
		python -m app.s3df.clients.example

REDIS_PORT      ?= 6379
REDIS_CONTAINER := iri-redis

redis: ## Start a local Redis container for idempotency (dev only)
	docker run -d --name $(REDIS_CONTAINER) -p $(REDIS_PORT):6379 redis:7-alpine 2>/dev/null || \
		docker start $(REDIS_CONTAINER) 2>/dev/null || true
	@echo "Redis running on localhost:$(REDIS_PORT)"
	@echo "Add to local.env:"
	@echo "  export REDIS_URL=redis://localhost:$(REDIS_PORT)"
	@echo "  export IDEMPOTENCY_TTL_SECONDS=86400  # cache TTL (default: 24h)"
	@echo "  export LOCK_TTL_SECONDS=60            # in-flight lock TTL (default: 60s)"

redis-stop: ## Stop the local Redis container
	docker stop $(REDIS_CONTAINER) 2>/dev/null || true

redis-clean: ## Stop and remove the local Redis container
	docker rm -f $(REDIS_CONTAINER) 2>/dev/null || true

.PHONY: clean
clean:
	rm -rf iri_sandbox
	rm -rf .venv

# Format and lint
format: deps
	$(BIN)/ruff format --line-length 200 .

ruff: deps
	$(BIN)/ruff check . --fix || true

pylint: deps
	find . -path ./$(VENV) -prune -o -type f -name "*.py" -print0 | while IFS= read -r -d '' f; do \
		echo "Pylint $$f"; \
		$(BIN)/pylint $$f --rcfile pylintrc || true; \
	done

# Security
audit: deps
	uv pip compile pyproject.toml -o requirements.txt
	uv pip sync requirements.txt
	uv pip install pip-audit
	$(BIN)/pip-audit || true
	rm -f requirements.txt

bandit: deps
	$(BIN)/bandit -r app || true

test: deps
	$(UV) pip install --python $(BIN)/python -e ".[dev]"
	$(BIN)/python -m pytest test/ -v

# Full validation bundle
lint: clean format ruff pylint audit bandit

globus: deps
	@source local.env && $(BIN)/python ./tools/globus.py

ping: deps
	@source local.env && $(BIN)/python ./tools/ping.py

ARGS ?=

# call it via: make manage-globus ARGS=scopes-show
manage-globus: deps
	@source local.env && $(BIN)/python ./tools/manage_globus.py $(ARGS)
