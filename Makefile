# Common tasks. `make help` lists them.
VENV_APP ?= .venv-app
VENV_HA  ?= .venv-ha

.DEFAULT_GOAL := help
.PHONY: help setup lint types test e2e ui fuzz docs-check dev demo screenshots check clean

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create both virtualenvs (Python 3.13) and install everything
	python3.13 -m venv $(VENV_APP) && $(VENV_APP)/bin/pip install -q -e "app[dev]"
	python3.13 -m venv $(VENV_HA) && $(VENV_HA)/bin/pip install -q -r integration/requirements_test.txt PyTurboJPEG av numpy pytest-timeout mypy -e app

lint: ## ruff for the app, integration and scripts
	cd app && ../$(VENV_APP)/bin/ruff check . && ../$(VENV_APP)/bin/ruff format --check .
	cd integration && ../$(VENV_HA)/bin/ruff check --config pyproject.toml . ../custom_components && ../$(VENV_HA)/bin/ruff format --check --config pyproject.toml . ../custom_components

types: ## mypy --strict for the app and the integration
	cd app && ../$(VENV_APP)/bin/mypy
	cd integration && ../$(VENV_HA)/bin/mypy

test: ## Unit, component and integration tests with coverage gates
	cd app && ../$(VENV_APP)/bin/pytest --cov
	cd integration && ../$(VENV_HA)/bin/pytest --cov
	$(VENV_APP)/bin/pytest scripts -q

e2e: ## Fake camera -> real app -> real integration inside Home Assistant
	cd e2e && ../$(VENV_HA)/bin/pytest --timeout=120

ui: ## Playwright browser tests for the panel (needs node + playwright + ffmpeg)
	node e2e/ui/panel.cjs

fuzz: ## A much deeper property-based search
	cd app && DASHCAM_FUZZ=1 ../$(VENV_APP)/bin/pytest tests/test_properties.py -p no:randomly

docs-check: ## Links, screenshots and API/option documentation drift
	$(VENV_APP)/bin/pytest scripts/tests/test_docs.py -q
	cd app && ../$(VENV_APP)/bin/pytest tests/test_packaging.py -q

dev: ## Run the daemon against a fake camera (panel on :8099, API on :8100)
	$(VENV_APP)/bin/python scripts/dev.py

demo: ## Same, with real generated video and thumbnails
	$(VENV_APP)/bin/python scripts/dev.py --demo --fresh

screenshots: ## Regenerate docs/images with the browser tests
	node e2e/ui/panel.cjs --screens docs/images

check: lint types test e2e docs-check ## Everything CI runs, except the browser tests and Docker

clean: ## Remove caches
	find . -name __pycache__ -prune -exec rm -rf {} + ; rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov
