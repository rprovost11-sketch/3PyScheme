# readline_win.py
# Windows readline replacement using msvcrt.
# Module-level API mirrors the GNU readline module for drop-in compatibility.
# This module is Windows-only; importing it on other platforms will raise ImportError.

import os
import sys
import msvcrt

# Enable ANSI escape code processing in the Windows console.
os.system('')

# ---------------------------------------------------------------------------
# Key constants
# ---------------------------------------------------------------------------

_KEY_ENTER     = 'ENTER'
_KEY_BACKSPACE = 'BACKSPACE'
_KEY_DELETE    = 'DELETE'
_KEY_LEFT      = 'LEFT'
_KEY_RIGHT     = 'RIGHT'
_KEY_UP        = 'UP'
_KEY_DOWN      = 'DOWN'
_KEY_HOME      = 'HOME'
_KEY_END       = 'END'
_KEY_CTRL_C    = 'CTRL_C'
_KEY_CTRL_R    = 'CTRL_R'
_KEY_EOF       = 'EOF'
_KEY_ESCAPE    = 'ESCAPE'

# Second-byte map for \xe0-prefixed extended key sequences.
_EXTENDED_KEYS = {
   '\x48': _KEY_UP,
   '\x50': _KEY_DOWN,
   '\x4b': _KEY_LEFT,
   '\x4d': _KEY_RIGHT,
   '\x47': _KEY_HOME,
   '\x4f': _KEY_END,
   '\x53': _KEY_DELETE,
}

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_history     = []
_history_max = 500


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _drop_last(s):
   """Return s without its last character.  s must be non-empty."""
   result = ''
   i = 0
   n = len(s) - 1
   while i < n:
      result = result + s[i]
      i = i + 1
   return result


def _first_line(chars):
   """Text up to the first newline in `chars` (a list of single-char strings)."""
   result = ''
   i = 0
   while i < len(chars):
      if chars[i] == '\n':
         break
      result = result + chars[i]
      i = i + 1
   return result


def _spaces(n):
   """Return a string of n spaces (n >= 0)."""
   result = ''
   i = 0
   while i < n:
      result = result + ' '
      i = i + 1
   return result


class _SearchResult:
   """Return container for the reverse-history-search helper.  POD."""
   def __init__(self, idx, buf):
      self.idx = idx
      self.buf = buf


# ---------------------------------------------------------------------------
# Internal key decoder
# ---------------------------------------------------------------------------


