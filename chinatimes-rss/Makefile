PY ?= .venv/bin/python
CTIME = $(PY) -m ctime_rss.cli

.PHONY: check lint format test validate build-fixtures

check:
	.venv/bin/ruff check src tests
	.venv/bin/ruff format --check src tests
	$(PY) -m pytest -q
	$(MAKE) build-fixtures
	$(CTIME) --config-dir config.test build --fixture-dir fixtures --fulltext
	$(CTIME) --config-dir config.test validate

lint:
	.venv/bin/ruff check src tests --fix

format:
	.venv/bin/ruff format src tests

test:
	$(PY) -m pytest -q

validate:
	$(CTIME) --config-dir config.test validate

build-fixtures:
	$(PY) tests/make_fixtures.py > /dev/null
