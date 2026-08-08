#!/usr/bin/env python3
# License: GPLv3 Copyright: 2026, strip layout prototype

"""A full text index over Claude Code and Codex sessions.

Both agents keep their history as JSONL, one file per session, in formats that
have changed over time -- so the extraction here is deliberately shape driven
rather than schema driven: anything that looks like a message with a role and
some text is indexed, and everything else is skipped.

The index is FTS5 with the trigram tokenizer, which is what makes substring and
Japanese queries work; the default tokenizer would only match whole ASCII words.
Indexing is incremental on (size, mtime), and files only ever grow, so a session
that is still being written is re-read from where it left off.

    python3 -m kitty.session_index --update      # bring the index up to date
    python3 -m kitty.session_index --search foo  # query it
"""

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

CLAUDE_DIR = os.path.expanduser('~/.claude/projects')
CODEX_DIR = os.path.expanduser('~/.codex/sessions')
DB_PATH = os.path.expanduser('~/.cache/kitty-session-search/index.db')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, msg_count INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    session TEXT PRIMARY KEY, source TEXT, cwd TEXT, path TEXT,
    first_ts TEXT, last_ts TEXT, msg_count INTEGER, summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_last ON sessions(last_ts DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session TEXT, source TEXT, role TEXT, ts TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session);
