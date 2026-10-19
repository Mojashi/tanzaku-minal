#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import subprocess
from unittest import TestCase
from unittest.mock import patch

from kitty.session_index import Hit
from kitty.strip_sidebar import tmux_session_for_hit, tmux_session_for_writer


def completed(args: list[str], stdout: str = '', returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, '')


class TestStripSidebar(TestCase):
    def test_finds_time_named_tmux_session_from_transcript_writer(self) -> None:
        results = (
            completed(['lsof'], '28577\n'),
            completed(
                ['ps'],
                '28551 2427 ttys057 zsh\n'
                '28553 28551 ttys057 node\n'
                '28577 28553 ttys057 /opt/codex/bin/codex\n',
            ),
            completed(['tmux'], 'codex-01a03244|14333|/dev/ttys094\ncodex-9772|28551|/dev/ttys057\n'),
        )
        with patch('kitty.strip_sidebar.os.path.isfile', return_value=True), patch(
            'kitty.strip_sidebar.subprocess.run', side_effect=results
        ):
            self.assertEqual(tmux_session_for_writer('/tmp/thread.jsonl', 'codex'), ('codex-9772', True))

    def test_live_writer_without_tmux_does_not_allow_resume(self) -> None:
        results = (
            completed(['lsof'], '42\n'),
            completed(['ps'], '42 1 ?? /opt/codex/bin/codex\n'),
            completed(['tmux'], 'codex-other|100|/dev/ttys010\n'),
        )
        hit = Hit('01a03244-eb36-7a40-8e97-d3f56823fa92', 'codex', '/tmp', 'user', '', 'text', path='/tmp/thread.jsonl')
        with patch('kitty.strip_sidebar.os.path.isfile', return_value=True), patch(
            'kitty.strip_sidebar.subprocess.run', side_effect=results
        ), patch('kitty.strip_sidebar.tmux_has') as tmux_has:
            self.assertEqual(tmux_session_for_hit(hit), ('', True))
            tmux_has.assert_not_called()

    def test_live_writer_still_blocks_resume_when_tmux_inspection_fails(self) -> None:
        results = (
            completed(['lsof'], '42\n'),
            completed(['ps'], '42 1 ?? /opt/codex/bin/codex\n'),
            completed(['tmux'], returncode=1),
        )
        with patch('kitty.strip_sidebar.os.path.isfile', return_value=True), patch(
            'kitty.strip_sidebar.subprocess.run', side_effect=results
        ):
            self.assertEqual(tmux_session_for_writer('/tmp/thread.jsonl', 'codex'), ('', True))

    def test_non_agent_reader_is_not_mistaken_for_writer(self) -> None:
        results = (
            completed(['lsof'], '99\n'),
            completed(['ps'], '99 1 ?? /usr/bin/python3\n'),
        )
        with patch('kitty.strip_sidebar.os.path.isfile', return_value=True), patch(
            'kitty.strip_sidebar.subprocess.run', side_effect=results
        ):
            self.assertEqual(tmux_session_for_writer('/tmp/thread.jsonl', 'codex'), ('', False))

    def test_finds_tmux_session_from_writer_tty_when_parent_walk_cannot(self) -> None:
        results = (
            completed(['lsof'], '42\n'),
            completed(['ps'], '42 999 ttys025 /opt/codex/bin/codex\n'),
            completed(['tmux'], 'codex-1329|100|/dev/ttys025\n'),
        )
        with patch('kitty.strip_sidebar.os.path.isfile', return_value=True), patch(
            'kitty.strip_sidebar.subprocess.run', side_effect=results
        ):
            self.assertEqual(tmux_session_for_writer('/tmp/thread.jsonl', 'codex'), ('codex-1329', True))
