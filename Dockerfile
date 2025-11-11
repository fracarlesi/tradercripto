# =============================================================================
# Stage 1: Frontend Build (T074)
# =============================================================================
FROM node:20-alpine AS frontend-build

WORKDIR /app

# Install pnpm globally (match local version for lockfile compatibility)
RUN npm install -g pnpm@8.15.5

# Copy workspace files first for pnpm workspace detection
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY frontend/package.json ./frontend/

# Install dependencies with frozen lockfile (workspace aware)
RUN pnpm install --frozen-lockfile

# Copy frontend source code
COPY frontend/ ./frontend/

# Build frontend
RUN cd frontend && pnpm run build

# =============================================================================
# Stage 2: Python Dependencies (T075)
# =============================================================================
FROM python:3.13-slim AS python-deps

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy dependency files first for better layer caching
COPY backend/pyproject.toml backend/uv.lock* ./

# Install Python dependencies
RUN uv sync --frozen --no-dev

# =============================================================================
# Stage 3: Runtime (T076)
# =============================================================================
FROM python:3.13-slim AS runtime

# Create non-root user with UID 1000, GID 1000 (T076)
RUN groupadd -g 1000 appuser && \
    useradd -m -u 1000 -g 1000 -s /bin/bash appuser

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy virtual environment from python-deps stage
COPY --from=python-deps --chown=appuser:appuser /app/.venv /app/.venv

# Copy backend application code
COPY --chown=appuser:appuser backend/ ./

# Copy frontend build from frontend-build stage (workspace aware path)
COPY --from=frontend-build --chown=appuser:appuser /app/frontend/dist ./static

# Create data directory for SQLite (with proper permissions)
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

# Set environment variables
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Switch to non-root user (T076)
USER appuser

# Expose port
EXPOSE 5611

# Health check (T077)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5611/api/health').read()"

# Start the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5611"]