# Works with GNU make from Git Bash on Windows and from any Linux shell.
# Windows: `winget install ezwinports.make`, then run make from Git Bash.
ifeq ($(OS),Windows_NT)
  BIN := .venv/Scripts
  SYS_PYTHON ?= python
else
  BIN := .venv/bin
  SYS_PYTHON ?= python3
endif
PY := $(BIN)/python

# Drive C on the dev machine is nearly full: caches and Playwright browsers must live next to the repo.
# Windows default: <repo parent>/.cache (D:\hydradb-hack\.cache). Override either variable in the environment.
ifeq ($(OS),Windows_NT)
  CACHE_ROOT ?= $(abspath ../.cache)
  export PIP_CACHE_DIR ?= $(CACHE_ROOT)/pip
  export PLAYWRIGHT_BROWSERS_PATH ?= $(CACHE_ROOT)/ms-playwright
endif

.PHONY: setup check lint types test-unit test-int e2e eval

setup:
	$(SYS_PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev,bench]"
	$(PY) -m playwright install chromium

check: lint types test-unit

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

types:
	$(PY) -m pyright

# Unit + contract tests. The contract suite runs on the fake, and on real HydraDB when HYDRADB_URL is set.
test-unit:
	$(PY) -m pytest tests/unit tests/contract

# Needs HYDRADB_URL and HYDRADB_TOKEN (see .env.example).
test-int:
	$(PY) -m pytest tests/contract tests/integration

# Needs ATLAS_E2E=1 plus a running stack (see tests/e2e/test_smoke.py).
e2e:
	$(PY) -m pytest tests/e2e

eval:
	$(PY) -m eval.run
