"""Locate the shared `pyscheme-cppscheme2-common` directory.

Both interpreters keep their shared assets -- `scheme-tests/`, `SRFI/`, and the
`help/` docs+examples tree -- in a single repo named
`pyscheme-cppscheme2-common`, so pyScheme and cppScheme2 single-source the same
files instead of each carrying a copy.  This module finds that root; the rest of
pyScheme resolves the individual shared subdirectories from it.

Resolution order:

  1. ``$SCHEME_COMMON_DIR``, if set and a directory.
  2. A bounded walk-up from this package: at each ancestor directory, look for a
     child named ``pyscheme-cppscheme2-common``.  The first hit wins, which
     yields subdir-before-sibling precedence for free -- a copy nested under the
     interpreter is found before one sitting beside it.

Returns ``None`` when no common directory is found.  Every shared subdirectory
is optional: callers degrade gracefully (no tests, no SRFI, no help) rather than
failing when one is absent.
"""

import os

COMMON_DIR_NAME = 'pyscheme-cppscheme2-common'
_MAX_WALK_UP = 6


def find_common_root():
    """Return the absolute path to the shared common directory, or None."""
    env = os.environ.get('SCHEME_COMMON_DIR')
    if env:
        return env if os.path.isdir(env) else None

    cur = os.path.dirname(os.path.abspath(__file__))   # the pyscheme package dir
    levels = 0
    while levels <= _MAX_WALK_UP:
        candidate = os.path.join(cur, COMMON_DIR_NAME)
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:                              # reached the filesystem root
            break
        cur = parent
        levels = levels + 1
    return None


def common_subdir(name):
    """Return ``<root>/<name>`` if both the common root and that subdir exist,
    else None.  Used to resolve the individual optional shared trees."""
    root = find_common_root()
    if root is None:
        return None
    path = os.path.join(root, name)
    return path if os.path.isdir(path) else None
