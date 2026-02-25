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

# Run with Docker Compose (recommended — includes required security opts for bwrap)
docker compose up -d
```

> **Note:** Running via `docker run` requires `--security-opt` flags (`apparmor:unconfined`, `seccomp=seccomp-bwrap.json`, `systempaths=unconfined`) for bwrap sandboxing to work. See `docker-compose.yml` for the full configuration and [Security — Running bwrap Inside Docker](security-model.md#running-bwrap-inside-docker) for details.

**Corporate proxy environments:** If outbound internet access requires a forward proxy, set `CCAS_UPSTREAM_HTTPS_PROXY` (and optionally `CCAS_UPSTREAM_HTTP_PROXY`). See [Security — Upstream Proxy Support](security-model.md#upstream-proxy-support) for details.

---

## Option B: Local Python

```bash
# Requirements (see Dockerfile for tested versions)
# - Python, Node.js, git
# - bubblewrap (bwrap) for process-level sandboxing
# - socat for network isolation (proxy bridging inside sandbox)

# Install
pip install -r requirements.txt
sudo apt-get install bubblewrap socat git  # Required for sandbox, network isolation, and MCP packages
npm install -g @anthropic-ai/claude-code @anthropic-ai/sandbox-runtime  # CLI + seccomp artifacts

# Run
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

See [Configuration](configuration.md) for environment variables.

---

## Option C: systemd Service

The recommended approach is a `docker compose`-based systemd unit (see `deploy/deploy.yml` for the full Ansible playbook):

```ini
# /etc/systemd/system/ccas.service
[Unit]
Description=Claude Code API Server
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=ccas
Group=ccas
WorkingDirectory=/opt/ccas
ExecStartPre=/usr/bin/docker compose build
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose up -d --force-recreate
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
```
