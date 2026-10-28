#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from unittest import TestCase
from unittest.mock import patch

from kitty import session_restore as sr
from kitty.session_restore import Column, Conversation, Pane, Processes, Snapshot, Tab

CLAUDE_ID = '9e00d11e-0f1b-41ab-a5d9-37031d7d9f79'
CODEX_ID = '01a14ef4-2ad2-7893-b7cc-330b0e1a1b8d'
KIMI_ID = 'session_2f2dca16-36fe-46ae-946b-78aca92caa3f'


def recording(calls: list[tuple[str, ...]]) -> Callable[..., str]:
    def tmux(*args: str, check: bool = False) -> str:
        calls.append(args)
        return ''
    return tmux


class TestSessionRestore(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude_sessions = os.path.join(self.tmp.name, 'sessions')
        os.mkdir(self.claude_sessions)
        p = patch.object(sr, 'CLAUDE_SESSIONS', self.claude_sessions)
        p.start()
        self.addCleanup(p.stop)

    def live_claude(self, pid: int, sid: str) -> None:
        with open(os.path.join(self.claude_sessions, f'{pid}.json'), 'w') as f:
            json.dump({'pid': pid, 'sessionId': sid}, f)

    def test_snapshot_survives_a_round_trip_through_json(self) -> None:
        snap = Snapshot(
            (Tab('pptx', (Column(80, tmux='claude-4963'), Column(None, None, '/tmp', ('kimi-code',), 'c', 't', 9, Conversation('kimi', KIMI_ID))), True),),
            {'claude-4963': (Pane('/repo', 'title', Conversation('claude', CLAUDE_ID)), Pane('/repo', command='./listen.py'))},
        )
        self.assertEqual(Snapshot.from_json(json.loads(json.dumps(snap.to_json()))), snap)

    def test_finds_claude_in_the_pane_process_itself(self) -> None:
        # conductor runs claude as the pane's own process, not below a shell.
        self.live_claude(98975, CLAUDE_ID)
        procs = Processes([(98975, 4204, 'claude --name conductor x'), (50686, 98975, 'bash -c watch')])
        agent = sr.agent_in(procs.tree(98975), '/c', procs)
        assert agent is not None
        self.assertEqual(sr.identify([agent]), {98975: Conversation('claude', CLAUDE_ID)})

    def test_codex_and_kimi_ids_come_from_their_command_line_first(self) -> None:
        codex = sr.Agent('codex', 2, '/d', 0, f'node /x/bin/codex --flag resume {CODEX_ID}')
        # kimi-code rewrites its argv, but one started with -S keeps the id.
        kimi = sr.Agent('kimi', 3, '/d', 0, f'kimi -S {KIMI_ID}')
        self.assertEqual(sr.identify([codex, kimi]), {2: Conversation('codex', CODEX_ID), 3: Conversation('kimi', KIMI_ID)})

    def test_kimi_is_recognised_under_its_rewritten_argv(self) -> None:
        self.assertEqual(sr.agent_kind(9761, 'kimi-code TERM=xterm-256color TERMINFO=/x'), 'kimi')
        self.assertEqual(sr.agent_kind(1, '/Users/m/.bun/install/global/node_modules/@openai/codex-darwin-arm64/vendor/bin/codex'), 'codex')
        self.assertIsNone(sr.agent_kind(1, '-zsh'))

    def test_codex_without_an_id_is_matched_to_the_transcript_it_writes(self) -> None:
        codex = os.path.join(self.tmp.name, 'codex', '2026', '10', '19')
        os.makedirs(codex)

        def rollout(tid: str, cwd: str, written: float, originator: str = 'codex-tui', source: object = 'vscode') -> None:
            path = os.path.join(codex, f'rollout-x-{tid}.jsonl')
            with open(path, 'w') as f:
                f.write(json.dumps({'payload': {'id': tid, 'cwd': cwd, 'originator': originator, 'source': source}}) + '\n')
            os.utime(path, (written, written))

        rollout('mine', '/maic', 1000)
        rollout('older', '/maic', 400)  # last written before the agent started
        rollout('imported', '/maic', 2000, originator='Codex Desktop')
        rollout('subagent', '/maic', 2000, source={'subagent': {}})
        rollout('elsewhere', '/finder', 2000)
        agent = sr.Agent('codex', 7, '/maic', 500, 'node /x/bin/codex --dangerously-bypass-approvals-and-sandbox resume')
        with patch.object(sr, 'CODEX_SESSIONS', os.path.join(self.tmp.name, 'codex')):
            self.assertEqual(sr.identify([agent]), {7: Conversation('codex', 'mine')})

    def test_two_agents_in_one_directory_do_not_get_the_same_conversation(self) -> None:
        kimi = os.path.join(self.tmp.name, 'kimi', 'wd_a')
        for sid, written in ((KIMI_ID, 1000), ('session_other', 900)):
            os.makedirs(os.path.join(kimi, sid))
            path = os.path.join(kimi, sid, 'state.json')
            with open(path, 'w') as f:
                json.dump({'id': sid, 'cwd': '/a', 'archived': False}, f)
            os.utime(path, (written, written))
        stated = sr.Agent('kimi', 1, '/a', 0, f'kimi -S {KIMI_ID}')
        fresh = sr.Agent('kimi', 2, '/a', 100, 'kimi-code TERM=x')
        with patch.object(sr, 'KIMI_SESSIONS', os.path.join(self.tmp.name, 'kimi')):
            self.assertEqual(sr.identify([stated, fresh]), {1: Conversation('kimi', KIMI_ID), 2: Conversation('kimi', 'session_other')})

    def test_ps_elapsed_time_is_read_in_all_its_shapes(self) -> None:
        self.assertEqual([sr.elapsed_seconds(x) for x in ('06:37', '01:02:03', '2-01:02:03')], [397, 3723, 2 * 86400 + 3723])

    def test_a_column_outside_tmux_resumes_its_conversation(self) -> None:
        col = Column(None, None, '/a', ('kimi-code',), pid=9, conversation=Conversation('kimi', KIMI_ID))
        self.assertEqual(col.launch_command(set()), ['zsh', '-ic', f'{sr.WAIT_FOR_NETWORK}cd /a && kimi -S {KIMI_ID}; exec zsh'])
        # Already open somewhere else: start the program, do not fork the conversation.
        self.assertNotIn(KIMI_ID, ' '.join(col.launch_command({KIMI_ID})))

    def test_stale_claude_session_file_is_not_a_running_conversation(self) -> None:
        # After a power cut the file is still there and its pid is someone else's.
        self.live_claude(1234, CLAUDE_ID)
        self.assertEqual(Processes([(1234, 1, '/usr/sbin/cfprefsd')]).running_conversations(), set())
        self.assertEqual(Processes([(1234, 1, 'claude -r x')]).running_conversations(), {CLAUDE_ID})

    def test_a_conversation_open_elsewhere_is_not_resumed_twice(self) -> None:
        pane = Pane('/repo', conversation=Conversation('claude', CLAUDE_ID))
        self.assertIn(f'-r {CLAUDE_ID}', pane.restart_command(set()))
        self.assertEqual(pane.restart_command({CLAUDE_ID}), 'cd /repo')

    def test_dead_session_is_created_again_with_conversations_resumed(self) -> None:
        panes = (Pane('/a', conversation=Conversation('codex', CODEX_ID)), Pane('/b'))
        calls: list[tuple[str, ...]] = []
        with patch.object(sr, 'tmux_alive', return_value=False), patch.object(sr, 'tmux', side_effect=recording(calls)):
            sr.bring_back_session('codex-5978', panes, set())
        self.assertEqual(calls[0][:6], ('new-session', '-d', '-s', 'codex-5978', '-c', '/a'))
        self.assertIn(f'resume {CODEX_ID}; exec zsh', calls[0][-1])
        self.assertEqual(calls[1][:4], ('new-window', '-t', '=codex-5978:', '-c'))

    def test_live_session_with_bare_shell_gets_its_conversation_back(self) -> None:
        panes = (Pane('/a', conversation=Conversation('claude', CLAUDE_ID)), Pane('/b', conversation=Conversation('claude', 'other')))
        calls: list[tuple[str, ...]] = []
        with patch.object(sr, 'tmux_alive', return_value=True), patch.object(
            sr, 'live_pane_commands', return_value=['zsh', '2.1.302']
        ), patch.object(sr, 'tmux', side_effect=recording(calls)):
            sr.bring_back_session('claude-9e00d11e', panes, set())
        # Only the pane that fell back to a shell; the other is still running.
        self.assertEqual([c[:3] for c in calls], [('send-keys', '-t', '=claude-9e00d11e:0')] * 2)
        self.assertIn(f'-r {CLAUDE_ID}', calls[0][-1])

    def test_snapshot_is_replaced_whole(self) -> None:
        path = os.path.join(self.tmp.name, 'state', 'last.json')
        sr.write_atomically(path, '{"tabs": []}')
        sr.write_atomically(path, '{"tabs": [], "sessions": {}}')
        self.assertEqual(os.listdir(os.path.dirname(path)), ['last.json'])
        self.assertEqual(sr.read_snapshot(path), Snapshot(()))

    def test_only_what_is_worth_running_again_is_kept_from_a_start_command(self) -> None:
        # Panes this module created are wrapped in zsh -ic; replaying the
        # wrapper would nest one more shell on every restore.
        self.assertEqual(sr.replayable('zsh -ic "cd /repo; exec zsh"'), '')
        self.assertEqual(sr.replayable('zsh -ic "cd /ai && ./listen.py | tee x; exec zsh"'), './listen.py | tee x')
        self.assertEqual(sr.replayable('"./listen.py | tee x"'), './listen.py | tee x')
        # The agent has exited: starting it again would be a new conversation.
        self.assertEqual(sr.replayable("\"cd '/p' && direnv exec . claude -r; zsh\""), '')
        self.assertEqual(sr.replayable('zsh -ic "cd /a && kimi -S x; exec zsh"'), '')

    def test_a_command_line_that_is_not_utf8_does_not_sink_the_record(self) -> None:
        ps = b'  1     0    01:00 /sbin/launchd\n  42     1    00:10 weird \x85name\n'
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, ps.decode('utf-8', 'replace'), '')) as run:
            procs = Processes.now()
        self.assertEqual(run.call_args.kwargs.get('errors'), 'replace')
        self.assertIn(42, procs.cmd)

    def test_every_agent_waits_for_the_network_before_resuming(self) -> None:
        # Codex started before the network is up after a reboot fails its
        # account check and exits.
        for kind in sr.AGENTS:
            self.assertTrue(Conversation(kind, 'x').resume_in('/a').startswith(sr.WAIT_FOR_NETWORK))

    def test_a_conversation_is_remembered_after_its_agent_exits(self) -> None:
        previous = Snapshot(
            (Tab('', (Column(None, None, '/k', ('kimi-code',), pid=30, conversation=Conversation('kimi', KIMI_ID)),)),),
            {'codex-0921': (Pane('/maic', conversation=Conversation('codex', CODEX_ID), pid=10),)})
        live = [sr.LivePane('codex-0921', 10, '/maic', 'host', '', None)]
        tabs = (Tab('', (Column(None, None, '/k', ('zsh',), pid=30),)),)
        with patch.object(sr.Processes, 'now', return_value=Processes([])), patch.object(sr, 'tmux_panes', return_value=live):
            snap = sr.with_conversations(tabs, previous)
        self.assertEqual(snap.sessions['codex-0921'][0].conversation, Conversation('codex', CODEX_ID))
        self.assertEqual(snap.tabs[0].columns[0].conversation, Conversation('kimi', KIMI_ID))

    def test_a_reused_pid_somewhere_else_does_not_inherit_a_conversation(self) -> None:
        # After a reboot pids start over; a different session or directory is a different place.
        previous = Snapshot((), {'codex-0921': (Pane('/maic', conversation=Conversation('codex', CODEX_ID), pid=10),)})
        live = [sr.LivePane('codex-0921', 10, '/elsewhere', 'host', '', None), sr.LivePane('other', 10, '/maic', 'host', '', None)]
        with patch.object(sr.Processes, 'now', return_value=Processes([])), patch.object(sr, 'tmux_panes', return_value=live):
            snap = sr.with_conversations((), previous)
        self.assertIsNone(snap.sessions['codex-0921'][0].conversation)
        self.assertIsNone(snap.sessions['other'][0].conversation)

    def test_a_record_losing_sessions_is_always_kept_and_others_only_now_and_then(self) -> None:
        path = os.path.join(self.tmp.name, 'state', 'last.json')
        history = os.path.join(self.tmp.name, 'state', 'history')
        sr.keep_in_history(path, {'saved_at': 1000.0, 'sessions': {'a': []}}, losing_sessions=False)
        # Just kept one: an ordinary change does not add another...
        sr.keep_in_history(path, {'saved_at': 1060.0, 'sessions': {'a': []}}, losing_sessions=False)
        self.assertEqual(len(os.listdir(history)), 1)
        # ...but sessions going away does.
        sr.keep_in_history(path, {'saved_at': 1120.0, 'sessions': {'a': [], 'b': []}}, losing_sessions=True)
        self.assertEqual(len(os.listdir(history)), 2)
        with patch.object(sr, 'HISTORY_KEEP', 2):
            sr.keep_in_history(path, {'saved_at': 1180.0, 'sessions': {}}, losing_sessions=True)
        self.assertEqual(len(os.listdir(history)), 2)

    def test_journal_stays_bounded(self) -> None:
        path = os.path.join(self.tmp.name, 'state', 'last.json')
        with patch.object(sr, 'JOURNAL_MAX', 200):
            for i in range(40):
                sr.journal(path, 'saves.log', f'line {i}')
        with open(os.path.join(self.tmp.name, 'state', 'saves.log')) as f:
            lines = f.read().splitlines()
        self.assertLess(len(lines), 40)
        self.assertTrue(lines[-1].endswith('line 39'))

    def test_saving_leaves_a_line_saying_what_was_recorded(self) -> None:
        path = os.path.join(self.tmp.name, 'state', 'last.json')
        snap = Snapshot((Tab('', (Column(None, tmux='codex-1'),)),), {'codex-1': (Pane('/a', conversation=Conversation('codex', CODEX_ID)),)})
        with patch.object(sr, 'with_conversations', return_value=snap):
            sr.record(path, snap.tabs)
            sr.record(path, snap.tabs)  # unchanged: no second line
        with open(os.path.join(self.tmp.name, 'state', 'saves.log')) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('tabs=1 columns=1 sessions=1 conversations=1 [codex-1]', lines[0])

    def test_a_session_that_fails_to_come_back_is_an_error_not_silence(self) -> None:
        failed = subprocess.CompletedProcess([], 1, '', 'no server running')
        with patch('subprocess.run', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'no server running'):
                sr.create_session('codex-1', [('/a', 'codex')])

    def test_bringing_back_a_session_says_what_was_done(self) -> None:
        panes = (Pane('/a', conversation=Conversation('codex', CODEX_ID)),)
        with patch.object(sr, 'tmux_alive', return_value=False), patch.object(sr, 'tmux', side_effect=recording([])):
            self.assertEqual(sr.bring_back_session('codex-1', panes, set()), 'recreated, 1 panes, 1 conversations')
        with patch.object(sr, 'tmux_alive', return_value=True), patch.object(sr, 'live_pane_commands', return_value=['zsh']), \
                patch.object(sr, 'tmux', side_effect=recording([])):
            self.assertEqual(sr.bring_back_session('codex-1', panes, set()), 'alive, resumed 1 in a shell')

    def test_restore_goes_on_when_ps_is_too_slow_at_boot(self) -> None:
        timeout = subprocess.TimeoutExpired(['ps'], 30)
        with patch.object(sr.Processes, 'now', side_effect=timeout), \
                patch.object(sr, 'ERROR_LOG', os.path.join(self.tmp.name, 'errors.log')), patch('kitty.utils.log_error'):
            self.assertEqual(sr.conversations_already_open(), set())
        with open(os.path.join(self.tmp.name, 'errors.log')) as f:
            self.assertIn('restoring as if none were', f.read())
