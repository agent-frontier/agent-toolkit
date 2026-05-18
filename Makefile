# Makefile — convenience wrappers for common dev tasks.
#
# The toolkit has no build step; this is purely a discoverability layer
# over the existing scripts (`tests/*.sh`, `demos/tour.sh`, etc.) so
# contributors and CI can run `make smoke` instead of remembering each
# harness path. Every target shells out to an existing script — there is
# no Make-specific logic here, and removing this file leaves the project
# fully usable.

SHELL := /bin/bash

TOOLS := system-janitor.sh system-updater.sh cert-watch.sh log-shipper.sh
SMOKE_HARNESSES := \
	tests/smoke.sh \
	tests/updater-smoke.sh \
	tests/cert-watch-smoke.sh \
	tests/log-shipper-smoke.sh \
	tests/install-smoke.sh \
	tests/bootstrap-smoke.sh \
	tests/capabilities-check.sh

.PHONY: help test smoke lint syntax shellcheck mcp-test demo tour clean-demo-workspaces all

help: ## Show this help (default target)
	@awk 'BEGIN{FS=":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

all: lint test ## Run lint + full test suite

test: smoke mcp-test ## Run smoke harnesses + MCP server pytest

smoke: ## Run every Bash smoke harness in sequence
	@set -e; for h in $(SMOKE_HARNESSES); do \
		echo "── $$h ──"; \
		./$$h; \
	done
	@echo "── all smoke harnesses passed ──"

lint: syntax shellcheck ## bash -n + shellcheck across every tool

syntax: ## bash -n on every shipped tool
	@for t in $(TOOLS); do echo "bash -n $$t"; bash -n $$t; done
	@for f in tests/*.sh demos/*.sh scripts/*.sh install.sh; do \
		[ -f "$$f" ] || continue; bash -n "$$f"; \
	done

shellcheck: ## shellcheck across every shell file (warning severity, matches CI)
	@command -v shellcheck >/dev/null || { echo "[skip] shellcheck not installed"; exit 0; }
	@shellcheck -S warning $(TOOLS)
	@for f in tests/*.sh demos/*.sh scripts/*.sh install.sh; do \
		[ -f "$$f" ] || continue; shellcheck -S warning "$$f"; \
	done

mcp-test: ## Run pytest against the MCP server
	@cd mcp-server && pytest -q

demo: ## Run the full demo tour (cleanup, health, discovery)
	@./demos/tour.sh

tour: demo ## Alias for `demo`

clean-demo-workspaces: ## Remove leftover demo temp dirs
	@rm -rf demos/.workspaces && echo "removed demos/.workspaces"