def _read_key():
   """Read one logical key from the console.
   Returns a _KEY_* constant or a printable character string.
   Returns None for unrecognised escape sequences."""
   ch = msvcrt.getwch()
   if ch == '\x00' or ch == '\xe0':
      ch2 = msvcrt.getwch()
      return _EXTENDED_KEYS.get(ch2, None)
   if ch == '\r':
      return _KEY_ENTER
   if ch == '\x08':
      return _KEY_BACKSPACE
   if ch == '\x03':
      return _KEY_CTRL_C
   if ch == '\x12':
      return _KEY_CTRL_R
   if ch == '\x1b':
      return _KEY_ESCAPE
   if ch == '\x04' or ch == '\x1a':
      return _KEY_EOF
   return ch


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def input_line(prompt='', continuation_prompt='... ', prefill=''):
   """Read one line of input with full line editing and history navigation.

   Multi-line history entries (containing newlines) are displayed across
   multiple lines using continuation_prompt for lines after the first.
   prefill pre-populates the buffer (used for auto-indent).

   Raises KeyboardInterrupt on Ctrl-C.
   Raises EOFError on Ctrl-D or Ctrl-Z when the buffer is empty.
   """
   buf          = list(prefill)
   cursor       = len(prefill)
   hist_idx     = -1   # -1 = editing current input; 0 = newest history entry
   hist_pending = ''   # saves in-progress line while navigating history
   prev_extra   = 0    # extra display lines drawn in the previous redraw
   pending_key  = None  # key requeued after exiting search mode

   def redraw():
      nonlocal prev_extra
      content = ''.join(buf)
      parts   = content.split('\n')
      extra   = len(parts) - 1

      # Move cursor up to the first line if the previous redraw drew multiple lines.
      if prev_extra > 0:
         sys.stdout.write('\033[%dA' % prev_extra)

      # Draw each line with the appropriate prompt.
      i = 0
      while i < len(parts):
         if i == 0:
            pfx = prompt
         else:
            pfx = continuation_prompt
         if i > 0:
            sys.stdout.write('\n')
         sys.stdout.write('\r' + pfx + parts[i] + '\033[K')
         i = i + 1

      # Clear any lines left over from a previous longer draw.
      leftover = prev_extra - extra
      if leftover > 0:
         k = 0
         while k < leftover:
            sys.stdout.write('\n\r\033[K')
            k = k + 1
         sys.stdout.write('\033[%dA' % leftover)

      # Compute which display line and column the cursor belongs on.
      cursor_line = 0
      cursor_col  = 0
      k = 0
      while k < cursor:
         if content[k] == '\n':
            cursor_line = cursor_line + 1
            cursor_col  = 0
         else:
            cursor_col = cursor_col + 1
         k = k + 1
      if cursor_line == 0:
         cursor_pfx = prompt
      else:
         cursor_pfx = continuation_prompt

      # Move up from the last drawn line to the cursor's line.
      lines_up = extra - cursor_line
      if lines_up > 0:
         sys.stdout.write('\033[%dA' % lines_up)

      # Position the cursor at the correct column.
      target_col = len(cursor_pfx) + cursor_col
      sys.stdout.write('\r')
      if target_col > 0:
         sys.stdout.write('\033[%dC' % target_col)

      sys.stdout.flush()
      prev_extra = extra

   def move_to_end_and_newline():
      """Position cursor at end of last display line then emit a newline."""
      content = ''.join(buf)
      extra = 0
      k = 0
      while k < len(content):
         if content[k] == '\n':
            extra = extra + 1
         k = k + 1
      cursor_line = 0
      k = 0
      while k < cursor:
         if content[k] == '\n':
            cursor_line = cursor_line + 1
         k = k + 1
      lines_down = extra - cursor_line
      if lines_down > 0:
         sys.stdout.write('\033[%dB' % lines_down)
      sys.stdout.write('\n')
      sys.stdout.flush()

   redraw()

   while True:
      if pending_key is not None:
         key = pending_key
      else:
         key = _read_key()
      pending_key = None

      if key is None:
         pass   # unrecognised escape sequence - ignore

      elif key == _KEY_ENTER:
         move_to_end_and_newline()
         return ''.join(buf)

      elif key == _KEY_CTRL_C:
         move_to_end_and_newline()
         raise KeyboardInterrupt

      elif key == _KEY_EOF:
         if not buf:
            move_to_end_and_newline()
            raise EOFError
         # Ctrl-D with content - ignore (matches GNU readline behaviour)

      elif key == _KEY_BACKSPACE:
         if cursor > 0:
            del buf[cursor - 1]
            cursor = cursor - 1
            redraw()

      elif key == _KEY_DELETE:
         if cursor < len(buf):
            del buf[cursor]
            redraw()

      elif key == _KEY_LEFT:
         if cursor > 0:
            cursor = cursor - 1
            redraw()

      elif key == _KEY_RIGHT:
         if cursor < len(buf):
            cursor = cursor + 1
            redraw()

      elif key == _KEY_HOME:
         cursor = 0
         redraw()

      elif key == _KEY_END:
         cursor = len(buf)
         redraw()

      elif key == _KEY_UP:
         if hist_idx == -1:
            hist_pending = ''.join(buf)
         next_idx = hist_idx + 1
         if next_idx < len(_history):
            hist_idx = next_idx
            buf      = list(_history[len(_history) - 1 - hist_idx])
            cursor   = len(buf)
            redraw()

      elif key == _KEY_DOWN:
         if hist_idx > 0:
            hist_idx = hist_idx - 1
            buf      = list(_history[len(_history) - 1 - hist_idx])
            cursor   = len(buf)
            redraw()
         elif hist_idx == 0:
            hist_idx = -1
            buf      = list(hist_pending)
            cursor   = len(buf)
            redraw()

      elif key == _KEY_CTRL_R:
         # Reverse incremental history search
         search_str = ''
         found_idx  = -1
         found_buf  = []
         saved_buf  = buf[:]
         saved_curs = cursor

         def do_search(from_idx=None):
            if from_idx is None:
               start = len(_history) - 1
            else:
               start = from_idx
            i = start
            while i >= 0:
               if search_str in _history[i]:
                  return _SearchResult(i, list(_history[i]))
               i = i - 1
            return _SearchResult(-1, [])

         def search_draw():
            nonlocal prev_extra
            if found_idx >= 0:
               first = _first_line(found_buf)
            else:
               first = ''
            spfx = "(reverse-i-search)'" + search_str + "': "
            if prev_extra > 0:
               sys.stdout.write('\033[%dA' % prev_extra)
               prev_extra = 0
            sys.stdout.write('\r' + spfx + first + '\033[K')
            sys.stdout.flush()

         search_draw()

         while True:
            skey = _read_key()

            if skey == _KEY_CTRL_R:
               if search_str:
                  if found_idx > 0:
                     r = do_search(found_idx - 1)
                     found_idx = r.idx
                     found_buf = r.buf
                  elif found_idx == -1:
                     r = do_search()
                     found_idx = r.idx
                     found_buf = r.buf
               search_draw()

            elif skey == _KEY_BACKSPACE:
               if search_str:
                  search_str = _drop_last(search_str)
                  if search_str:
                     r = do_search()
                     found_idx = r.idx
                     found_buf = r.buf
                  else:
                     found_idx = -1
                     found_buf = []
               search_draw()

            elif skey == _KEY_ENTER:
               if found_idx >= 0:
                  buf = found_buf
               else:
                  buf = saved_buf
               cursor = len(buf)
               prev_extra = 0
               sys.stdout.write('\n')
               sys.stdout.flush()
               return ''.join(buf)

            elif skey == _KEY_CTRL_C:
               buf    = saved_buf
               cursor = saved_curs
               prev_extra = 0
               move_to_end_and_newline()
               raise KeyboardInterrupt

            elif skey == _KEY_ESCAPE or skey is None:
               buf    = saved_buf
               cursor = saved_curs
               prev_extra = 0
               redraw()
               break

            elif isinstance(skey, str) and skey.isprintable():
               search_str = search_str + skey
               r = do_search()
               found_idx = r.idx
               found_buf = r.buf
               search_draw()

            else:
               # Any other key: accept match, requeue key for normal handling
               if found_idx >= 0:
                  buf = found_buf
               else:
                  buf = saved_buf
               cursor     = len(buf)
               prev_extra = 0
               pending_key = skey
               redraw()
               break

      elif isinstance(key, str) and key.isprintable():
         buf.insert(cursor, key)
         cursor = cursor + 1
         redraw()


