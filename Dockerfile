# Claude Code API Server
# Multi-stage build for smaller final image

# =============================================================================
# Version pins (override with --build-arg at build time)
# =============================================================================
ARG PYTHON_VERSION=3.14
ARG NODE_MAJOR=20
ARG CLAUDE_CODE_VERSION=2.1.39
ARG GOSU_VERSION=1.19
ARG SANDBOX_RUNTIME_VERSION=0.0.37

# =============================================================================
# Build stage
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# =============================================================================
# Runtime stage
# =============================================================================
FROM python:${PYTHON_VERSION}-slim

# Re-declare ARGs needed in this stage (scoping rule)
ARG NODE_MAJOR
ARG CLAUDE_CODE_VERSION
ARG GOSU_VERSION
ARG SANDBOX_RUNTIME_VERSION

# Install runtime system dependencies:
#   - curl, ca-certificates : health checks and HTTPS
#   - git                   : required by some MCP/npm packages during install
#   - bubblewrap            : process-level sandbox (bwrap) for job isolation
#   - Node.js + npm         : Claude Code CLI (bundled in SDK) and npm MCP servers
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    bubblewrap \
    socat \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version \
    && bwrap --version

# Install gosu for dropping root privileges in entrypoint
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://github.com/tianon/gosu/releases/download/${GOSU_VERSION}/gosu-${arch}" -o /usr/local/bin/gosu \
    && chmod +x /usr/local/bin/gosu \
    && gosu --version

# Install Claude Code CLI globally (required by the Agent SDK at runtime)
# Install sandbox-runtime for seccomp BPF binaries (apply-seccomp + unix-block.bpf)
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version \
    && npm install -g @anthropic-ai/sandbox-runtime@${SANDBOX_RUNTIME_VERSION} \
    && mkdir -p /opt/ccas \
    && ln -sf "$(npm root -g)/@anthropic-ai/sandbox-runtime/vendor/seccomp" /opt/ccas/seccomp \
    && test -x /opt/ccas/seccomp/x64/apply-seccomp || echo "WARN: seccomp binary not found after symlink"

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Create data directory tree (auth, jobs, uploads, mcp)
# All owned by appuser so the application can write to them at runtime.
RUN mkdir -p /data/auth /data/jobs /data/uploads /data/mcp \
    && chown -R appuser:appuser /data

# Set working directory
WORKDIR /app

# Copy application code and admin bootstrap script
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser create_admin.py ./create_admin.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Environment defaults
ENV CCAS_HOST=0.0.0.0 \
    CCAS_PORT=8000 \
    CCAS_DATA_DIR=/data \
    CCAS_DEBUG=false \
    CCAS_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/v1/health || exit 1

# Entry point — runs as root to fix volume permissions, then drops to appuser
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
