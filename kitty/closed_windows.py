#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Bringing back a window that was closed by accident.

A terminal cannot truly restore a closed window: closing it killed the process.
What can be restored is where it was and what it was running, and for programs
that keep their own state on disk, asking them to pick that state back up gets
close enough to the real thing.
"""

import os
import shlex
import shutil
import subprocess
from typing import NamedTuple, Sequence

from kitty.utils import which

MAX_CLOSED_WINDOWS = 32

SHELL_NAMES = frozenset({'sh', 'bash', 'zsh', 'fish', 'dash', 'ksh', 'tcsh', 'csh', 'nu', 'xonsh', 'elvish'})

AI_SHELL_COMMANDS = ('c', 'x', 'a', 'claude', 'codex', 'agy')


class ClosedWindow(NamedTuple):
    cwd: str
    argv: tuple[str, ...]
    title: str
    last_cmd: str = ''


def extract_tmux_session(argv: Sequence[str]) -> str | None:
    for i, a in enumerate(argv):
        if a in ('-s', '-t') and i + 1 < len(argv):
            return argv[i + 1].lstrip('=')
        if (a.startswith('-s') or a.startswith('-t')) and len(a) > 2:
            return a[2:].lstrip('=')
    return None


def tmux_has_session(session_name: str) -> bool:
    try:
        clean = session_name.lstrip('=')
        res = subprocess.run([tmux_executable(), 'has-session', '-t', f'={clean}'],
                             capture_output=True, timeout=1)
        return res.returncode == 0
    except Exception:
        return False


def tmux_executable() -> str:
    # Finder starts kitty with /usr/bin:/bin; tmux installed by Homebrew is
    # outside that PATH and macOS's /etc/paths.
    return which('tmux') or shutil.which('tmux', path='/opt/homebrew/bin:/usr/local/bin:/opt/local/bin') or 'tmux'


def tmux_relaunch(argv: tuple[str, ...]) -> list[str]:
    """Re-attach to the session rather than trying to create it again."""
    sess = extract_tmux_session(argv)
    if sess:
        if tmux_has_session(sess):
            # Session is alive in tmux -> attach to it safely through interactive zsh
            return ['zsh', '-ic', f'{shlex.quote(tmux_executable())} attach -t {shlex.quote("=" + sess)}; exec zsh']
        # Session is dead -> check if it's an AI agent session
        if sess.startswith('claude-'):
            return ['zsh', '-ic', 'c -r; exec zsh']
        if sess.startswith('agy-'):
            return ['zsh', '-ic', 'a -c; exec zsh']
        if sess.startswith('codex-'):
            return ['zsh', '-ic', 'x resume; exec zsh']

    # General tmux new-session
    out = list(argv)
    out[0] = tmux_executable()
    for verb in ('new-session', 'new'):
        if verb in out:
            i = out.index(verb)
            rest = out[i + 1:]
            if '-A' not in rest and any(a == '-s' or a.startswith('-s') for a in rest):
                out.insert(i + 1, '-A')
            break
    # Run through zsh -ic so shell environment & functions are loaded, and stay in shell on exit
    cmd_str = ' '.join(shlex.quote(a) for a in out)
    return ['zsh', '-ic', f'{cmd_str}; exec zsh']


def relaunch_argv(argv: tuple[str, ...], last_cmd: str = '') -> list[str]:
    """Turn a recorded command line into arguments for launch.

    Returns an empty list to mean "just start the shell", which is what should
    happen when the window was only ever sitting at a prompt.
    """
    if argv:
        exe = os.path.basename(argv[0])
        if exe not in SHELL_NAMES and not exe.startswith('-'):
            if exe == 'tmux':
                return tmux_relaunch(argv)
            if exe == 'claude':
                if not any(a in ('-c', '--continue', '-r', '--resume', '--session-id') for a in argv[1:]):
                    return ['zsh', '-ic', f'{" ".join(shlex.quote(a) for a in argv)} --continue; exec zsh']
                return ['zsh', '-ic', f'{" ".join(shlex.quote(a) for a in argv)}; exec zsh']
            if exe == 'agy':
                if not any(a in ('-c', '--continue', '--conversation') for a in argv[1:]):
                    return ['zsh', '-ic', f'{" ".join(shlex.quote(a) for a in argv)} --continue; exec zsh']
                return ['zsh', '-ic', f'{" ".join(shlex.quote(a) for a in argv)}; exec zsh']
            cmd_str = ' '.join(shlex.quote(a) for a in argv)
            return ['zsh', '-ic', f'{cmd_str}; exec zsh']

    # If argv was a bare shell or empty, check last_cmd
    if last_cmd:
        first_word = last_cmd.strip().split()[0] if last_cmd.strip() else ''
        if first_word in AI_SHELL_COMMANDS:
            return ['zsh', '-ic', f'{last_cmd.strip()}; exec zsh']

    return []