def add_history(entry):
   """Append entry to history.  Skips empty entries and exact duplicates of
   the most recent."""
   if not entry:
      return
   if _history and _history[len(_history) - 1] == entry:
      return
   _history.append(entry)
   if len(_history) > _history_max:
      _history.pop(0)


def set_history_length(n):
   """Set the maximum number of history entries retained."""
   global _history_max
   _history_max = n


def read_history_file(path):
   """Load history from file.  Silently ignores a missing file.
   Embedded newlines are stored as the two-character sequence \\n and
   decoded on load."""
   try:
      f = open(path, 'r', encoding='utf-8')
   except FileNotFoundError:
      return
   for raw in f:
      entry = raw.rstrip('\n').replace('\\n', '\n')
      if entry:
         _history.append(entry)
   f.close()


def write_history_file(path):
   """Save history to file.  Embedded newlines are encoded as the two-
   character sequence \\n."""
   n = len(_history)
   start = n - _history_max
   if start < 0:
      start = 0
   f = open(path, 'w', encoding='utf-8')
   i = start
   while i < n:
      entry = _history[i]
      f.write(entry.replace('\n', '\\n') + '\n')
      i = i + 1
   f.close()


def get_history():
   """Return a copy of the current history list."""
   return list(_history)


def set_history(entries):
   """Replace the history list with entries."""
   global _history
   _history = list(entries)
