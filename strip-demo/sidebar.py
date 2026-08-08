#!/usr/bin/env python3
"""A session list for the strip layout, meant to run in the docked sidebar.

Redraws when the window list changes. It polls rather than being pushed to,
because there is no event for "a window's title or directory changed" and
the poll is one remote control call a second.

    kitten @ launch --var strip_sidebar=1 --title sessions -- python3 sidebar.py
"""

import json
import os
import shutil
import subprocess
import sys
import time

KITTEN = os.environ.get('KITTY_KITTEN', 'kitten')
INTERVAL = 1.0

RESET = '\033[0m'
DIM = '\033[2m'
BOLD = '\033[1m'
AMBER = '\033[38;5;214m'
GREEN = '\033[38;5;149m'
BLUE = '\033[38;5;110m'


def windows() -> list[dict]:
    try:
        out = subprocess.run(
            [KITTEN, '@', 'ls'], capture_output=True, timeout=4, check=True).stdout
    except Exception:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    ans = []
    for osw in data:
        for tab in osw.get('tabs', ()):
            if not tab.get('is_focused'):
                continue
            for w in tab.get('windows', ()):
                if any(v for k, v in (w.get('user_vars') or {}).items() if k == 'strip_sidebar'):
                    continue
                ans.append(w)
    return ans


def program_of(w: dict) -> str:
    """What the column is actually for, which is rarely the shell."""
    for p in reversed(w.get('foreground_processes') or ()):
        argv = p.get('cmdline') or ()
        if not argv:
            continue
        exe = os.path.basename(argv[0]).lstrip('-')
        if exe in ('sh', 'bash', 'zsh', 'fish', 'dash', 'login'):
            continue
        return exe
    return ''


def shorten(path: str, width: int) -> str:
    home = os.path.expanduser('~')
    if path == home:
        return '~'
    if path.startswith(home + os.sep):
        path = '~' + path[len(home):]
    if len(path) <= width:
        return path
    parts = path.split(os.sep)
    while len(parts) > 2 and len(os.sep.join(parts)) > width:
        del parts[1]
        parts[0] = parts[0] + os.sep + '…' if not parts[0].endswith('…') else parts[0]
    out = os.sep.join(parts)
    return out[-width:] if len(out) > width else out


def render(ws: list[dict], width: int) -> str:
    inner = max(8, width - 2)
    lines = [f'{DIM} SESSIONS{RESET}', '']
    for i, w in enumerate(ws, 1):
        focused = w.get('is_focused')
        marker = f'{AMBER}▍{RESET}' if focused else ' '
        num = f'{BOLD if focused else DIM}{i}{RESET}'
        prog = program_of(w)
        name = prog or (w.get('title') or '').strip() or 'shell'
        colour = GREEN if prog else ''
        head = f'{marker}{num} {colour}{name[:inner - 4]}{RESET}'
        lines.append(head)
        cwd = shorten(w.get('cwd') or '', inner - 3)
        lines.append(f'  {BLUE}{DIM}{cwd}{RESET}')
        lines.append('')
    if not ws:
        lines.append(f'{DIM} (none){RESET}')
    return '\n'.join(lines)


def main() -> None:
    last = None
    print('\033[?25l', end='')  # the list is not a prompt, so no cursor
    try:
        while True:
            width = shutil.get_terminal_size((24, 40)).columns
            ws = windows()
            key = (width, [(w.get('id'), w.get('is_focused'), w.get('cwd'), program_of(w)) for w in ws])
            if key != last:
                last = key
                sys.stdout.write('\033[2J\033[H' + render(ws, width))
                sys.stdout.flush()
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        print('\033[?25h', end='')


if __name__ == '__main__':
    main()
