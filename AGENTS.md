# Repository Guidelines

## Environment Setup
- Install Node.js 18+, PNPM (`corepack enable pnpm`), and the `uv` Python tool.
- Run `pnpm install` at the repo root, then `pnpm install:all` to pull backend deps via `uv sync`.
- Copy any `.env.example` files you add into `backend/` or `frontend/`, and document required secrets in the PR.

## Project Structure & Module Organization
- `frontend/` contains the React + Vite client; feature code lives under `frontend/app/` with co-located UI primitives in `frontend/app/components/`.
- `backend/` is a FastAPI service with domain folders (`api/`, `repositories/`, `services/`, `factors/`) and SQLAlchemy models in `models.py`.
- Shared fixtures and regression data sit in `backend/tests/` and `backend/test_ai_trader.json`; keep generated data under `backend/data/`.

## Build, Test, and Development Commands
- `pnpm dev` runs FastAPI (port 5611) and Vite (port 5621) together.
- `pnpm run dev:backend` refreshes `uv` deps and launches `uvicorn`; only use when iterating backend endpoints.
- `pnpm run dev:frontend` starts the Vite dev server.
- `pnpm run build` bundles the frontend and executes the placeholder backend build. Check in a backend build script when it becomes meaningful.

## Coding Style & Naming Conventions
- Python: format with `uv run black backend` and lint with `uv run ruff check backend`; prefer explicit service/repository names and 4-space indentation.
- TypeScript/React: follow functional components in PascalCase, hooks and util files in camelCase, and Tailwind classes grouped by layout → typography → effects.
- Use descriptive module names (e.g., `trades_service.py`) and keep DTOs under `backend/schemas/`.

## Testing Guidelines
- Run backend tests via `uv run pytest backend/tests`; add focused regression fixtures next to the test files.
- Name tests after the behavior under test (`test_matches_signal_window`). Update `backend/TEST_RESULTS.md` when recording significant runs.
- Add snapshot data (e.g., chart baselines) under `frontend/app/__tests__/fixtures/` if you introduce frontend tests.

## Commit & Pull Request Guidelines
- Follow the existing short, imperative commit style (`fix trader retry`, `add candles chart`). Squash noisy commits before merge.
- Reference tickets or issues in the PR description, include API contract changes, and attach screenshots or screencasts for UI updates.
- Request at least one review, ensure `pnpm run build` and `uv run pytest` pass locally, and mention any manual verification steps performed.
