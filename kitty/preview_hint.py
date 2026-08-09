#!/usr/bin/env python3
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Pick a path off the screen with the keyboard and preview it.

The selection based preview never fires inside a full screen TUI, because the
program has the mouse and a drag there never becomes a kitty selection. This is
the way in that does not involve the mouse at all.

    map cmd+shift+f kitten hints --type path --program @preview
"""

from typing import Any


def main(args: list[str]) -> None:
    raise SystemExit('Must be run as a hints program')


def handle_result(args: list[str], data: str, target_window_id: int, boss: Any) -> None:
    path = (data or '').strip()
    if path:
        boss.preview_selection(path)


handle_result.type = 'hints'  # type: ignore[attr-defined]
