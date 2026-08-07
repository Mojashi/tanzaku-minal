#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Bringing back a window that was closed by accident.

A terminal cannot truly restore a closed window: closing it killed the process.
What can be restored is where it was and what it was running, and for programs
that keep their own state on disk, asking them to pick that state back up gets
close enough to the real thing.
"""

import os
from typing import NamedTuple

MAX_CLOSED_WINDOWS = 32

SHELL_NAMES = frozenset({'sh', 'bash', 'zsh', 'fish', 'dash', 'ksh', 'tcsh', 'csh', 'nu', 'xonsh', 'elvish'})

#: Programs that can pick up where they left off, and the argument that asks them to.
#: Only consulted when the recorded command line does not already say something
#: about which session to use.
RESUMABLE: dict[str, tuple[str, tuple[str, ...]]] = {
    # claude --continue resumes the most recent conversation in the cwd, which is
    # what "reopen the thing I just closed" means for it.
    'claude': ('--continue', ('-c', '--continue', '-r', '--resume', '--session-id')),
}


class ClosedWindow(NamedTuple):
    cwd: str
    argv: tuple[str, ...]
    title: str


def relaunch_argv(argv: tuple[str, ...]) -> list[str]:
    """Turn a recorded command line into arguments for launch.

    Returns an empty list to mean "just start the shell", which is what should
    happen when the window was only ever sitting at a prompt.
    """
    if not argv:
        return []
    exe = os.path.basename(argv[0])
    if exe in SHELL_NAMES or exe.startswith('-'):  # login shells come through as -zsh
        return []
    resume = RESUMABLE.get(exe)
    if resume is not None:
        flag, already = resume
        if not any(a in already or a.startswith(f'{already[-1]}=') for a in argv[1:]):
            return [*argv, flag]
    return list(argv)
