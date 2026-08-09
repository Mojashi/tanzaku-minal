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
import shlex
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
    first_ts TEXT, last_ts TEXT, msg_count INTEGER, summary TEXT,
    -- How the session stopped: '' finished, 'error' died on an API failure,
    -- 'cut' ended mid-turn with the agent never answering, 'stopped' was
    -- interrupted on purpose.
    ending TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_last ON sessions(last_ts DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, session TEXT, source TEXT, role TEXT, ts TEXT, text TEXT
);
-- Ordered, because every lookup by session wants the latest message in it.
-- Without the ts half, one session costs a sort of everything it ever said,
-- and listing a project means paying that three hundred times.
CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session, ts DESC);
DROP INDEX IF EXISTS idx_messages_session;  -- subsumed by the above
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

#: Text that both agents inject into the conversation before the person says
#: anything. A session summary made of this says nothing about the session.
BOILERPLATE = (
    '<recommended_plugins', '<environment_context', '<permissions', '<user_instructions',
    '# AGENTS.md', '<system-reminder', 'Caveat: The messages below', '<command-name>',
    '<local-command', '<ide_', 'Analyze this codebase', '[Recent context]',
    'Caveat:', '<command-message>', 'DO NOT respond to these messages',
)


def is_boilerplate(text: str) -> bool:
    head = text.lstrip()[:400]
    for marker in BOILERPLATE:
        # Not startswith: injected blocks are often wrapped in another tag, or
        # follow one, so they turn up a little way in rather than at the front.
        if marker in head:
            return True
    return False


MAX_TEXT = 1200  # a single tool result can be megabytes; the tail is never the searchable part
MIN_FREE_BYTES = 3 * 1024 ** 3  # stop indexing rather than fill the disk
RECENT_SCAN_ROWS = 150_000  # how far back a sub-trigram query scans


def free_bytes() -> int:
    try:
        st = os.statvfs(os.path.dirname(DB_PATH))
        return st.f_bavail * st.f_frsize
    except OSError:
        return 1 << 62


#: Markers an agent writes when the person stops it on purpose. Ending on one of
#: these means the session was abandoned deliberately, which is not the same as
#: being cut off, and telling the two apart is the whole point of the flag.
DELIBERATE_STOP = (
    '<turn_aborted>', '[Request interrupted by user',
)


class Hit(NamedTuple):
    session: str
    source: str
    cwd: str
    role: str
    ts: str
    text: str
    summary: str = ''   # what the session was asked to do
    msg_count: int = 0
    hits: int = 1       # matches in this session, once results are grouped
    ending: str = ''    # '', 'error', 'cut' or 'stopped'


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


def claude_messages(path: str, start_line: int) -> Iterator[tuple[str, str, str, str, bool]]:
    """(role, ts, text, cwd, is_error) for a Claude Code session file."""
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
                # Claude marks these itself, so a session that died on a
                # connection failure is a fact rather than a guess.
                yield (role, d.get('timestamp') or '', text[:MAX_TEXT], d.get('cwd') or '',
                       bool(d.get('isApiErrorMessage')))


def codex_messages(path: str, start_line: int) -> Iterator[tuple[str, str, str, str, bool]]:
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
                yield (role, d.get('timestamp') or payload.get('timestamp') or '',
                       text[:MAX_TEXT], cwd, False)


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
            last_role = last_text = ''
            last_error = False
            for n, (role, ts, text, c, err) in enumerate(reader(path, start_line)):
                cwd = c or cwd
                last_role, last_text, last_error = role, text, err
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
            if last_error:
                ending = 'error'
            elif any(m in last_text for m in DELIBERATE_STOP):
                ending = 'stopped'
            elif last_role == 'user' and not is_boilerplate(last_text):
                ending = 'cut'
            else:
                ending = ''
            summary = ''
            for _s, _so, role, _t, text in rows:
                if role == 'user' and not is_boilerplate(text):
                    summary = ' '.join(text.split())[:200]
                    break
            db.execute(
                '''INSERT INTO sessions(session, source, cwd, path, first_ts, last_ts, msg_count, summary, ending)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session) DO UPDATE SET
                     cwd=COALESCE(NULLIF(excluded.cwd,''), sessions.cwd),
                     last_ts=COALESCE(NULLIF(excluded.last_ts,''), sessions.last_ts),
                     msg_count=excluded.msg_count,
                     summary=COALESCE(NULLIF(sessions.summary,''), excluded.summary),
                     ending=excluded.ending''',
                (session, source, cwd, path, first_ts, last_ts, count, summary, ending))
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


def like_arg(term: str) -> str:
    return '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def terms_of(query: str) -> list[str]:
    """The words a query is asking for, all of which have to appear.

    Quotes keep a phrase together, for the times when the space is the point.
    Shared with the caller that highlights the results, so what is marked is
    exactly what was matched.
    """
    try:
        terms = shlex.split(query)
    except ValueError:  # an unbalanced quote, mid-typing
        terms = query.replace('"', ' ').split()
    return [t for t in terms if t]