-- id is insertion order, which is the order files happened to be indexed in,
-- not time. Bounded scans need the real thing.
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
-- detail=none drops the per-token position lists, which for a trigram index
-- over this much text is most of the size. Phrase and NEAR queries stop working;
-- substring matching, which is all this is used for, does not.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='trigram', detail=none
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
'''

MAX_TEXT = 1200  # a single tool result can be megabytes; the tail is never the searchable part
MIN_FREE_BYTES = 3 * 1024 ** 3  # stop indexing rather than fill the disk
RECENT_SCAN_ROWS = 150_000  # how far back a sub-trigram query scans


def free_bytes() -> int:
    try:
        st = os.statvfs(os.path.dirname(DB_PATH))
        return st.f_bavail * st.f_frsize
    except OSError:
        return 1 << 62


class Hit(NamedTuple):
    session: str
    source: str
    cwd: str
    role: str
    ts: str
    text: str


def connect(readonly: bool = False) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if readonly:
        db = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=2)
    else:
        db = sqlite3.connect(DB_PATH, timeout=30)
        db.executescript(SCHEMA)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    return db


# MARK: extraction

def flatten(content: Any) -> str:
    """Text out of the many shapes a message body takes."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ('text', 'content', 'input', 'output'):
            if key in content:
                return flatten(content[key])
        return ''
    if isinstance(content, list):
        return '\n'.join(filter(None, (flatten(c) for c in content)))
    if content is None or isinstance(content, bool):
        return ''
    return str(content)


def claude_messages(path: str, start_line: int) -> Iterator[tuple[str, str, str, str]]:
    """(role, ts, text, cwd) for a Claude Code session file."""
    with open(path, encoding='utf-8', errors='replace') as f:
        for i, line in enumerate(f):
            if i < start_line or not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            msg = d.get('message')
            if not isinstance(msg, dict):
                continue
            role = msg.get('role') or d.get('type') or ''
            text = flatten(msg.get('content'))
            if text:
                yield role, d.get('timestamp') or '', text[:MAX_TEXT], d.get('cwd') or ''


def codex_messages(path: str, start_line: int) -> Iterator[tuple[str, str, str, str]]:
    """(role, ts, text, cwd) for a Codex rollout file, old and new layouts."""
    cwd = ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for i, line in enumerate(f):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            payload = d.get('payload') if isinstance(d.get('payload'), dict) else d
            if d.get('type') == 'session_meta' or 'cwd' in payload:
                cwd = payload.get('cwd') or cwd
            if i < start_line:
                continue
            if payload.get('type') not in ('message', None):
                continue
            role = payload.get('role') or ''
            if not role:
                continue
            text = flatten(payload.get('content'))
            if text:
                yield role, d.get('timestamp') or payload.get('timestamp') or '', text[:MAX_TEXT], cwd


def session_files() -> Iterator[tuple[str, str, str]]:
    """(path, source, session_id) for everything worth indexing."""
    for root, _dirs, names in os.walk(CLAUDE_DIR):
        for name in names:
            if name.endswith('.jsonl'):
                yield os.path.join(root, name), 'claude', name[:-6]
    for root, _dirs, names in os.walk(CODEX_DIR):
        for name in names:
            if not name.endswith('.jsonl'):
                continue
            # rollout-<iso timestamp>-<uuid>.jsonl
            stem = name[:-6]
            session = stem[-36:] if len(stem) > 36 else stem
            yield os.path.join(root, name), 'codex', session


# MARK: indexing

def update(progress: bool = False, budget: float = 0.0) -> tuple[int, int]:
    """Index anything new. Returns (files done, files left)."""
    db = connect()
    known = {p: (s, m, c) for p, s, m, c in db.execute('SELECT path, size, mtime, msg_count FROM files')}
    todo = []
    for path, source, session in session_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        prev = known.get(path)
        if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 0.001:
            continue
        todo.append((path, source, session, st.st_size, st.st_mtime, prev[2] if prev else 0))

    started = time.monotonic()
    done = 0
    for path, source, session, size, mtime, start_line in todo:
        if budget and time.monotonic() - started > budget:
            break
        # A trigram index over this much text is big enough to matter, and
        # filling the disk is far worse than an index that is behind.
        if done % 200 == 0 and free_bytes() < MIN_FREE_BYTES:
            break
        reader = claude_messages if source == 'claude' else codex_messages
        rows = []
        cwd = ''
        first_ts = last_ts = ''
        try:
            for n, (role, ts, text, c) in enumerate(reader(path, start_line)):
                cwd = c or cwd
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                rows.append((session, source, role, ts, text))
        except OSError:
            continue
        count = start_line + len(rows)
        with db:
            if rows:
                db.executemany(
                    'INSERT INTO messages(session, source, role, ts, text) VALUES (?,?,?,?,?)', rows)
            summary = next((t for _s, _so, r, _t, t in rows if r == 'user'), '')[:200]
            db.execute(
                '''INSERT INTO sessions(session, source, cwd, path, first_ts, last_ts, msg_count, summary)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(session) DO UPDATE SET
                     cwd=COALESCE(NULLIF(excluded.cwd,''), sessions.cwd),
                     last_ts=COALESCE(NULLIF(excluded.last_ts,''), sessions.last_ts),
                     msg_count=excluded.msg_count,
                     summary=COALESCE(NULLIF(sessions.summary,''), excluded.summary)''',
                (session, source, cwd, path, first_ts, last_ts, count, summary))
            db.execute('INSERT OR REPLACE INTO files(path, size, mtime, msg_count) VALUES (?,?,?,?)',
                       (path, size, mtime, count))
        done += 1
        if progress and done % 500 == 0:
            print(f'{done}/{len(todo)}', file=sys.stderr, flush=True)
    db.close()
    return done, len(todo) - done


# MARK: querying

def escape(query: str) -> str:
    # trigram FTS wants a quoted string; embedded quotes are doubled
    return '"' + query.replace('"', '""') + '"'


def search(query: str, limit: int = 200, source: str = '', cwd: str = '') -> list[Hit]:
    query = query.strip()
    if not query and not cwd:
        return []
    try:
        db = connect(readonly=True)
    except sqlite3.Error:
        return []
    args: list[Any]
    if not query:
        # Project filter on its own: the most recent thing said in it, which is
        # a useful way in even when you cannot remember any of the words.
        sql = '''SELECT m.session, m.source, COALESCE(s.cwd,''), m.role, m.ts, m.text
                 FROM messages m
                 JOIN sessions s ON s.session = m.session
                 WHERE 1=1'''
        args = []
    elif len(query) < 3:
        # The trigram tokenizer cannot index anything shorter than three
        # characters, which rules out most two character Japanese words. Scan
        # instead -- but bounded to recent history, because a scan of the whole
        # corpus takes tens of seconds and this runs on every keystroke.
        row = db.execute(
            'SELECT ts FROM messages WHERE ts != \'\' ORDER BY ts DESC LIMIT 1 OFFSET ?',
            (RECENT_SCAN_ROWS,)).fetchone()
        floor = row[0] if row else ''
        sql = '''SELECT m.session, m.source, COALESCE(s.cwd,''), m.role, m.ts, m.text
                 FROM messages m
                 LEFT JOIN sessions s ON s.session = m.session
                 WHERE m.ts > ? AND m.text LIKE ? ESCAPE '\\' '''
        args = [floor, '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%']
    else:
        sql = '''SELECT m.session, m.source, COALESCE(s.cwd,''), m.role, m.ts, m.text
                 FROM messages_fts f
                 JOIN messages m ON m.id = f.rowid
                 LEFT JOIN sessions s ON s.session = m.session
                 WHERE messages_fts MATCH ?'''
        args = [escape(query)]
    if source:
        sql += ' AND m.source = ?'
        args.append(source)
    if cwd:
        sql += ' AND s.cwd LIKE ?'
        args.append(f'%{cwd}%')
    # No GROUP BY: it would force the whole result set to be materialised
    # before LIMIT could apply, which on the substring path means scanning every
    # message. Duplicates are collapsed below instead, where it costs nothing.
    sql += ' ORDER BY m.ts DESC LIMIT ?'
    args.append(limit * 3)
    try:
        rows = db.execute(sql, args).fetchall()
    except sqlite3.Error:
        rows = []
    db.close()
    seen = set()
    ans = []
    for r in rows:
        hit = Hit(*r)
        key = (hit.session, hit.text)
        if key in seen:
            continue
        seen.add(key)
        ans.append(hit)
        if len(ans) >= limit:
            break
    return ans


def stats() -> tuple[int, int]:
    try:
        db = connect(readonly=True)
    except sqlite3.Error:
        return 0, 0
    try:
        m = db.execute('SELECT count(*) FROM messages').fetchone()[0]
        s = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
    except sqlite3.Error:
        m = s = 0
    db.close()
    return m, s


def main() -> None:
    args = sys.argv[1:]
    if '--update' in args:
        done, left = update(progress=True)
        m, s = stats()
        print(f'indexed {done} files, {left} left; {m} messages in {s} sessions')
    elif '--search' in args:
        q = args[args.index('--search') + 1]
        for h in search(q, limit=20):
            print(f'{h.source} {h.session[:8]} {h.role:9} {h.cwd}\n    {h.text[:120]!r}')
    else:
        m, s = stats()
        print(f'{m} messages in {s} sessions at {DB_PATH}')


if __name__ == '__main__':
    main()
