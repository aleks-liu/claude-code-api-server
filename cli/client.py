#!/usr/bin/env python3
"""CCAS Client — CLI for submitting jobs to Claude Code API Server.

Usage:
    python cli/client.py [command] [options]
    python -m cli.job [command] [options]
"""
import os
import sys

# Ensure project root is on sys.path so 'cli' package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.job.__main__ import main

if __name__ == "__main__":
    main()