def recent_sessions(db: sqlite3.Connection, cwd: str, source: str, limit: int) -> list[Hit]:
    """The latest sessions in a project, newest first.

    Asked as "which sessions" rather than "which messages". Walking messages
    newest first and keeping the ones whose session is in this project means
    reading the whole corpus whenever the project has not been touched lately:
    a project last used a month ago took 17s, while today's took 0.3s, for the
    same question. There are 50k sessions against a million messages, and only
    one message per session is ever shown.
    """
    inner = 'SELECT session, source, cwd, summary, msg_count, ending, last_ts FROM sessions WHERE 1=1'
    args: list[Any] = []
    if cwd:
        inner += ' AND cwd LIKE ?'
        args.append(f'%{cwd}%')
    if source:
        inner += ' AND source = ?'
        args.append(source)
    # Filter first, then sort what is left. Sorting by the index and filtering
    # as it goes has to walk the whole table when the project was last used a
    # while ago -- the rows it wants are at the far end. 1.4s against 0.04s.
    sql = f'SELECT * FROM ({inner}) ORDER BY last_ts DESC LIMIT ?'
    args.append(limit)
    try:
        rows = db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for session, src, scwd, summary, msg_count, ending, last_ts in rows:
        try:
            latest = db.execute(
                'SELECT role, ts, text FROM messages WHERE session = ? ORDER BY ts DESC LIMIT 1',
                (session,)).fetchone()
        except sqlite3.Error:
            latest = None
        role, ts, text = latest if latest else ('', last_ts or '', '')
        out.append(Hit(session, src or '', scwd or '', role or '', ts or last_ts or '',
                       text or '', summary or '', msg_count or 0, 1, ending or ''))
    return out


def search(query: str, limit: int = 200, source: str = '', cwd: str = '') -> list[Hit]:
    query = query.strip()
    if not query and not cwd:
        return []
    try:
        db = connect(readonly=True)
    except sqlite3.Error:
        return []
    # Words separated by spaces mean all of them, each matched as a substring --
    # the way every other search box works. Matching the whole string including
    # its spaces, which is what one quoted phrase does, made any query of more
    # than one word return nothing at all.
    terms = terms_of(query)
    # Only three characters or more can be looked up in a trigram index. Shorter
    # terms ride along as a filter over whatever the longer ones found, so a two
    # character word costs nothing as long as it has company.
    indexable = [t for t in terms if len(t) >= 3]
    args: list[Any]
    if not terms:
        # Project filter on its own: the most recent sessions in it, which is a
        # useful way in even when you cannot remember any of the words.
        try:
            return recent_sessions(db, cwd, source, limit)
        finally:
            db.close()
    if not indexable:
        # Nothing long enough to look up, so there is no way around reading the
        # text -- bounded to recent history, because a scan of the whole corpus
        # takes tens of seconds and this runs on every keystroke.
        row = db.execute(
            'SELECT ts FROM messages WHERE ts != \'\' ORDER BY ts DESC LIMIT 1 OFFSET ?',
            (RECENT_SCAN_ROWS,)).fetchone()
        floor = row[0] if row else ''
        sql = '''SELECT m.session, m.source, COALESCE(s.cwd,''), m.role, m.ts, m.text,
                        COALESCE(s.summary,''), COALESCE(s.msg_count,0), COALESCE(s.ending,'')
                 FROM messages m
                 LEFT JOIN sessions s ON s.session = m.session
                 WHERE m.ts > ?'''
        args = [floor]
    else:
        sql = '''SELECT m.session, m.source, COALESCE(s.cwd,''), m.role, m.ts, m.text,
                        COALESCE(s.summary,''), COALESCE(s.msg_count,0), COALESCE(s.ending,'')
                 FROM messages_fts f
                 JOIN messages m ON m.id = f.rowid
                 LEFT JOIN sessions s ON s.session = m.session
                 WHERE messages_fts MATCH ?'''
        args = [' AND '.join(escape(t) for t in indexable)]
    # The index answers with trigrams, which is a superset: detail=none cannot
    # tell a phrase from the same trigrams scattered about. Every term is
    # therefore checked as a real substring here, which is also what makes the
    # highlighting in the results honest.
    for term in terms:
        sql += " AND m.text LIKE ? ESCAPE '\\'"
        args.append(like_arg(term))
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
    # Rows are messages but results are sessions, and one busy session can
    # easily contribute hundreds of rows, so fetch well past the target.
    args.append(max(limit * 40, 2000))
    try:
        rows = db.execute(sql, args).fetchall()
    except sqlite3.Error:
        rows = []
    db.close()
    # One row per conversation. Ten hits in one session are one answer to
    # "which conversation was this", not ten, and they used to crowd out every
    # other session in the results.
    order: list[str] = []
    best: dict[str, Hit] = {}
    counts: dict[str, int] = {}
    for r in rows:
        hit = Hit(*r)
        counts[hit.session] = counts.get(hit.session, 0) + 1
        if hit.session not in best:
            best[hit.session] = hit
            order.append(hit.session)
            if len(order) >= limit:
                break
    return [best[s]._replace(hits=counts[s]) for s in order]


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
