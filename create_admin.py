#!/usr/bin/env python3
"""
Create an admin client for Claude Code API Server.

Usage:
    python create_admin.py [CLIENT_ID] [--description "Description"]

If CLIENT_ID is omitted, defaults to "admin".
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.auth import AuthManager
from src.config import get_settings, ensure_directories
from src.models import ClientRole


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create an admin client for Claude Code API Server",
    )
    parser.add_argument(
        "client_id",
        nargs="?",
        default="admin",
        help="Admin client ID (default: admin)",
    )
    parser.add_argument(
        "--description", "-d",
        default="",
        help="Client description",
    )
    args = parser.parse_args()

    settings = get_settings()
    ensure_directories(settings)
    auth_manager = AuthManager(settings.clients_file)

    try:
        api_key = auth_manager.add_client(
            args.client_id, args.description, role=ClientRole.ADMIN
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print()
    print("=" * 60)
    print("Admin client created successfully!")
    print("=" * 60)
    print()
    print(f"  Client ID:   {args.client_id}")
    print(f"  API Key:     {api_key}")
    print()
    print("=" * 60)
    print("IMPORTANT: Save this API key securely!")
    print("It cannot be retrieved later.")
    print("=" * 60)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
