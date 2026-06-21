.PHONY: dev test lint typecheck build clean

dev:
	pip install -e ".[dev]"
	pre-commit install

test:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	pre-commit run --all-files
	pytest tests/ -v --cov=src/codepilot --cov-report=term-missing --cov-fail-under=85

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

typecheck:
	mypy src/ --strict

build:
	python -m build

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete 2>/dev/null; true
	rm -rf .mypy_cache .pytest_cache .ruff_cache .coverage build dist *.egg-info src/*.egg-info 2>/dev/null; true
