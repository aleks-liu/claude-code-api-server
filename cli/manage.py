#!/usr/bin/env python3
"""CCAS Admin Manager — CLI for managing a Claude Code API Server.

Usage:
    python cli/manage.py [command] [options]
    python -m cli.admin [command] [options]
"""
import os
import sys

# Ensure project root is on sys.path so 'cli' package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.admin.__main__ import main

if __name__ == "__main__":
    main()
