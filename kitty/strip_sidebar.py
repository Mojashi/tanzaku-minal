#!/usr/bin/env python3
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Claude Code and Codex sessions, live and past, for the docked sidebar.

A search box at the top and results below. With the box empty the list is the
conversations still running in tmux, because those are the ones you can lose
track of; type and it becomes a full text search over everything ever said.
Picking one opens it in a new column rather than in here, so the search stays
where it is.

Deliberately not fzf: the sidebar is a narrow column, and fzf's single line of
results with a preview pane to the right needs width this does not have. Here
each result is two lines -- where it was and what was said -- which reads fine
at 30 columns.

Started for you by the layout when it is enabled with::

    enabled_layouts strip:sidebar=yes
"""

import json
import os
import re
import unicodedata
import selectors
import shutil
import signal
import shlex
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Any, NamedTuple

from kitty.constants import kitten_exe
from kitty.session_index import Hit, search, stats, update

DEBOUNCE = 0.18
MIN_QUERY = 2
LIVE_POLL = 2.0

R = '\033[0m'
DIM = '\033[2m'
BOLD = '\033[1m'
REV = '\033[7m'
AMBER = '\033[38;5;214m'
GREEN = '\033[38;5;149m'
BLUE = '\033[38;5;110m'
GREY = '\033[38;5;244m'
HIT = '\033[38;5;214m\033[1m'
RED = '\033[38;5;203m'


def clip_right(text: str, width: int) -> str:
    """Keep the tail of text that fits in width display columns."""
    if width <= 0:
        return ''
    out = ''
    used = 0
    for ch in reversed(text):
        w = 2 if unicodedata.east_asian_width(ch) in 'WF' else 1
        if used + w > width:
            break
        out = ch + out
        used += w
    return out


def tmux_has(session: str) -> bool:
    try:
        return subprocess.run(
            ['tmux', 'has-session', '-t', f'={session}'],
            capture_output=True, timeout=3).returncode == 0
    except Exception:
        return False


class Live(NamedTuple):
    """A conversation still running in tmux."""
    name: str
    attached: bool
    activity: int
    windows: int
    path: str
    title: str

    @property
    def source(self) -> str:
        return 'codex' if self.name.startswith('codex') else 'claude'


#: The title comes last so that a separator inside it cannot shift the fields.
LIVE_FORMAT = '#{session_name}|#{session_attached}|#{session_activity}|#{session_windows}|#{session_path}|#{pane_title}'


def live_sessions() -> list[Live]:
    """What tmux is running right now.

    tmux is the authority on this, not the index: a session that started a
    minute ago may not be indexed yet, and the ones named before c pinned the
    conversation id carry no id to look up at all. The pane title is the
    headline for the same reason -- the agent keeps it current, so it says what
    the conversation is doing now rather than what it was first asked.
    """
    try:
        out = subprocess.run(['tmux', 'list-sessions', '-F', LIVE_FORMAT],
                             capture_output=True, timeout=5, text=True)
    except Exception:
        return []
    if out.returncode:  # no server running is the usual reason
        return []
    items = []
    for line in out.stdout.splitlines():
        parts = line.split('|', 5)
        if len(parts) < 6:
            continue
        name, attached, activity, windows, path, title = parts
        try:
            items.append(Live(name, attached == '1', int(activity), int(windows), path, title))
        except ValueError:
            continue
    return items


def ago(when: int) -> str:
    d = max(0, int(time.time()) - when)
    if d < 60:
        return f'{d}s'
    if d < 3600:
        return f'{d // 60}m'
    if d < 86400:
        return f'{d // 3600}h'
    return f'{d // 86400}d'


def focus_window_running(session: str) -> bool:
    """Bring the column already attached to this session into view.

    Attaching twice would work, but tmux sizes a session to its smallest client,
    so the second attach shrinks the one you were already using.
    """
    try:
        out = subprocess.run([kitten_exe(), '@', 'ls'], capture_output=True, timeout=5)
        data = json.loads(out.stdout or b'[]')
    except Exception:
        return False
    for os_window in data:
        for tab in os_window.get('tabs', []):
            for w in tab.get('windows', []):
                for proc in w.get('foreground_processes', []):
                    if session in ' '.join(proc.get('cmdline') or []):
                        try:
                            subprocess.run(
                                [kitten_exe(), '@', 'focus-window', '--match', f'id:{w["id"]}'],
                                capture_output=True, timeout=5)
                        except Exception:
                            return False
                        return True
    return False


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
        self.live: list[Live] = []
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

    # MARK: rows

    def visible_live(self) -> list[Live]:
        """Ongoing sessions, unless a query has turned this into a search."""
        if len(self.query) >= MIN_QUERY:
            return []
        p = self.project.strip().lower()
        items = [x for x in self.live if not p or p in x.path.lower()]
        # Detached first: the point of this list is to reach the conversations
        # that are not already on screen somewhere.
        items.sort(key=lambda x: (x.attached, -x.activity))
        return items

    def cards(self, inner: int) -> list[list[str]]:
        """Every row as the lines it occupies, live ones then search results."""
        out: list[list[str]] = []
        live = self.visible_live()
        for i, x in enumerate(live):
            out.append(self.live_card(x, i == self.sel, inner))
        for j, hit in enumerate(self.hits):
            out.append(self.hit_card(hit, len(live) + j == self.sel, inner))
        return out

    def live_card(self, x: Live, cur: bool, inner: int) -> list[str]:
        bar = f'{AMBER}▍{R}' if cur else ' '
        # Attached means it is open in a column right now; detached means it is
        # running with nobody watching it.
        mark = f'{GREEN}●{R}' if x.attached else f'{AMBER}○{R}'
        tag = f'{BLUE}cx{R}' if x.source == 'codex' else f'{GREEN}cc{R}'
        title = x.title.strip() or x.name
        head = f'{BOLD if cur else ""}{fit(title, inner - 4)}{R}'
        meta = f'{fit(home_relative(x.path), inner - 18)} · {ago(x.activity)} · {x.name}'
        return [f'{bar}{mark} {head}', f'  {DIM}{tag} {fit(meta, inner - 6)}{R}']

    def hit_card(self, hit: Hit, cur: bool, inner: int) -> list[str]:
        bar = f'{AMBER}▍{R}' if cur else ' '
        tag = f'{BLUE}cx{R}' if hit.source == 'codex' else f'{GREEN}cc{R}'
        head = f'{BOLD if cur else ""}{fit(home_relative(hit.cwd) or "?", inner - 8)}{R}'
        # What the conversation was for, which the matching line on its own
        # rarely says. Without it every result is a fragment out of context.
        summary = fit(hit.summary, inner - 2) if hit.summary else f'{DIM}(no summary){R}'
        extra = f' ·{hit.hits} hits' if hit.hits > 1 else ''
        # How it stopped, when that is worth knowing: a session cut off by a
        # crash or a connection failure is one you probably meant to finish.
        mark = {'error': f'{RED}⚡cut off{R}', 'cut': f'{AMBER}⚠ unanswered{R}'}.get(hit.ending, '')
        meta = f'{hit.ts[:10]} {hit.ts[11:16]} · {hit.msg_count} msgs{extra}'
        return [
            f'{bar}{tag} {head}',
            f'  {summary}',
            f'  {DIM}{fit(meta, inner - 2)}{R}' + (f' {mark}' if mark else ''),
            f'  {DIM}{snippet(hit.text, self.query, inner - 3)}{R}',
        ]

    # MARK: drawing

    def compose(self) -> tuple[list[str], int, int]:
        w, h = self.size.columns, self.size.lines
        inner = max(10, w - 1)
        out: list[str] = []

        # The border lines are inner columns wide, so a field line has to be
        # too: two for the box sides, the label, then the value and its padding.
        label_w = 2
        room = inner - 2 - label_w

        def field(label: str, shown: str, active: bool) -> str:
            pad = ' ' * max(0, room - width_of(shown))
            tint = AMBER if active else DIM
            return f'{DIM}│{R}{tint}{label}{R}{shown}{pad}{DIM}│{R}'

        qshown = clip_right(self.query, room)
        pshown = clip_right(self.project, room)
        title = ' SESSIONS '
        rule = '─' * max(0, inner - 2 - len(title))
        out.append(f'{DIM}┌{R}{AMBER}{title}{R}{DIM}{rule}┐{R}')
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
        cards = self.cards(inner)
        n_live = len(self.visible_live())

        # Rows are not all the same height, so the window is pushed down from
        # the top until the selected row fits rather than sized by a fixed
        # number of rows per screen.
        self.top = max(0, min(self.top, len(cards) - 1))
        if self.sel < self.top:
            self.top = self.sel
        while self.top < self.sel and sum(len(c) + 1 for c in cards[self.top:self.sel + 1]) > rows:
            self.top += 1

        shown = 0
        for i in range(self.top, len(cards)):
            card = cards[i]
            if i == n_live and n_live and self.top < n_live:
                if shown + 1 >= rows:
                    break
                out.append(f'{DIM}{"─" * inner}{R}')
                shown += 1
            if shown + len(card) > rows:
                break
            out += card
            out.append('')
            shown += len(card) + 1

        if not cards:
            msg = 'type to search' if (len(self.query) < MIN_QUERY and not self.project) else 'no matches'
            out.append(f'{DIM} {msg}{R}')

        cur_row = 2 if self.focus == 0 else 3
        cur_col = 1 + label_w + 1 + width_of(qshown if self.focus == 0 else pshown)
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

    def poller(self) -> None:
        """Keep the ongoing list current without a keystroke to trigger it."""
        while self.alive:
            live = live_sessions()
            with self.wake:
                # Only a real change redraws: a redraw every couple of seconds
                # would fight the IME, which lives at the cursor in the box.
                if live != self.live:
                    self.live = live
                    self.dirty = True
            for _ in range(int(LIVE_POLL * 10)):
                if not self.alive:
                    return
                time.sleep(0.1)

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

    def total(self) -> int:
        return len(self.visible_live()) + len(self.hits)

    def open_selected(self) -> None:
        live = self.visible_live()
        if self.sel < len(live):
            threading.Thread(target=self.open_live, args=(live[self.sel],), daemon=True).start()
            return
        idx = self.sel - len(live)
        if idx >= len(self.hits):
            return
        hit = self.hits[idx]
        # If the conversation is already live in tmux, go to it rather than
        # starting a second copy of it. c and x name the tmux session after the
        # conversation precisely so this lookup is exact rather than a guess.
        prefix = 'claude' if hit.source == 'claude' else 'codex'
        sess = f'{prefix}-{hit.session[:8]}'
        if tmux_has(sess):
            args = [kitten_exe(), '@', 'launch', '--location', 'before', '--title', sess]
            if hit.cwd and os.path.isdir(hit.cwd):
                args += ['--cwd', hit.cwd]
            args += ['--', 'tmux', 'attach', '-t', f'={sess}']
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.status = f'attached {sess}'
            except Exception as e:
                self.status = f'failed: {e}'
            self.dirty = True
            return

        # Go through the user's own launcher rather than running the agent
        # directly: c and x set up direnv, the flags they always pass, and the
        # tmux session everything else expects to find.
        fn = 'c' if hit.source == 'claude' else 'x'
        resume = f'--resume {shlex.quote(hit.session)}' if hit.source == 'claude' \
            else f'resume {shlex.quote(hit.session)}'
        line = f'{fn} {resume}'
        if hit.cwd and os.path.isdir(hit.cwd):
            line = f'cd {shlex.quote(hit.cwd)} && {line}'
        args = [kitten_exe(), '@', 'launch', '--location', 'before', '--title', hit.session[:8]]
        if hit.cwd and os.path.isdir(hit.cwd):
            args += ['--cwd', hit.cwd]
        # -i so the functions from .zshrc are defined.
        args += ['--', 'zsh', '-ic', line]
        try:
            # Fire and forget: waiting on this would freeze the input thread.
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status = f'opened {hit.session[:8]}'
        except Exception as e:
            self.status = f'failed: {e}'
        self.dirty = True

    def open_live(self, x: Live) -> None:
        """Go to an ongoing session. Runs off the input thread: it asks kitty
        what it has open, which is a round trip."""
        if x.attached and focus_window_running(x.name):
            self.status = f'focused {x.name}'
            self.dirty = True
            return
        args = [kitten_exe(), '@', 'launch', '--location', 'before', '--title', x.name]
        if x.path and os.path.isdir(x.path):
            args += ['--cwd', x.path]
        args += ['--', 'tmux', 'attach', '-t', f'={x.name}']
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status = f'attached {x.name}'
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
            self.sel = min(max(0, self.total() - 1), self.sel + 1)
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


#: The sidebar is not a terminal you work in, and it should not look like one.
#: It paints itself rather than being styled by the layout, because a window can
#: only be told its own colours from inside it.
SIDEBAR_BG = '#15171c'
SIDEBAR_FG = '#c8ccd4'


def paint_self() -> None:
    sys.stdout.write(f'\033]11;{SIDEBAR_BG}\007\033]10;{SIDEBAR_FG}\007')
    # No cursor block in a list, but the cursor still has to exist and stay in
    # the search box for the IME candidate window to have somewhere to go.
    sys.stdout.write('\033[6 q')
    sys.stdout.flush()


def main() -> None:
    ui = UI()
    paint_self()
    m, s = stats()
    ui.status = f'{m} messages · {s} sessions'

    def on_resize(*_a: Any) -> None:
        ui.size = shutil.get_terminal_size((30, 40))
        ui.dirty = True

    signal.signal(signal.SIGWINCH, on_resize)
    threading.Thread(target=ui.worker, daemon=True).start()
    threading.Thread(target=ui.indexer, daemon=True).start()
    threading.Thread(target=ui.poller, daemon=True).start()
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
