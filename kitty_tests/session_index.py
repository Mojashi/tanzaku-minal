#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

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
