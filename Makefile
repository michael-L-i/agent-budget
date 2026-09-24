.PHONY: check test
RUN = uv run --project plugins/agent-budget

test:
	$(RUN) pytest tests -q

check:
	$(RUN) ruff check --config plugins/agent-budget/pyproject.toml .
	$(RUN) ruff format --check --config plugins/agent-budget/pyproject.toml .
	$(RUN) pytest tests -q
