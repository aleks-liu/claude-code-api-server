# Installation

[← Back to README](../README.md)

## Table of Contents

- [Option A: Docker (Recommended)](#option-a-docker-recommended)
- [Option B: Local Python](#option-b-local-python)
- [Option C: systemd Service](#option-c-systemd-service)

---

## Option A: Docker (Recommended)

```bash
# Build image
docker build -t claude-code-api-server .

# Run with Docker
docker run -d \
  --name claude-code-api \
  -p 8000:8000 \
  -v claude_data:/data \
  claude-code-api-server

# Or use Docker Compose
docker-compose up -d
```

**Corporate proxy environments:** If outbound internet access requires a forward proxy, set `CCAS_UPSTREAM_HTTPS_PROXY` (and optionally `CCAS_UPSTREAM_HTTP_PROXY`). See [Security — Upstream Proxy Support](security-model.md#upstream-proxy-support) for details.

---

## Option B: Local Python

```bash
# Requirements
# - Python 3.11+
# - Node.js 18+ (for Claude Code CLI bundled in SDK)
# - bubblewrap (bwrap) for process-level sandboxing
# - socat for network isolation (proxy bridging inside sandbox)

# Install
pip install -r requirements.txt
sudo apt-get install bubblewrap socat   # Required for sandbox and network isolation
npm install -g @anthropic-ai/claude-code @anthropic-ai/sandbox-runtime  # CLI + seccomp artifacts

# Run
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

---

## Option C: systemd Service

```ini
# /etc/systemd/system/claude-code-api.service
[Unit]
Description=Claude Code API Server
After=network.target

[Service]
Type=simple
User=claude
WorkingDirectory=/opt/claude-code-api
Environment="CCAS_DATA_DIR=/var/lib/claude-code-api-server"
ExecStart=/opt/claude-code-api/venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
