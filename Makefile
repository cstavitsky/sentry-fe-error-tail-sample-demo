# Usage: cp .env.example .env  (fill it in)  ->  make up  ->  make demo
COMPOSE ?= docker compose
MODE ?= otlp
N ?= 500

.PHONY: up down demo scenario perf mixed fate logs dirs be-mode export-customer

dirs:
	@mkdir -p out/collector out/loadgen

up: dirs ## build + start the stack (UI http://localhost:8080, Jaeger http://localhost:16686)
	$(COMPOSE) up -d --build

down: ## stop and remove everything (incl. the Postgres volume)
	$(COMPOSE) down -v

demo: dirs ## run every scenario once through the browser
	$(COMPOSE) run --rm loadgen demo

scenario: dirs ## make scenario S=S2 COUNT=3
	$(COMPOSE) run --rm loadgen scenario $(S) $(or $(COUNT),1)

# Recreate the backend in the given tracing mode (otlp | sentry). nginx re-resolves
# the backend's IP through Docker DNS, so it keeps working after the recreate.
be-mode:
	BE_TRACING_MODE=$(MODE) $(COMPOSE) up -d --force-recreate --no-deps backend
	@sleep 8

perf: dirs ## AC6: make perf MODE=otlp|sentry  (N+1 x10, Slow DB x120); always ends in otlp mode
	@$(MAKE) --no-print-directory be-mode MODE=$(MODE)
	BE_TRACING_MODE=$(MODE) $(COMPOSE) run --rm loadgen perf; status=$$?; \
	if [ "$(MODE)" != "otlp" ]; then echo "restoring BE_TRACING_MODE=otlp"; $(MAKE) --no-print-directory be-mode MODE=otlp; fi; \
	exit $$status

mixed: dirs ## S11: make mixed N=500
	$(COMPOSE) run --rm loadgen mixed $(N)

fate: ## per-trace outcome (kept for Sentry vs trace store only): make fate RUN=demo|mixed
	python3 scripts/trace_fate.py --run $(or $(RUN),demo)

logs: ## follow logs of all services
	$(COMPOSE) logs -f --tail=100

export-customer: ## write ./dist-customer/: the repo minus internal files (SPEC, reviews, research, test fakes)
	sh scripts/export_customer.sh dist-customer
