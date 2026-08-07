#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

"""Resolve a piece of selected text to a file and build a command to preview it.

Selecting a path in build output, a stack trace or an `ls` listing and being
shown the thing itself is the common case; everything here is in service of
being quiet when the selection is *not* a path, since a preview that pops up on
every selection would be unusable.
"""

import os
import shlex
from typing import NamedTuple

IMAGE_SUFFIXES = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff', '.tif', '.ico', '.svg', '.avif', '.heic',
})

# A selection longer than this is prose, not a path.
MAX_PATH_LEN = 512


class Preview(NamedTuple):
    path: str
    is_image: bool


def resolve(text: str, cwd: str | None = None) -> Preview | None:
    """Return the file the text refers to, or None if it does not refer to one."""
    text = text.strip()
    if not text or len(text) > MAX_PATH_LEN or '\n' in text or '\x00' in text:
        return None

    candidates = [text]
    # Paths are routinely quoted, and routinely reported with a line number
    # appended by compilers, linters and grep.
    if len(text) > 1 and text[0] == text[-1] and text[0] in '"\'`':
        candidates.append(text[1:-1])
    stripped = text.rstrip(':')
    for cand in tuple(candidates):
        # foo.py:12 and foo.py:12:5
        head = cand
        for _ in range(2):
            base, sep, tail = head.rpartition(':')
            if not sep or not tail.isdigit():
                break
            head = base
        if head != cand:
            candidates.append(head)
    if stripped != text:
        candidates.append(stripped)

    for cand in candidates:
        if not cand:
            continue
        p = os.path.expanduser(cand)
        if not os.path.isabs(p) and cwd:
            p = os.path.join(cwd, p)
        try:
            if os.path.isfile(p):
                return Preview(os.path.realpath(p), os.path.splitext(p)[1].lower() in IMAGE_SUFFIXES)
        except OSError:
            continue
    return None


def command_for(preview: Preview, kitten: str) -> list[str]:
    """A shell command that shows the file and stays up until dismissed."""
    if preview.is_image:
        script = (
            f'{shlex.quote(kitten)} icat --align=left -- "$1" || exit 1; '
            'printf "\\n\\033[2m%s\\033[0m\\n" "$1"; '
            'printf "\\033[2mpress any key\\033[0m"; '
            # cbreak so a single key dismisses it rather than needing Enter
            'stty -icanon -echo 2>/dev/null; dd bs=1 count=1 >/dev/null 2>&1; stty icanon echo 2>/dev/null'
        )
    else:
        # A pager gives scrolling and q-to-quit for free.
        script = 'exec "${PAGER:-less}" -R -- "$1"'
    return ['sh', '-c', script, 'preview', preview.path]
