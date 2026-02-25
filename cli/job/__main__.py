"""Entry point for the CCAS client CLI."""

from __future__ import annotations

import sys

from ..api import ApiError
from .commands import build_parser
from ..formatters import Console


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    console = Console(
        no_color=getattr(args, "no_color", False),
        json_mode=getattr(args, "json", False),
    )

    try:
        args.func(args, console)
    except ApiError as e:
        if getattr(args, "json", False):
            console.json_output({"error": e.detail, "status_code": e.status_code})
        else:
            console.error(f"API error ({e.status_code}): {e.detail}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.spinner_clear()
        console.error("Interrupted.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        if getattr(args, "json", False):
            console.json_output({"error": str(e)})
        else:
            console.error(f"Unexpected error: {e}")
            if getattr(args, "verbose", False):
                import traceback
                traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
