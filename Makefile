.PHONY: dev test lint typecheck build

dev:
	pip install -e ".[dev]"
	pre-commit install

test:
	pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=80

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

typecheck:
	mypy src/ --strict

build:
	python -m build
