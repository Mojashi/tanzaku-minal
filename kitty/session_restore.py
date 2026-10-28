#!/usr/bin/env python3
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Bring the strip back after kitty restarts, or after the machine lost power.

What is worth bringing back is where everything was and which conversation was
running in it. kitty records that every minute and when it quits:

* the tabs, and in each the columns in order, with their widths;
* which tmux session each column was showing;
* for every tmux session (not only the ones on screen), each pane's directory
  and the Claude Code / Codex / Kimi conversation running in it, down to its id.

On startup the record is replayed. A tmux session that is still alive (kitty
alone was restarted) is simply attached again. One that died with the machine
is created again, with each conversation resumed by id rather than by "the
most recent one here", which is wrong as soon as two share a directory.
"""

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .closed_windows import SHELL_NAMES, extract_tmux_session, relaunch_argv, tmux_executable

if TYPE_CHECKING:
    from .boss import Boss
    from .tabs import Tab as KittyTab

STATE_PATH = os.path.expanduser('~/.local/state/kitty-strip/last-session.json')
ERROR_LOG = os.path.join(os.path.dirname(STATE_PATH), 'errors.log')
HISTORY_KEEP = 48  # generations of the record kept under history/
HISTORY_EVERY = 600.0  # seconds between generations, unless sessions disappear
JOURNAL_MAX = 512 * 1024  # bytes a journal may grow to before its older half goes
#: Right after a reboot the machine is under heavy load (dozens of agents
#: starting at once) and ps alone has taken over 5 seconds.
COMMAND_TIMEOUT = 30.0
SAVE_INTERVAL = 60.0  # seconds
CLAUDE_SESSIONS = os.path.expanduser('~/.claude/sessions')
CODEX_SESSIONS = os.path.expanduser('~/.codex/sessions')
KIMI_SESSIONS = os.path.expanduser('~/.kimi-code/sessions')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
KIMI_ID = re.compile(r'session_[0-9a-f-]{36}')
AGENTS = ('claude', 'codex', 'kimi')
#: Right after a reboot the network is not up yet, and Codex, started then,
#: fails its first account check and exits. Every agent waits for it instead.
WAIT_FOR_NETWORK = "until curl -s -o /dev/null -m 3 https://chatgpt.com; do printf '\\rネットワーク待ち…'; sleep 2; done; "


# MARK: The record


@dataclass(frozen=True)
class Conversation:
    kind: str  # one of AGENTS
    id: str

    def resume_in(self, cwd: str) -> str:
        # The same command lines the c / x shell functions build, so a restored
        # conversation runs exactly like one started by hand.
        project = f'{WAIT_FOR_NETWORK}cd {shlex.quote(cwd)} && direnv allow . 2>/dev/null;'
        if self.kind == 'claude':
            return f'{project} CLAUDE_CODE_NO_FLICKER=1 direnv exec . claude --dangerously-skip-permissions -r {self.id}'
        if self.kind == 'kimi':
            return f'{WAIT_FOR_NETWORK}cd {shlex.quote(cwd)} && kimi -S {self.id}'
        return f'{project} direnv exec . codex --dangerously-bypass-approvals-and-sandbox resume {self.id}'


@dataclass(frozen=True)
class Pane:
    cwd: str
    title: str = ''
    conversation: Conversation | None = None
    #: What the pane was started with, replayed only when it is not a conversation.
    command: str = ''
    #: The pane's shell, for recognising the same pane in the next record. Not used on restore.
    pid: int = 0

    def restart_command(self, running: set[str]) -> str:
        """The shell line that puts this pane back."""
        if self.conversation is not None:
            # Resuming a conversation that is already open somewhere else forks it.
            if self.conversation.id in running:
                return f'cd {shlex.quote(self.cwd)}'
            return self.conversation.resume_in(self.cwd)
        if self.command:
            return f'cd {shlex.quote(self.cwd)} && {self.command}'
        return f'cd {shlex.quote(self.cwd)}'


@dataclass(frozen=True)
class Column:
    width: int | None
    #: The tmux session the column was showing, if any. Everything else below
    #: is only used for columns that were not in tmux.
    tmux: str | None = None
    cwd: str = ''
    argv: tuple[str, ...] = ()
    last_cmd: str = ''
    title: str = ''
    #: The process the column runs, for finding its conversation. Not used on restore.
    pid: int = 0
    conversation: Conversation | None = None

    def launch_command(self, running: set[str]) -> list[str]:
        if self.conversation is not None and self.conversation.id not in running:
            return ['zsh', '-ic', f'{self.conversation.resume_in(self.cwd)}; exec zsh']
        return relaunch_argv(self.argv, self.last_cmd)


@dataclass(frozen=True)
class Tab:
    title: str
    columns: tuple[Column, ...]
    active: bool = False


@dataclass(frozen=True)
class Snapshot:
    tabs: tuple[Tab, ...]
    sessions: dict[str, tuple[Pane, ...]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {'tabs': [asdict(t) for t in self.tabs], 'sessions': {k: [asdict(p) for p in v] for k, v in self.sessions.items()}}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> 'Snapshot':
        def pane(p: dict[str, Any]) -> Pane:
            c = p.get('conversation')
            return Pane(p['cwd'], p.get('title', ''), Conversation(c['kind'], c['id']) if c else None, p.get('command', ''), p.get('pid', 0))

        def column(c: dict[str, Any]) -> Column:
            conv = c.get('conversation')
            return Column(
                c.get('width'), c.get('tmux'), c.get('cwd', ''), tuple(c.get('argv', ())), c.get('last_cmd', ''), c.get('title', ''),
                c.get('pid', 0), Conversation(conv['kind'], conv['id']) if conv else None)

        return cls(
            tuple(Tab(t.get('title', ''), tuple(column(c) for c in t['columns']), t.get('active', False)) for t in data.get('tabs', ())),
            {name: tuple(pane(p) for p in panes) for name, panes in data.get('sessions', {}).items()},
        )


# MARK: Saving


class SessionKeeper:
    """Owns the record on disk: restores from it once, then keeps it current.

    Reading kitty's tabs has to happen on the main thread. Finding the
    conversations means running ps and tmux, which would stall the UI, and a
    Python thread does not help: kitty's event loop holds the GIL while it
    waits, so a thread barely runs. That part goes to a child process instead.
    """

    def __init__(self, boss: 'Boss', path: str = STATE_PATH) -> None:
        self.boss = boss
        self.path = path
        self.recorder_running = False

    def start(self, restore: bool) -> None:
        from .fast_data_types import add_timer
        if restore:
            # Let the first window finish appearing, then lay the strip out over it.
            add_timer(self.restore, 0.05, False)
        else:
            self.begin_saving()

    def restore(self, timer_id: int | None = None) -> None:
        snapshot = read_snapshot(self.path)
        if snapshot is not None:
            journal(self.path, 'restore.log', f'restoring {describe(snapshot)}')
            report: list[str] = []
            best_effort('restore session', lambda: restore(self.boss, snapshot, report))
            for line in report:
                journal(self.path, 'restore.log', line)
        # Only after the restore, or the half-built strip would be saved over the record.
        self.begin_saving()

    def begin_saving(self) -> None:
        from .fast_data_types import add_timer
        add_timer(self.save_in_background, SAVE_INTERVAL, True)

    def save_in_background(self, timer_id: int | None = None) -> None:
        tabs = self.tabs_to_save()
        if tabs is None or self.recorder_running:
            return
        from .constants import kitty_exe
        self.recorder_running = True
        self.boss.run_background_process(
            [kitty_exe(), '+runpy', 'from kitty.session_restore import record_from_stdin; record_from_stdin()'],
            stdin=json.dumps({'path': self.path, 'tabs': [asdict(t) for t in tabs]}).encode(),
            notify_on_death=self.recorder_finished,
        )

    def recorder_finished(self, status: int, err: Exception | None) -> None:
        self.recorder_running = False
        if err is not None:
            best_effort('start the session recorder', lambda: _raise(err))

    def save_now(self) -> None:
        """For quitting: there is no later to do it in, so this one blocks."""
        tabs = self.tabs_to_save()
        if tabs is not None:
            best_effort('save session', lambda: record(self.path, tabs))

    def tabs_to_save(self) -> tuple[Tab, ...] | None:
        from .fast_data_types import NO_CLOSE_REQUESTED, current_application_quit_request
        # While kitty is closing, windows disappear one by one; none of those
        # in-between states is the one to come back to.
        if current_application_quit_request() != NO_CLOSE_REQUESTED:
            return None
        tabs = layout_of(self.boss)
        # No tabs means every window was closed, not that nothing should come back.
        return tabs or None


def record_from_stdin() -> None:
    """The child process side of SessionKeeper.save_in_background."""
    import sys
    data = json.load(sys.stdin)
    tabs = Snapshot.from_json({'tabs': data['tabs']}).tabs
    best_effort('save session', lambda: record(data['path'], tabs))


def record(path: str, tabs: tuple[Tab, ...]) -> None:
    """Add the conversations to kitty's layout and write it, if anything changed."""
    try:
        with open(path) as f:
            previous_file = json.load(f)
    except (OSError, ValueError):
        previous_file = None
    previous = {k: v for k, v in previous_file.items() if k != 'saved_at'} if previous_file else None
    snapshot = with_conversations(tabs, Snapshot.from_json(previous) if previous else Snapshot(()))
    # Through JSON and back, so tuples compare equal to the lists read from the file.
    data = json.loads(json.dumps(snapshot.to_json()))
    if data == previous:
        return
    if previous_file is not None and previous is not None:
        keep_in_history(path, previous_file, losing_sessions=bool(set(previous.get('sessions', {})) - set(data['sessions'])))
    write_atomically(path, json.dumps({'saved_at': time.time(), **data}, ensure_ascii=False, indent=1))
    journal(path, 'saves.log', describe(snapshot))


