# AWS Admin CLI - Guidelines for Claude Code

## Project Overview
Interactive Python CLI/TUI built with Typer, Rich, Questionary, and boto3.
Simulates AWS services against a local LocalStack environment.

## Architecture
- `src/aws_admin_cli/presentation/`: TUI wizards, prompts, and CLI menus.
- `src/aws_admin_cli/infrastructure/`: AWS/LocalStack boto3 client calls and local seeders.
- `src/aws_admin_cli/domain/`: Core entities and business logic.

## Commands
- Run CLI: `poetry run aws-admin`
- Run Tests: `poetry run pytest`
- Run Single Test: `poetry run pytest tests/test_ec2.py`
- Linting/Formatting: `poetry run ruff check .`
- LocalStack Stack: `docker-compose up -d`

## Code Rules & Standards
- **Python-side Filtering:** Never pass `Filters=[...]` to LocalStack boto3 calls (e.g., `describe_security_groups`). Always fetch all and filter in Python memory to prevent LocalStack `InternalError`.
- **Questionary Prompts:** Always map choices as lists of dicts `[{"name": "Label (ID)", "value": "ID"}]` so the user sees clear labels while the code receives pure IDs.
- **Smart Defaults:** If a user confirms an empty selection (e.g., Security Groups), default automatically to the VPC default SG rather than throwing an error.
- **Navigation:** Always handle `<- Back` option gracefully by returning to the previous wizard step state.

## Rules for Claude Code Agent
- Keep modifications minimal and focused on the requested bug or feature.
- Do not add unnecessary comments or rewrite untouched files.
- Always run tests after code modifications to ensure zero regressions.