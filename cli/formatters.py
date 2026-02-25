"""Console output formatting for CLI tools."""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .admin.models import SyncEntry, SyncStatus

# ANSI codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BOLD_RED = "\033[1;31m"
_BOLD_GREEN = "\033[1;32m"
_BOLD_CYAN = "\033[1;36m"
_BOLD_YELLOW = "\033[1;33m"


class Console:
    """Manages colored console output with spinner and table support."""

    def __init__(self, no_color: bool = False, json_mode: bool = False):
        self._color = not no_color and sys.stderr.isatty()
        self._json = json_mode
        self._is_tty = sys.stderr.isatty()

    # --- Color helpers ---

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    def green(self, text: str) -> str:
        return self._c(_GREEN, text)

    def yellow(self, text: str) -> str:
        return self._c(_YELLOW, text)

    def red(self, text: str) -> str:
        return self._c(_RED, text)

    def cyan(self, text: str) -> str:
        return self._c(_CYAN, text)

    def magenta(self, text: str) -> str:
        return self._c(_MAGENTA, text)

    def bold(self, text: str) -> str:
        return self._c(_BOLD, text)

    def dim(self, text: str) -> str:
        return self._c(_DIM, text)

    # --- Output methods ---

    def success(self, msg: str) -> None:
        if self._json:
            return
        print(f"  {self._c(_BOLD_GREEN, chr(10003))} {self.green(msg)}", file=sys.stderr)

    def error(self, msg: str) -> None:
        if self._json:
            return
        print(f"  {self._c(_BOLD_RED, chr(10007))} {self.red(msg)}", file=sys.stderr)

    def warning(self, msg: str) -> None:
        if self._json:
            return
        print(f"  {self._c(_BOLD_YELLOW, chr(9888))} {self.yellow(msg)}", file=sys.stderr)

    def step(self, msg: str) -> None:
        if self._json:
            return
        print(f"  {self._c(_BOLD_CYAN, chr(8594))} {msg}", file=sys.stderr)

    def detail(self, msg: str) -> None:
        if self._json:
            return
        print(f"    {self.dim(msg)}", file=sys.stderr)

    def progress(self, current: int, total: int, msg: str) -> None:
        if self._json:
            return
        print(f"  [{current}/{total}] {msg}", file=sys.stderr)

    def info(self, msg: str) -> None:
        if self._json:
            return
        print(f"  {msg}", file=sys.stderr)

    def blank(self) -> None:
        if self._json:
            return
        print(file=sys.stderr)

    # --- Spinner (from job client) ---

    def spinner_frame(self, char: str, msg: str) -> None:
        """Write a spinner frame, overwriting the current line on TTY."""
        if self._json:
            return
        if self._is_tty:
            line = f"  {self.cyan(char)} {msg}"
            print(f"\r{line}\033[K", end="", file=sys.stderr, flush=True)

    def spinner_clear(self) -> None:
        """Clear the spinner line."""
        if self._json:
            return
        if self._is_tty:
            print("\r\033[K", end="", file=sys.stderr, flush=True)

    def status_line(self, msg: str) -> None:
        """Print a status line (for non-TTY environments like CI)."""
        if self._json:
            return
        print(f"  {msg}", file=sys.stderr)

    # --- Tables (from admin manager) ---

    def table(self, headers: list[str], rows: list[list[str]],
              align: list[str] | None = None) -> None:
        if self._json:
            return
        if not rows:
            self.detail("(none)")
            return

        col_count = len(headers)
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row[:col_count]):
                widths[i] = max(widths[i], self._visible_len(cell))

        if align is None:
            align = ["l"] * col_count

        def _pad(text: str, width: int, alignment: str) -> str:
            visible = self._visible_len(text)
            padding = width - visible
            if padding <= 0:
                return text
            if alignment == "r":
                return " " * padding + text
            if alignment == "c":
                left = padding // 2
                return " " * left + text + " " * (padding - left)
            return text + " " * padding

        header_line = "  " + "  ".join(
            self.bold(_pad(h, widths[i], align[i])) for i, h in enumerate(headers)
        )
        print(header_line, file=sys.stderr)

        sep = "  " + "  ".join(chr(9472) * w for w in widths)
        print(self.dim(sep), file=sys.stderr)

        for row in rows:
            cells = []
            for i in range(col_count):
                cell = row[i] if i < len(row) else ""
                cells.append(_pad(cell, widths[i], align[i]))
            print("  " + "  ".join(cells), file=sys.stderr)

    def sync_table(self, entries: list[SyncEntry], url: str = "",
                   local_path: str = "") -> None:
        if self._json:
            return

        from .admin.models import SyncStatus

        if url or local_path:
            self.blank()
            parts = []
            if local_path:
                parts.append(f"Local ({local_path})")
            if url:
                parts.append(f"Remote ({url})")
            title = " " + chr(8596) + " ".join(parts) if len(parts) == 2 else " ".join(parts)
            self.info(self.bold(f"Sync Status: {title}"))
            self.blank()

        status_map = {
            SyncStatus.SYNCED: (self.green, f"{chr(10003)} synced"),
            SyncStatus.DIVERGED: (self.yellow, "~ diverged"),
            SyncStatus.LOCAL_ONLY: (self.cyan, "+ local"),
            SyncStatus.REMOTE_ONLY: (self.magenta, f"{chr(8592)} remote"),
            SyncStatus.INCOMPATIBLE: (self.red, f"{chr(10007)} incompatible"),
        }

        headers = ["Type", "Name", "Status", "Details"]
        rows = []
        for entry in entries:
            color_fn, status_text = status_map.get(
                entry.status, (lambda x: x, "?")
            )
            rows.append([
                entry.entity_type,
                entry.name,
                color_fn(status_text),
                entry.detail,
            ])

        self.table(headers, rows)

        counts: dict[SyncStatus, int] = {}
        for entry in entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1

        parts = []
        for st, label in [
            (SyncStatus.SYNCED, "synced"),
            (SyncStatus.DIVERGED, "diverged"),
            (SyncStatus.LOCAL_ONLY, "local-only"),
            (SyncStatus.REMOTE_ONLY, "remote-only"),
            (SyncStatus.INCOMPATIBLE, "incompatible"),
        ]:
            if st in counts:
                parts.append(f"{counts[st]} {label}")

        if parts:
            self.blank()
            self.info(f"  Summary: {', '.join(parts)}")

    def entity_detail(self, data: dict) -> None:
        if self._json:
            self.json_output(data)
            return
        if not data:
            return
        max_key_len = max(len(k) for k in data)
        self.blank()
        for key, value in data.items():
            padded_key = key.ljust(max_key_len)
            print(f"    {self.bold(padded_key)}  {value}", file=sys.stderr)
        self.blank()

    def confirm(self, msg: str) -> bool:
        try:
            answer = input(f"  {self._c(_BOLD_YELLOW, '?')} {msg} [y/N] ")
            return answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return False

    # --- JSON mode ---

    def json_output(self, data: Any) -> None:
        print(json.dumps(data, indent=2, default=str), file=sys.stdout)

    # --- Static helpers ---

    @staticmethod
    def format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / (1024 * 1024):.1f} MB"

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format seconds into human-readable duration like '1m 30s'."""
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        secs = seconds % 60
        if minutes < 60:
            return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins:02d}m"

    @staticmethod
    def _visible_len(text: str) -> int:
        """Length of text excluding ANSI escape sequences."""
        return len(re.sub(r"\033\[[0-9;]*m", "", text))
