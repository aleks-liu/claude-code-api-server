"""Interactive checkbox picker for sync push entity selection."""

from __future__ import annotations

import sys
from typing import Sequence

from .models import SyncEntry, SyncStatus


# Status display config: (label, color_code)
_STATUS_DISPLAY: dict[SyncStatus, tuple[str, str]] = {
    SyncStatus.LOCAL_ONLY: ("+ local", "\033[36m"),
    SyncStatus.DIVERGED: ("~ diverged", "\033[33m"),
    SyncStatus.SYNCED: ("= synced", "\033[32m"),
    SyncStatus.REMOTE_ONLY: ("< remote", "\033[35m"),
    SyncStatus.INCOMPATIBLE: ("x incompat", "\033[31m"),
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_REVERSE = "\033[7m"


def _is_pushable(entry: SyncEntry) -> bool:
    """Check if an entry can be pushed."""
    return entry.status in (SyncStatus.LOCAL_ONLY, SyncStatus.DIVERGED)


def pick_entries(entries: list[SyncEntry]) -> list[SyncEntry] | None:
    """Interactive picker for sync entries.

    Returns selected entries, or None if cancelled.
    Tries curses first, falls back to numbered list.
    """
    pushable = [e for e in entries if _is_pushable(e)]
    if not pushable:
        return []

    # Only try curses if both stdin and stderr are real TTYs
    if sys.stdin.isatty() and sys.stderr.isatty():
        try:
            import curses
            return _curses_picker(pushable)
        except (ImportError, curses.error, Exception):
            pass

    return _numbered_picker(pushable)


def _format_entry_line(entry: SyncEntry, selected: bool,
                       current: bool = False, use_color: bool = True) -> str:
    """Format a single entry line for display."""
    check = "[x]" if selected else "[ ]"
    label, color = _STATUS_DISPLAY.get(entry.status, ("?", ""))

    if use_color:
        name_part = f"{_BOLD}{entry.name}{_RESET}"
        status_part = f"{color}{label}{_RESET}"
        type_part = f"{_DIM}{entry.entity_type}{_RESET}"
    else:
        name_part = entry.name
        status_part = label
        type_part = entry.entity_type

    return f" {check}  {type_part:>8}  {name_part}  {status_part}"


# =============================================================================
# Curses-based picker
# =============================================================================

def _curses_picker(entries: list[SyncEntry]) -> list[SyncEntry] | None:
    """Full-screen curses checkbox picker."""
    import curses

    selected = [False] * len(entries)
    cursor = 0
    scroll_offset = 0

    def _draw(stdscr, max_rows: int) -> None:
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        # Header
        header = " CCAS Sync Push — Select entities to push"
        stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
        help_text = " [Space] toggle  [a] all  [Enter] confirm  [q] cancel"
        stdscr.addnstr(1, 0, help_text, w - 1, curses.A_DIM)
        sel_count = sum(selected)
        count_text = f" {sel_count}/{len(entries)} selected"
        stdscr.addnstr(2, 0, count_text, w - 1)

        # Draw entries
        list_start = 4
        visible = min(len(entries), h - list_start - 1)

        for i in range(visible):
            idx = scroll_offset + i
            if idx >= len(entries):
                break
            row = list_start + i
            if row >= h - 1:
                break

            entry = entries[idx]
            check = "[x]" if selected[idx] else "[ ]"
            status_label, _ = _STATUS_DISPLAY.get(entry.status, ("?", ""))
            line = f" {check}  {entry.entity_type:>8}  {entry.name}  ({status_label})"

            # Truncate to terminal width
            line = line[:w - 1]

            attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
            try:
                stdscr.addnstr(row, 0, line, w - 1, attr)
            except curses.error:
                pass

        # Footer
        footer_row = h - 1
        footer = f" {sel_count} selected | arrows/j/k to move | space to toggle"
        try:
            stdscr.addnstr(footer_row, 0, footer, w - 1, curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()

    def _run(stdscr) -> list[SyncEntry] | None:
        nonlocal cursor, scroll_offset
        curses.curs_set(0)  # Hide cursor
        # Use default colors
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        while True:
            h, _ = stdscr.getmaxyx()
            list_start = 4
            visible = h - list_start - 1

            # Keep cursor in view
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + visible:
                scroll_offset = cursor - visible + 1

            _draw(stdscr, visible)

            key = stdscr.getch()

            if key in (ord('q'), 27):  # q or Escape
                return None
            elif key in (curses.KEY_UP, ord('k')):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                cursor = min(len(entries) - 1, cursor + 1)
            elif key == ord(' '):
                selected[cursor] = not selected[cursor]
                cursor = min(len(entries) - 1, cursor + 1)
            elif key == ord('a'):
                # Toggle all: if all selected -> deselect all, else select all
                if all(selected):
                    for i in range(len(selected)):
                        selected[i] = False
                else:
                    for i in range(len(selected)):
                        selected[i] = True
            elif key in (curses.KEY_ENTER, 10, 13):
                return [entries[i] for i in range(len(entries)) if selected[i]]

    return curses.wrapper(_run)


# =============================================================================
# Numbered list fallback
# =============================================================================

def _numbered_picker(entries: list[SyncEntry]) -> list[SyncEntry] | None:
    """Numbered list picker for non-curses environments."""
    use_color = sys.stderr.isatty()

    print("\n  Select entities to push:\n", file=sys.stderr)
    for i, entry in enumerate(entries, 1):
        pre = "*" if entry.status == SyncStatus.LOCAL_ONLY else " "
        label, color = _STATUS_DISPLAY.get(entry.status, ("?", ""))
        if use_color:
            print(f"  {pre} {i:3d}) {_DIM}{entry.entity_type:>8}{_RESET}  "
                  f"{entry.name}  {color}{label}{_RESET}", file=sys.stderr)
        else:
            print(f"  {pre} {i:3d}) {entry.entity_type:>8}  "
                  f"{entry.name}  {label}", file=sys.stderr)

    print(f"\n  (* = local-only, pre-selected)", file=sys.stderr)
    print(f"  Enter numbers (e.g. 1,3,5-7), 'all', or 'q' to cancel.", file=sys.stderr)

    try:
        answer = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None

    if not answer or answer.lower() == 'q':
        return None

    if answer.lower() == 'all':
        return list(entries)

    # Parse number ranges
    indices: set[int] = set()
    for part in answer.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                for n in range(int(start), int(end) + 1):
                    indices.add(n)
            except ValueError:
                continue
        else:
            try:
                indices.add(int(part))
            except ValueError:
                continue

    result = []
    for idx in sorted(indices):
        if 1 <= idx <= len(entries):
            result.append(entries[idx - 1])

    return result if result else None
