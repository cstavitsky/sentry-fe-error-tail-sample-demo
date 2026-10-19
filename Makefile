# Usage: cp .env.example .env  (fill it in)  ->  make up  ->  make demo
COMPOSE ?= docker compose
MODE ?= otlp
N ?= 500
# Where browser spans go (see frontend/src/sentry.js): otlp (default) | sentry.
# Resolved once here and exported, so EVERY compose build (incl. the rebuilds that
# `run --build loadgen` does for nginx) uses the same mode. Precedence:
# FE_SPANS=... on the command line / in the shell > .fe_spans (written by `make fe-mode`)
# > FE_SPANS in .env > otlp.
FE_SPANS ?= $(or $(shell cat .fe_spans 2>/dev/null),$(shell sed -n 's/^FE_SPANS=//p' .env 2>/dev/null | tail -1),otlp)
export FE_SPANS
# Django tags sampled browser requests so the collector keeps their BE spans (0 | 1).
# Same precedence: command line > .keep_browser_traces (`make keep-browser ON=..`) > .env > 0.
KEEP_BROWSER_TRACES ?= $(or $(shell cat .keep_browser_traces 2>/dev/null),$(shell sed -n 's/^KEEP_BROWSER_TRACES=//p' .env 2>/dev/null | tail -1),0)
export KEEP_BROWSER_TRACES

.PHONY: up down demo scenario perf mixed fate logs dirs be-mode fe-mode keep-browser export-customer

dirs:
	@mkdir -p out/collector out/loadgen

up: dirs ## build + start the stack (UI http://localhost:8080, Jaeger http://localhost:16686)
	$(COMPOSE) up -d --build
	@echo "frontend built with FE_SPANS=$(FE_SPANS), backend KEEP_BROWSER_TRACES=$(KEEP_BROWSER_TRACES)"

down: ## stop and remove everything (incl. the Postgres volume)
	$(COMPOSE) down -v

demo: dirs ## run every scenario once through the browser
	$(COMPOSE) run --rm --build loadgen demo

scenario: dirs ## make scenario S=S2 COUNT=3
	$(COMPOSE) run --rm --build loadgen scenario $(S) $(or $(COUNT),1)

# Recreate the backend in the given tracing mode (otlp | sentry). nginx re-resolves
# the backend's IP through Docker DNS, so it keeps working after the recreate.
be-mode:
	BE_TRACING_MODE=$(MODE) $(COMPOSE) up -d --force-recreate --no-deps backend
	@sleep 8

# Rebuild the frontend with browser spans going via the collector (otlp, default) or
# straight to Sentry (sentry). The choice is saved in .fe_spans and sticks for every
# later make target until you run `make fe-mode FE=otlp`.
fe-mode: dirs ## make fe-mode FE=sentry|otlp  (sticky)
	@case "$(FE)" in otlp|sentry) ;; *) echo "usage: make fe-mode FE=sentry|otlp"; exit 2;; esac
	@echo $(FE) > .fe_spans
	FE_SPANS=$(FE) $(COMPOSE) up -d --build --no-deps nginx
	@echo "frontend now FE_SPANS=$(FE)"

# Recreate Django with/without the KeepBrowserTracesMiddleware (sticky, like fe-mode).
# Intended together with FE_SPANS=sentry; with FE_SPANS=otlp it would keep EVERY
# browser trace and defeat tail sampling for browser traffic.
keep-browser: ## make keep-browser ON=1|0  (sticky; use with FE_SPANS=sentry)
	@case "$(ON)" in 0|1) ;; *) echo "usage: make keep-browser ON=1|0"; exit 2;; esac
	@echo $(ON) > .keep_browser_traces
	KEEP_BROWSER_TRACES=$(ON) $(COMPOSE) up -d --force-recreate --no-deps backend
	@sleep 8
	@echo "backend now KEEP_BROWSER_TRACES=$(ON) (frontend FE_SPANS=$(FE_SPANS))"

perf: dirs ## AC6: make perf MODE=otlp|sentry  (N+1 x10, Slow DB x120); always ends in otlp mode
	@$(MAKE) --no-print-directory be-mode MODE=$(MODE)
	BE_TRACING_MODE=$(MODE) $(COMPOSE) run --rm --build loadgen perf; status=$$?; \
	if [ "$(MODE)" != "otlp" ]; then echo "restoring BE_TRACING_MODE=otlp"; $(MAKE) --no-print-directory be-mode MODE=otlp; fi; \
	exit $$status

mixed: dirs ## S11: make mixed N=500
	$(COMPOSE) run --rm --build loadgen mixed $(N)

fate: ## per-trace outcome (kept for Sentry vs trace store only): make fate RUN=demo|mixed|perf|scenario
	python3 scripts/trace_fate.py --run $(or $(RUN),demo)

logs: ## follow logs of all services
	$(COMPOSE) logs -f --tail=100

export-customer: ## write ./dist-customer/: the repo minus internal files (SPEC, reviews, research, test fakes)
	sh scripts/export_customer.sh dist-customer
