"""Entry point for the CCAS manager CLI."""

from __future__ import annotations

import sys

from ..api import ApiError
from .commands import build_parser
from .config import resolve_config
from ..formatters import Console


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    # Track if key was passed via --key for security warning
    if getattr(args, "key", None):
        sys._ccas_key_from_flag = True  # type: ignore[attr-defined]

    # Resolve config
    config = resolve_config(args)

    # Create console
    console = Console(no_color=config.no_color, json_mode=config.json_mode)

    try:
        args.func(args, config, console)
    except ApiError as e:
        if config.json_mode:
            console.json_output({"error": e.detail, "status_code": e.status_code})
        else:
            console.error(f"API error ({e.status_code}): {e.detail}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.error("Interrupted.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        if config.json_mode:
            console.json_output({"error": str(e)})
        else:
            console.error(f"Unexpected error: {e}")
            if config.verbose:
                import traceback
                traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
