#!/usr/bin/env python3
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Full text search over Claude Code and Codex sessions, for the docked sidebar.

A search box at the top and results below. Picking one opens it in a new column
rather than in here, so the search stays where it is.

Deliberately not fzf: the sidebar is a narrow column, and fzf's single line of
results with a preview pane to the right needs width this does not have. Here
each result is two lines -- where it was and what was said -- which reads fine
at 30 columns.

Started for you by the layout when it is enabled with::

    enabled_layouts strip:sidebar=yes
"""

import os
import re
import unicodedata
import selectors
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Any

from kitty.constants import kitten_exe
from kitty.session_index import Hit, search, stats, update

DEBOUNCE = 0.18
MIN_QUERY = 2

R = '\033[0m'
DIM = '\033[2m'
BOLD = '\033[1m'
REV = '\033[7m'
AMBER = '\033[38;5;214m'
GREEN = '\033[38;5;149m'
BLUE = '\033[38;5;110m'
GREY = '\033[38;5;244m'
HIT = '\033[38;5;214m\033[1m'


def kill_word(text: str) -> str:
    """Drop the last word, the way alt+backspace does everywhere else."""
    stripped = text.rstrip()
    if not stripped:
        return ''
    # Japanese has no spaces, so fall back to dropping a run of the same kind
    # of character rather than swallowing the whole field.
    cut = max(stripped.rfind(' '), stripped.rfind('/'))
    if cut >= 0:
        return stripped[:cut + 1]
    return ''


def width_of(text: str) -> int:
    """Display columns, so the cursor lands where CJK text actually ends."""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def home_relative(path: str) -> str:
    home = os.path.expanduser('~')
    if path == home:
        return '~'
    if path.startswith(home + os.sep):
        return '~' + path[len(home):]
    return path


def fit(text: str, width: int) -> str:
    text = text.replace('\n', ' ').replace('\t', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= width else text[:max(0, width - 1)] + '…'


def snippet(text: str, query: str, width: int) -> str:
    """The part of the message the query is in, with the query marked."""
    flat = re.sub(r'\s+', ' ', text.replace('\n', ' ')).strip()
    idx = flat.lower().find(query.lower())
    if idx < 0:
        return fit(flat, width)
    start = max(0, idx - width // 3)
    piece = flat[start:start + width]
    rel = idx - start
    if rel < 0 or rel + len(query) > len(piece):
        return fit(piece, width)
    lead = '…' if start else ''
    body = piece[:rel] + HIT + piece[rel:rel + len(query)] + R + DIM + piece[rel + len(query):]
    return fit_ansi(lead + body, width)


def fit_ansi(text: str, width: int) -> str:
    """Truncate to width counting only printable characters."""
    out = []
    n = 0
    i = 0
    while i < len(text):
        if text[i] == '\033':
            j = text.find('m', i)
            if j < 0:
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if n >= width:
            break
        out.append(text[i])
        n += 1
        i += 1
    return ''.join(out)


class UI:

    def __init__(self) -> None:
        self.query = ''
        # Kept apart from the query on purpose: narrowing to a project is a
        # different question from searching for words, and mixing them into one
        # box means inventing a syntax to tell them apart.
        self.project = ''
        self.focus = 0  # 0 = query, 1 = project
        self.hits: list[Hit] = []
        self.sel = 0
        self.top = 0
        self.status = ''
        self.dirty = True
        self.pending: float = 0.0
        self.inbuf = b''
        # Everything slow happens off the input thread. A query can take
        # seconds, indexing longer, and neither may hold up a keystroke.
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.want: tuple[str, str] = ('', '')
        self.running: tuple[str, str] = ('', '')
        self.searching = False
        self.alive = True
        self.size = shutil.get_terminal_size((30, 40))

    # MARK: drawing

    def compose(self) -> tuple[list[str], int, int]:
        w, h = self.size.columns, self.size.lines
        inner = max(10, w - 1)
        out: list[str] = []

        def field(label: str, value: str, active: bool) -> str:
            room = inner - 4 - len(label)
            shown = value[-room:] if room > 0 else ''
            pad = ' ' * max(0, room - width_of(shown))
            tint = AMBER if active else DIM
            return f'{DIM}│{R}{tint}{label}{R}{shown}{pad}{DIM}│{R}'

        qshown = self.query[-(inner - 6):] if inner > 6 else ''
        pshown = self.project[-(inner - 6):] if inner > 6 else ''
        out.append(f'{DIM}┌{"─" * (inner - 2)}┐{R}')
        out.append(field('/ ', qshown, self.focus == 0))
        out.append(field('@ ', pshown, self.focus == 1))
        out.append(f'{DIM}└{"─" * (inner - 2)}┘{R}')

        label = 'searching…' if self.searching else self.status
        if label:
            out.append(f'{GREY}{fit(label, inner)}{R}')
        out.append('')

        # Everything after the box has to fit: one line too many scrolls the
        # box off the top, and there is no scrollback worth having here.
        header = 5 + (1 if (self.status or self.searching) else 0)
        rows = max(0, h - header - 1)
        per = 3
        capacity = max(1, rows // per)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + capacity:
            self.top = self.sel - capacity + 1

        shown_rows = 0
        for i in range(self.top, min(len(self.hits), self.top + capacity)):
            if shown_rows + per > rows:
                break
            hit = self.hits[i]
            cur = i == self.sel
            bar = f'{AMBER}▍{R}' if cur else ' '
            tag = f'{BLUE}cx{R}' if hit.source == 'codex' else f'{GREEN}cc{R}'
            head = f'{BOLD if cur else ""}{fit(home_relative(hit.cwd) or "?", inner - 8)}{R}'
            out.append(f'{bar}{tag} {head}')
            meta = f'{hit.role[:9]} {hit.ts[:16].replace("T", " ")}'
            out.append(f'  {DIM}{fit(meta, inner - 2)}{R}')
            out.append(f'  {DIM}{snippet(hit.text, self.query, inner - 3)}{R}')
            shown_rows += per

        if not self.hits:
            msg = 'type to search' if (len(self.query) < MIN_QUERY and not self.project) else 'no matches'
            out.append(f'{DIM} {msg}{R}')

        cur_row = 2 if self.focus == 0 else 3
        cur_col = 4 + width_of(qshown if self.focus == 0 else pshown)
        return out, cur_row, cur_col

    def draw(self) -> None:
        lines, cursor_row, cursor_col = self.compose()
        h = self.size.lines
        parts = []
        for i in range(h):
            parts.append(f'\033[{i + 1};1H\033[K')
            if i < len(lines):
                parts.append(lines[i])
        # The IME candidate window is placed at the terminal cursor, so it has to
        # sit in the search box. Hiding the cursor, or clearing the screen out
        # from under it on every keystroke, is what made typing Japanese here
        # unusable.
        parts.append(f'\033[{cursor_row};{cursor_col}H')
        sys.stdout.write(''.join(parts))
        sys.stdout.flush()
        self.dirty = False

    # MARK: actions

    def run_query(self) -> None:
        """Hand the query to the worker; never search on the input thread."""
        if len(self.query) < MIN_QUERY and not self.project:
            self.hits = []
            self.status = ''
            self.sel = self.top = 0
            self.dirty = True
            return
        with self.wake:
            self.want = (self.query, self.project)
            self.wake.notify()
        self.searching = True
        self.dirty = True

    def worker(self) -> None:
        while True:
            with self.wake:
                while self.alive and (not self.want or self.want == self.running):
                    self.wake.wait(0.5)
                if not self.alive:
                    return
                query = self.want
                self.running = query
            t = time.monotonic()
            try:
                hits = search(query[0], limit=300, cwd=query[1])
            except Exception as e:
                hits, note = [], str(e)[:40]
            else:
                note = f'{len(hits)} hits · {int((time.monotonic() - t) * 1000)}ms'
            with self.wake:
                # A newer query was typed while this one ran; drop this result.
                if self.want != query:
                    continue
                self.hits = hits
                self.status = note
                self.sel = self.top = 0
                self.searching = False
                self.dirty = True

    def indexer(self) -> None:
        while self.alive:
            try:
                update(budget=20.0)
            except Exception:
                pass
            for _ in range(60):
                if not self.alive:
                    return
                time.sleep(1)

    def open_selected(self) -> None:
        if not self.hits:
            return
        hit = self.hits[self.sel]
        cmd = ['claude', '--resume', hit.session] if hit.source == 'claude' else ['codex', 'resume', hit.session]
        args = [kitten_exe(), '@', 'launch', '--location', 'before', '--title', hit.session[:8]]
        if hit.cwd and os.path.isdir(hit.cwd):
            args += ['--cwd', hit.cwd]
        args += ['--'] + cmd
        try:
            # Fire and forget: waiting on this would freeze the input thread.
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status = f'opened {hit.session[:8]}'
        except Exception as e:
            self.status = f'failed: {e}'
        self.dirty = True

    # MARK: input

    def key(self, data: bytes) -> bool:
        """Returns False to quit."""
        if data in (b'\x03', b'\x04'):  # ctrl-c, ctrl-d
            return False
        if data in (b'\r', b'\n'):
            self.open_selected()
        elif data == b'\t':
            self.focus = 1 - self.focus
            self.dirty = True
        elif data in (b'\x1b\x7f', b'\x1b\x08', b'\x17'):  # alt+backspace, ctrl-w
            self.set_field(kill_word(self.field()))
        elif data in (b'\x7f', b'\b'):
            self.set_field(self.field()[:-1])
        elif data == b'\x15':  # ctrl-u
            self.set_field('')
        elif data in (b'\x1b[A', b'\x10'):  # up, ctrl-p
            self.sel = max(0, self.sel - 1)
            self.dirty = True
        elif data in (b'\x1b[B', b'\x0e'):  # down, ctrl-n
            self.sel = min(max(0, len(self.hits) - 1), self.sel + 1)
            self.dirty = True
        elif data == b'\x1b':
            self.set_field('')
        elif data and data[0] >= 32:
            self.inbuf += data
            while self.inbuf:
                try:
                    self.set_field(self.field() + self.inbuf.decode('utf-8'))
                    self.inbuf = b''
                    break
                except UnicodeDecodeError as e:
                    if e.end >= len(self.inbuf):
                        # a sequence split across reads; wait for the rest
                        self.set_field(self.field() + self.inbuf[:e.start].decode('utf-8'))
                        self.inbuf = self.inbuf[e.start:]
                        break
                    self.set_field(self.field() + self.inbuf[:e.start].decode('utf-8'))
                    self.inbuf = self.inbuf[e.end:]
            self.schedule()
        return True

    def field(self) -> str:
        return self.query if self.focus == 0 else self.project

    def set_field(self, value: str) -> None:
        if self.focus == 0:
            self.query = value
        else:
            self.project = value
        self.schedule()

    def schedule(self) -> None:
        self.pending = time.monotonic() + DEBOUNCE
        self.dirty = True


def main() -> None:
    ui = UI()
    m, s = stats()
    ui.status = f'{m} messages · {s} sessions'

    def on_resize(*_a: Any) -> None:
        ui.size = shutil.get_terminal_size((30, 40))
        ui.dirty = True

    signal.signal(signal.SIGWINCH, on_resize)
    threading.Thread(target=ui.worker, daemon=True).start()
    threading.Thread(target=ui.indexer, daemon=True).start()
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        saved = None
    try:
        if saved is not None:
            tty.setraw(fd)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        while True:
            if ui.dirty:
                ui.draw()
            for _k, _e in sel.select(timeout=0.1):
                data = os.read(fd, 1024)
                if not data or not ui.key(data):
                    return
            now = time.monotonic()
            if ui.pending and now >= ui.pending:
                ui.pending = 0.0
                ui.run_query()

    except KeyboardInterrupt:
        pass
    finally:
        ui.alive = False
        with ui.wake:
            ui.wake.notify_all()
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write('\033[H\033[2J')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
