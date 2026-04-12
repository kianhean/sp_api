# Contributing

## Setup

```bash
uv sync --group dev
```

## Common Commands

Run tests:

```bash
uv run pytest
```

Run lint:

```bash
uv run ruff check .
```

Run the manual E2E flow:

```bash
uv run sp-services-e2e
```

## Development Guidelines

- Keep changes focused and small.
- Prefer deterministic tests over live network tests.
- Add or update tests for parser changes.
- Preserve current public models unless a breaking change is intentional.
- Document user-visible behavior changes in `README.md` and `CHANGELOG.md`.

## Pull Requests

Please include:

- what changed
- why it changed
- how it was tested
- any upstream SP/Auth0 assumptions or risks

## Reporting Problems

- Use GitHub Issues for bugs and feature requests.
- Use `SECURITY.md` for private vulnerability reporting.
