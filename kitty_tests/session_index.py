#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

import json
import os
import tempfile
from unittest import TestCase
from unittest.mock import patch

from kitty import session_index


class TestSessionIndex(TestCase):
    def test_finds_words_longer_than_one_trigram(self) -> None:
        # detail=none refuses phrase queries, and a word of four characters or
        # more is one. The error used to come back as an empty result.
        with tempfile.TemporaryDirectory() as tdir, patch.object(session_index, 'DB_PATH', os.path.join(tdir, 'index.db')):
            db = session_index.connect()
            with db:
                db.execute("INSERT INTO sessions(session, source, cwd, last_ts) VALUES ('s1', 'claude', '/x', '2026')")
                db.executemany('INSERT INTO messages(session, source, role, ts, text) VALUES (?,?,?,?,?)', [
                    ('s1', 'claude', 'user', '2026-01-01', 'セッション検索が壊れてる'),
                    ('s1', 'claude', 'assistant', '2026-01-02', 'the strip layout'),
                ])
            db.close()
            self.assertEqual([h.session for h in session_index.search('strip')], ['s1'])
            self.assertEqual([h.session for h in session_index.search('セッション 検索')], ['s1'])
            self.assertEqual([h.session for h in session_index.search('"strip layout"')], ['s1'])
            # Every trigram of "layout strip" is present, but not the substring.
            self.assertEqual(session_index.search('"layout strip"'), [])

    def test_indexes_kimi_sessions_with_their_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tdir, \
                patch.object(session_index, 'DB_PATH', os.path.join(tdir, 'index.db')), \
                patch.object(session_index, 'CLAUDE_DIR', os.path.join(tdir, 'none')), \
                patch.object(session_index, 'CODEX_DIR', os.path.join(tdir, 'none')), \
                patch.object(session_index, 'AGY_BRAIN_DIR', os.path.join(tdir, 'none')), \
                patch.object(session_index, 'KIMI_DIR', os.path.join(tdir, 'kimi')):
            sid = 'session_3acd8599-9fd5-43a1-849a-1219f1f3f4b7'
            sdir = os.path.join(tdir, 'kimi', 'wd_proj_5bf4072ba02b', sid)

            def message(role: str, ms: int, text: str, source: str = '', **extra: object) -> dict[str, object]:
                msg = {'role': role, 'content': [{'type': 'think', 'think': 'hidden'}, {'type': 'text', 'text': text}], **extra}
                return {'type': 'agent.message.appended', 'time': ms,
                        'message': {'message': msg, 'meta': {'source': source}}}

            def wire(agent: str, *records: dict[str, object]) -> None:
                os.makedirs(os.path.join(sdir, 'agents', agent))
                with open(os.path.join(sdir, 'agents', agent, 'wire.jsonl'), 'w') as f:
                    f.write('{"type":"metadata"}\n')
                    for r in records:
                        f.write(json.dumps(r) + '\n')

            # A subagent sorts before main by name, which must not make its
            # prompt the session's summary or its ending the session's.
            wire('agent-0', message('user', 1792593348000, 'look into ARTEX please', 'input'),
                 message('assistant', 1792593349000, 'ARTEX is done'))
            wire('main', message('user', 1792593340000, 'investigate the vendor', 'input'),
                 message('assistant', 1792593341000, '', toolCalls=[{'name': 'AgentSwarm'}]),
                 message('user', 1792593342000, '<notification> task done', 'notify'),
                 {'type': 'agent.turn.ended', 'outcome': 'failed', 'time': 1792593343000})
            with open(os.path.join(sdir, 'state.json'), 'w') as f:
                json.dump({'id': sid, 'cwd': '/repos/proj'}, f)

            self.assertEqual(session_index.update(), (2, 0))
            hits = session_index.search('artex')
            self.assertEqual([(h.session, h.source, h.cwd) for h in hits], [(sid, 'kimi', '/repos/proj')])
            self.assertEqual(hits[0].summary, 'investigate the vendor')
            self.assertEqual(hits[0].ts, '2026-10-21T14:35:49.000Z')
            self.assertEqual(hits[0].ending, 'error')
            self.assertTrue(hits[0].path.endswith('/agents/main/wire.jsonl'))
            self.assertEqual(session_index.search('hidden'), [])
            self.assertEqual(len(session_index.search('[AgentSwarm]')), 1)
