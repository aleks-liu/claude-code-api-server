#!/bin/sh
set -e

# When a volume is mounted to /data, it may be owned by root.
# Fix ownership so the non-root appuser can write to it.
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/auth /data/jobs /data/uploads /data/mcp
    chown -R appuser:appuser /data
    # Drop privileges and re-exec as appuser
    exec gosu appuser "$@"
else
    # Already running as appuser (no volume permission issue)
    exec "$@"
fi