# MARK: Leaving a trail
#
# When a restore brings back less than was there, the record that drove it has
# usually been overwritten by then. These keep enough behind to tell whether
# the record was short or the restore failed.


def keep_in_history(path: str, record: dict[str, Any], losing_sessions: bool) -> None:
    """Keep the record about to be replaced: every HISTORY_EVERY, and always when sessions disappear from it."""
    history = os.path.join(os.path.dirname(path), 'history')
    os.makedirs(history, exist_ok=True)
    kept = sorted(os.listdir(history))
    if kept and not losing_sessions and time.time() - mtime(os.path.join(history, kept[-1])) < HISTORY_EVERY:
        return
    saved_at = record.get('saved_at') or time.time()
    name = time.strftime('%Y%m%d-%H%M%S', time.localtime(saved_at)) + '.json'
    write_atomically(os.path.join(history, name), json.dumps(record, ensure_ascii=False, indent=1))
    for old in sorted(os.listdir(history))[:-HISTORY_KEEP]:
        with suppress(OSError):
            os.remove(os.path.join(history, old))


def journal(path: str, name: str, line: str) -> None:
    """Append a timestamped line to a log next to the record, keeping it bounded."""
    log = os.path.join(os.path.dirname(path), name)
    with suppress(OSError):
        os.makedirs(os.path.dirname(log), exist_ok=True)
        if mtime(log) and os.path.getsize(log) > JOURNAL_MAX:
            with open(log) as f:
                lines = f.readlines()
            write_atomically(log, ''.join(lines[len(lines) // 2:]))
        with open(log, 'a') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {line}\n')


def describe(snapshot: Snapshot) -> str:
    columns = [c for t in snapshot.tabs for c in t.columns]
    panes = [p for ps in snapshot.sessions.values() for p in ps]
    conversations = sum(p.conversation is not None for p in panes) + sum(c.conversation is not None for c in columns)
    return (f'tabs={len(snapshot.tabs)} columns={len(columns)} sessions={len(snapshot.sessions)} '
            f'conversations={conversations} [{" ".join(snapshot.sessions)}]')


def _raise(err: Exception) -> None:
    raise err


def layout_of(boss: 'Boss') -> tuple[Tab, ...]:
    """kitty's side of the record: tabs, columns, widths. Main thread only."""
    tabs = []
    for tm in boss.os_window_map.values():
        for tab in tm:
            columns = tuple(column_of(tab, g) for g in tab.windows.iter_all_layoutable_groups() if not is_sidebar(g))
            if columns:
                tabs.append(Tab(tab.name, columns, tab is boss.active_tab))
    return tuple(tabs)


def column_of(tab: 'KittyTab', group: Any) -> Column:
    from .layout.strip import Strip
    window = group.windows[0]
    layout = tab.current_layout
    width = layout.column_width(group.id) if isinstance(layout, Strip) else None
    try:
        # Programs that rewrite their own argv (kimi-code) leave empty strings behind.
        argv = tuple(a for a in window.child.foreground_cmdline or () if a)
        cwd = window.cwd_of_child or ''
        pid = window.child.pid or 0
    except Exception:
        argv, cwd, pid = (), '', 0
    if argv and os.path.basename(argv[0]) == 'tmux' and (session := extract_tmux_session(argv)):
        return Column(width, tmux=session)
    return Column(width, None, cwd, argv, getattr(window, 'last_cmd_cmdline', '') or '', window.title or '', pid)


def is_sidebar(group: Any) -> bool:
    from .layout.strip import SIDEBAR_VAR
    return any(w.user_vars.get(SIDEBAR_VAR) for w in group.windows)


def with_conversations(tabs: tuple[Tab, ...], previous: Snapshot) -> Snapshot:
    """kitty の配置に、tmux の全ペインと、各所で開いている会話を書き足す。前回の記録にあった会話は、終わっていても覚えておく。"""
    procs = Processes.now()
    panes = list(tmux_panes(procs))
    # Columns not in tmux can run an agent directly (kimi in a kitty window).
    column_agents = {
        c.pid: a for t in tabs for c in t.columns
        if c.tmux is None and c.pid and (a := agent_in(procs.tree(c.pid), c.cwd, procs)) is not None}
    conversations = identify([p.agent for p in panes if p.agent is not None] + list(column_agents.values()))
    remembered = Remembered(previous)

    sessions: dict[str, list[Pane]] = {}
    for p in panes:
        if p.agent is not None:
            conv = conversations.get(p.agent.pid)
            command = ''
        else:
            # An agent that exited, or never got going (no network after a
            # reboot), leaves its pane at a shell. Forgetting its conversation
            # then would lose it at the next restart.
            conv = remembered.pane(p.session, p.pid, p.cwd)
            command = '' if conv else replayable(p.start)
        sessions.setdefault(p.session, []).append(Pane(p.cwd, p.title, conv, command, p.pid))
    tabs = tuple(
        replace(t, columns=tuple(
            c if c.tmux is not None else replace(c, conversation=(
                conversations.get(column_agents[c.pid].pid) if c.pid in column_agents else remembered.column(c.pid, c.cwd)))
            for c in t.columns))
        for t in tabs)
    return Snapshot(tabs, {k: tuple(v) for k, v in sessions.items()})


class Remembered:
    """The conversations of the previous record, by the shell that held them.

    A pane's or column's shell keeps its pid while the agent inside it comes
    and goes, so the pid (with the directory, and for a pane its session) says
    it is the same place. After a reboot pids start over; the directory and
    session name keep a reused pid from inheriting someone else's conversation.
    """

    def __init__(self, previous: Snapshot) -> None:
        self.panes = {(name, p.pid): p for name, panes in previous.sessions.items() for p in panes if p.pid}
        self.columns = {c.pid: c for t in previous.tabs for c in t.columns if c.pid}

    def pane(self, session: str, pid: int, cwd: str) -> Conversation | None:
        p = self.panes.get((session, pid))
        return p.conversation if p is not None and same_dir(p.cwd, cwd) else None

    def column(self, pid: int, cwd: str) -> Conversation | None:
        c = self.columns.get(pid)
        return c.conversation if c is not None and same_dir(c.cwd, cwd) else None


@dataclass(frozen=True)
class LivePane:
    session: str
    pid: int
    cwd: str
    title: str
    start: str
    agent: 'Agent | None'


def tmux_panes(procs: 'Processes') -> Iterator[LivePane]:
    fmt = '#{session_name}\t#{pane_pid}\t#{pane_current_path}\t#{pane_title}\t#{pane_start_command}'
    for line in tmux('list-panes', '-a', '-F', fmt).splitlines():
        name, pid, cwd, title, start = (line.split('\t') + [''] * 5)[:5]
        yield LivePane(name, int(pid), cwd, title, start, agent_in(procs.tree(int(pid)), cwd, procs))


def replayable(start_command: str) -> str:
    """The part of a pane's start command worth running again, or '' for a plain shell."""
    try:
        argv = shlex.split(start_command)
    except ValueError:
        return ''
    if len(argv) == 3 and argv[:2] == ['zsh', '-ic']:
        # One of ours: unwrap it, or every restore nests one more zsh.
        line = argv[2].removesuffix('; exec zsh')
    else:
        line = ' '.join(argv) if len(argv) != 1 else argv[0]
    line = re.sub(r'^cd (\'[^\']*\'|"[^"]*"|\S+)( && |; |$)', '', line).strip()
    # An agent's start command without the agent running means the
    # conversation ended and the pane is back at a shell; replaying it would
    # start a new one.
    if not line or any(agent in line for agent in AGENTS):
        return ''
    return line


# MARK: Which conversation an agent has open


@dataclass(frozen=True)
class Agent:
    """A running Claude Code / Codex / Kimi process."""
    kind: str
    pid: int
    cwd: str
    started: float  # epoch seconds
    cmd: str


def agent_in(tree: list[tuple[int, str]], cwd: str, procs: 'Processes') -> Agent | None:
    for pid, cmd in tree:
        if (kind := agent_kind(pid, cmd)) is not None:
            return Agent(kind, pid, cwd, procs.started.get(pid, 0.0), cmd)
    return None


def agent_kind(pid: int, cmd: str) -> str | None:
    parts = cmd.split()
    if not parts:
        return None
    exe = os.path.basename(parts[0])
    if claude_session_of(pid) is not None and 'claude' in cmd:
        return 'claude'
    if exe == 'codex' or '/codex ' in cmd:
        return 'codex'
    # kimi-code rewrites its argv, so it shows up as "kimi-code TERM=...".
    if exe in ('kimi', 'kimi-code') or '/kimi ' in cmd:
        return 'kimi'
    return None


def identify(agents: list[Agent]) -> dict[int, Conversation]:
    """どのエージェントがどの会話を開いているか。本人が言っていればそれ、言わなければ記録ファイルから推す。"""
    known = {a.pid: c for a in agents if (c := stated_conversation(a)) is not None}
    unknown = [a for a in agents if a.pid not in known]
    return known | guessed_from_transcripts(unknown, claimed={c.id for c in known.values()})


def stated_conversation(agent: Agent) -> Conversation | None:
    """The id the process itself carries: Claude's session file, `resume <id>`, `-S <id>`."""
    if agent.kind == 'claude':
        sid = claude_session_of(agent.pid)
        return Conversation('claude', sid) if sid else None
    if agent.kind == 'codex' and (m := re.search(r'\bresume\s+(' + UUID.pattern + ')', agent.cmd)):
        return Conversation('codex', m.group(1))
    if agent.kind == 'kimi' and (m := re.search(r'(?:-S|--session)[ =](' + KIMI_ID.pattern + ')', agent.cmd)):
        return Conversation('kimi', m.group(1))
    return None


@dataclass(frozen=True)
class Transcript:
    kind: str
    id: str
    cwd: str
    written: float  # mtime


def guessed_from_transcripts(agents: list[Agent], claimed: set[str]) -> dict[int, Conversation]:
    """A fresh agent, or one resumed from its picker, never says which conversation it has.

    What it does leave behind is the transcript it keeps writing: one in the
    agent's directory, written to since the agent started. When several agents
    share a directory, the most recently written transcripts go first, and each
    transcript to only one agent.
    """
    if not agents:
        return {}
    since = min(a.started for a in agents) - 60
    transcripts = [t for t in all_transcripts({a.kind for a in agents}, since) if t.id not in claimed]
    pairs = sorted(
        ((t, a) for t in transcripts for a in agents
         if t.kind == a.kind and same_dir(t.cwd, a.cwd) and t.written >= a.started - 5),
        key=lambda ta: ta[0].written, reverse=True)
    found: dict[int, Conversation] = {}
    used: set[str] = set()
    for t, a in pairs:
        if a.pid not in found and t.id not in used:
            found[a.pid] = Conversation(t.kind, t.id)
            used.add(t.id)
    return found


def all_transcripts(kinds: set[str], since: float) -> list[Transcript]:
    out: list[Transcript] = []
    if 'codex' in kinds:
        out += codex_transcripts(since)
    if 'kimi' in kinds:
        out += kimi_transcripts(since)
    return out


def codex_transcripts(since: float) -> Iterator[Transcript]:
    for dirpath, _, filenames in os.walk(CODEX_SESSIONS):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not name.startswith('rollout-') or not name.endswith('.jsonl') or (written := mtime(path)) < since:
                continue
            with suppress(OSError, ValueError, KeyError):
                with open(path) as f:
                    meta = json.loads(f.readline())['payload']
                # Only the terminal UI's own top-level threads: not subagents, and
                # not the conversations the desktop app imports from other agents.
                if meta.get('originator') == 'codex-tui' and not isinstance(meta.get('source'), dict):
                    yield Transcript('codex', meta['id'], meta['cwd'], written)


def kimi_transcripts(since: float) -> Iterator[Transcript]:
    with suppress(OSError):
        for workdir in os.listdir(KIMI_SESSIONS):
            with suppress(OSError):
                for session in os.listdir(os.path.join(KIMI_SESSIONS, workdir)):
                    state = os.path.join(KIMI_SESSIONS, workdir, session, 'state.json')
                    if (written := mtime(state)) < since:
                        continue
                    with suppress(OSError, ValueError, KeyError):
                        with open(state) as f:
                            data = json.load(f)
                        if not data.get('archived'):
                            yield Transcript('kimi', data['id'], data['cwd'], written)


def mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def same_dir(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def claude_session_of(pid: int) -> str | None:
    try:
        with open(os.path.join(CLAUDE_SESSIONS, f'{pid}.json')) as f:
            return str(json.load(f)['sessionId'])
    except (OSError, ValueError, KeyError):
        return None


class Processes:
    def __init__(self, rows: list[tuple[int, int, str]], started: dict[int, float] | None = None) -> None:
        self.cmd = {pid: cmd for pid, _, cmd in rows}
        self.started = started or {}
        self.children: dict[int, list[int]] = {}
        for pid, ppid, _ in rows:
            self.children.setdefault(ppid, []).append(pid)

    @classmethod
    def now(cls) -> 'Processes':
        # One process with a command line that is not UTF-8 must not sink the whole record.
        out = subprocess.run(['ps', '-axo', 'pid=,ppid=,etime=,command='], capture_output=True, text=True, errors='replace', timeout=COMMAND_TIMEOUT).stdout
        now = time.time()
        rows, started = [], {}
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3:
                pid = int(parts[0])
                rows.append((pid, int(parts[1]), parts[3] if len(parts) > 3 else ''))
                started[pid] = now - elapsed_seconds(parts[2])
        return cls(rows, started)

    def tree(self, pid: int) -> list[tuple[int, str]]:
        """pid itself and everything below it: the pane's own process can be the agent."""
        out, todo = [], [pid]
        while todo:
            p = todo.pop()
            if p in self.cmd:
                out.append((p, self.cmd[p]))
            todo += self.children.get(p, [])
        return out

    def running_conversations(self) -> set[str]:
        ids = set()
        try:
            names = os.listdir(CLAUDE_SESSIONS)
        except OSError:
            names = []
        for name in names:
            if name.endswith('.json') and name[:-5].isdigit():
                pid = int(name[:-5])
                # The files outlive their processes across a power cut, and the
                # pid may by now belong to something else.
                if 'claude' in self.cmd.get(pid, '') and (sid := claude_session_of(pid)):
                    ids.add(sid)
        for pid, cmd in self.cmd.items():
            if agent_kind(pid, cmd) in ('codex', 'kimi'):
                ids.update(UUID.findall(cmd))
                ids.update(KIMI_ID.findall(cmd))
        return ids


def elapsed_seconds(etime: str) -> int:
    """ps etime: [[dd-]hh:]mm:ss"""
    days, _, rest = etime.rpartition('-')
    secs = 0
    for part in rest.split(':'):
        secs = secs * 60 + int(part)
    return secs + int(days or 0) * 86400


def write_atomically(path: str, text: str) -> None:
    # The whole point is surviving a power cut, so a half-written file must
    # never replace a good one.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_snapshot(path: str) -> Snapshot | None:
    try:
        with open(path) as f:
            return Snapshot.from_json(json.load(f))
    except FileNotFoundError:
        return None


# MARK: Restoring


def restore(boss: 'Boss', snapshot: Snapshot, report: list[str]) -> None:
    """会話を tmux に戻してから、kitty のタブとカラムを元の並びで開き直す。何をしたかは report に書く。"""
    running = conversations_already_open()
    for name, panes in snapshot.sessions.items():
        outcome = [f'{name}: FAILED (see errors.log)']

        def bring_back(name: str = name, panes: tuple[Pane, ...] = panes, outcome: list[str] = outcome) -> None:
            outcome[0] = f'{name}: {bring_back_session(name, panes, running)}'

        best_effort(f'bring back tmux session {name}', bring_back)
        report.append(outcome[0])
    first_tab = boss.active_tab
    opened = [(tab, kitty_tab) for tab in snapshot.tabs if (kitty_tab := open_tab(boss, tab, running, report)) is not None]
    if opened and first_tab is not None:
        # The tab kitty started with is empty; the restored ones replace it.
        boss.close_tab_no_confirm(first_tab)
    for tab, kitty_tab in opened:
        if tab.active:
            boss.set_active_tab(kitty_tab)


def conversations_already_open() -> set[str]:
    """Which conversations are open somewhere already, so that they are not resumed twice.

    Only a safeguard: after a reboot nothing is open yet. Not knowing must
    not stop the restore, which is what happened when ps timed out at boot.
    """
    found: set[str] = set()

    def look() -> None:
        found.update(Processes.now().running_conversations())

    best_effort('list the conversations already open (restoring as if none were)', look)
    return found


def bring_back_session(name: str, panes: tuple[Pane, ...], running: set[str]) -> str:
    """Put one tmux session back, and say what was done."""
    conversations = sum(p.conversation is not None for p in panes)
    if not tmux_alive(name):
        create_session(name, [(p.cwd, p.restart_command(running)) for p in panes])
        return f'recreated, {len(panes)} panes, {conversations} conversations'
    # The session outlived kitty. Leave it alone, except where something
    # recreated it with a bare shell where a conversation should be.
    resumed = 0
    for index, (pane, current) in enumerate(zip(panes, live_pane_commands(name))):
        if pane.conversation is not None and current in SHELL_NAMES and pane.conversation.id not in running:
            tmux('send-keys', '-t', f'={name}:{index}', '-l', pane.restart_command(running), check=True)
            tmux('send-keys', '-t', f'={name}:{index}', 'Enter', check=True)
            running.add(pane.conversation.id)
            resumed += 1
    return f'alive, resumed {resumed} in a shell' if resumed else 'alive'


def open_tab(boss: 'Boss', tab: Tab, running: set[str], report: list[str]) -> 'KittyTab | None':
    kitty_tab = None
    for column in tab.columns:
        window = open_column(boss, column, kitty_tab, tab.title, running)
        if window is None:
            report.append(f'column {column.tmux or column.argv[:1]} in tab {tab.title!r}: not opened, its tmux session is gone')
            continue
        kitty_tab = window.tabref()
        if kitty_tab is not None and column.width is not None:
            set_column_width(kitty_tab, window, column.width)
    if kitty_tab is not None:
        kitty_tab.relayout()
    return kitty_tab


def open_column(boss: 'Boss', column: Column, into: 'KittyTab | None', tab_title: str, running: set[str]) -> Any:
    from .launch import launch, parse_launch_args
    if column.tmux is not None:
        if not tmux_alive(column.tmux):
            return None
        cmd = [tmux_executable(), 'attach-session', '-t', f'={column.tmux}']
    else:
        cmd = column.launch_command(running)
    args = ['--type=window', '--location=last'] if into is not None else ['--type=tab', '--tab-title', tab_title]
    if column.cwd:
        args += ['--cwd', column.cwd]
    if column.title and column.tmux is None:
        args += ['--title', column.title]
    opts, rest = parse_launch_args(args + cmd)
    return launch(boss, opts, rest, target_tab=into, force_target_tab=into is not None)


def set_column_width(kitty_tab: 'KittyTab', window: Any, cells: int) -> None:
    from .layout.strip import Strip
    layout = kitty_tab.current_layout
    group = kitty_tab.windows.group_for_window(window)
    if isinstance(layout, Strip) and group is not None:
        layout.set_column_width(group.id, cells)


# MARK: tmux


def tmux(*args: str, check: bool = False) -> str:
    # Always argv, never a shell line: zsh would read "-t =name" as "the path of
    # the command called name".
    res = subprocess.run([tmux_executable(), *args], capture_output=True, text=True, errors='replace', timeout=COMMAND_TIMEOUT)
    if check and res.returncode:
        # A session that silently fails to come back looks exactly like one
        # that was never recorded.
        raise RuntimeError(f'tmux {" ".join(args[:4])} failed ({res.returncode}): {res.stderr.strip()}')
    return res.stdout


def tmux_alive(name: str) -> bool:
    return subprocess.run([tmux_executable(), 'has-session', '-t', f'={name}'], capture_output=True, timeout=COMMAND_TIMEOUT).returncode == 0


def live_pane_commands(name: str) -> list[str]:
    return tmux('list-panes', '-s', '-t', f'={name}', '-F', '#{pane_current_command}').splitlines()


def create_session(name: str, panes: list[tuple[str, str]]) -> None:
    for index, (cwd, command) in enumerate(panes):
        # Through an interactive zsh so the c / x environment (direnv, PATH)
        # is there, and back to a shell when the program exits.
        where = ['new-session', '-d', '-s', name] if index == 0 else ['new-window', '-t', f'={name}:']
        tmux(*where, '-c', cwd, 'zsh', '-ic', f'{command}; exec zsh', check=True)


# MARK: Failure policy


def best_effort(what: str, action: Callable[[], None]) -> None:
    """One session or tab failing to come back must not stop the rest."""
    try:
        action()
    except Exception:
        import traceback

        from .utils import log_error
        message = f'[session_restore] could not {what}:\n{traceback.format_exc()}'
        log_error(message)
        # kitty started from the Dock has its stderr on /dev/null, so keep a copy
        # next to the record, where a failure to save can actually be seen.
        with suppress(OSError):
            os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
            with open(ERROR_LOG, 'a') as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}\n')
