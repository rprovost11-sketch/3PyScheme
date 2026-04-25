"""Port primitives.

R7RS 6.13 - input and output.  Covers the practical subset most user
code needs: file and string ports, char-level read / peek, eof-object,
read (one S-expression), write-char / write-string, plus optional port
arguments on display / write / newline.

Port semantics:
   open-input-file  - opens a file, slurps its contents into the port's
                      buffer (so read/read-char operate on a snapshot).
                      The file handle is held only for the close call.
   open-input-string - wraps a string as an input port.
   open-output-file - opens a file for writing; output goes to the file
                      handle directly.
   open-output-string - accumulates chunks; get-output-string returns
                        the joined result.

Streaming-from-stdin / interactive line-by-line reads are not supported
by this first cut: open-input-file reads the whole file up front.

current-input-port and current-output-port are R7RS parameters; the
Listener installs sys.stdin / sys.stdout as their initial values.
"""

import io as _pyio

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   alloc_cons, NIL_VALUE, VOID_VALUE,
   is_cons, is_string, is_character, is_integer, is_port, is_eof,
   as_string, as_character, as_integer, as_port,
   make_port, make_eof, make_string, make_character, make_integer,
   make_boolean, make_parameter,
   Port,
   src_of,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'ports'


# Module-level handles to the current input / output / error parameters.
# Initialised lazily on first use; the Listener may replace these via
# (parameterize ...) for capture during testing.
_current_input_param  = [None]
_current_output_param = [None]
_current_error_param  = [None]


def _stdin_port():
   import sys
   p = Port('', is_input=True, is_text=True, file_h=sys.stdin, name='<stdin>')
   return make_port(p)


def _stdout_port():
   import sys
   p = Port([], is_input=False, is_text=True, file_h=sys.stdout, name='<stdout>')
   return make_port(p)


def _stderr_port():
   import sys
   p = Port([], is_input=False, is_text=True, file_h=sys.stderr, name='<stderr>')
   return make_port(p)


def _get_current_input(ctx):
   if _current_input_param[0] is None:
      _current_input_param[0] = make_parameter(_stdin_port(), None)
   from pyscheme.AST import as_parameter_value
   return as_parameter_value(_current_input_param[0])


def _get_current_output(ctx):
   if _current_output_param[0] is None:
      _current_output_param[0] = make_parameter(_stdout_port(), None)
   from pyscheme.AST import as_parameter_value
   return as_parameter_value(_current_output_param[0])


def _check_port(v, name, app_node, idx=1):
   if not is_port(v):
      raise SchemeTypeError(
         '%s: argument %d must be a port' % (name, idx), src_of(app_node))
   p = as_port(v)
   if not p.is_open:
      raise SchemeTypeError(
         '%s: port is closed' % name, src_of(app_node))
   return p


def _check_input_port(v, name, app_node, idx=1):
   p = _check_port(v, name, app_node, idx)
   if not p.is_input:
      raise SchemeTypeError(
         '%s: argument %d must be an input port' % (name, idx),
         src_of(app_node))
   return p


def _check_output_port(v, name, app_node, idx=1):
   p = _check_port(v, name, app_node, idx)
   if p.is_input:
      raise SchemeTypeError(
         '%s: argument %d must be an output port' % (name, idx),
         src_of(app_node))
   return p


# ── Predicates ─────────────────────────────────────────────────────────────

def _prim_port_p(ctx, env, args, app_node):
   return make_boolean(is_port(args[0]))


def _prim_input_port_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_port(v) and as_port(v).is_input)


def _prim_output_port_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_port(v) and not as_port(v).is_input)


def _prim_textual_port_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_port(v) and as_port(v).is_text)


def _prim_binary_port_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_port(v) and not as_port(v).is_text)


def _prim_input_port_open_p(ctx, env, args, app_node):
   v = args[0]
   if not is_port(v):
      return make_boolean(False)
   p = as_port(v)
   return make_boolean(p.is_input and p.is_open)


def _prim_output_port_open_p(ctx, env, args, app_node):
   v = args[0]
   if not is_port(v):
      return make_boolean(False)
   p = as_port(v)
   return make_boolean((not p.is_input) and p.is_open)


def _prim_eof_object_p(ctx, env, args, app_node):
   return make_boolean(is_eof(args[0]))


def _prim_eof_object(ctx, env, args, app_node):
   return make_eof()


# ── Constructors / closers ────────────────────────────────────────────────

def _prim_open_input_file(ctx, env, args, app_node):
   if not is_string(args[0]):
      raise SchemeTypeError(
         'open-input-file: filename must be a string', src_of(app_node))
   path = as_string(args[0])
   try:
      f = open(path, 'r', encoding='utf-8')
   except OSError as e:
      raise SchemeTypeError(
         'open-input-file: cannot open %s: %s' % (path, str(e)),
         src_of(app_node))
   buf = f.read()
   p = Port(buf, is_input=True, is_text=True, file_h=f, name=path)
   return make_port(p)


def _prim_open_output_file(ctx, env, args, app_node):
   if not is_string(args[0]):
      raise SchemeTypeError(
         'open-output-file: filename must be a string', src_of(app_node))
   path = as_string(args[0])
   try:
      f = open(path, 'w', encoding='utf-8')
   except OSError as e:
      raise SchemeTypeError(
         'open-output-file: cannot open %s: %s' % (path, str(e)),
         src_of(app_node))
   p = Port([], is_input=False, is_text=True, file_h=f, name=path)
   return make_port(p)


def _prim_open_input_string(ctx, env, args, app_node):
   if not is_string(args[0]):
      raise SchemeTypeError(
         'open-input-string: argument must be a string', src_of(app_node))
   p = Port(as_string(args[0]), is_input=True, is_text=True,
            name='<input-string>')
   return make_port(p)


def _prim_open_output_string(ctx, env, args, app_node):
   p = Port([], is_input=False, is_text=True, name='<output-string>')
   return make_port(p)


def _prim_get_output_string(ctx, env, args, app_node):
   p = _check_output_port(args[0], 'get-output-string', app_node)
   if p.file_h is not None:
      raise SchemeTypeError(
         'get-output-string: port is not a string output port',
         src_of(app_node))
   return make_string(''.join(p.buf))


def _prim_close_port(ctx, env, args, app_node):
   if not is_port(args[0]):
      raise SchemeTypeError(
         'close-port: argument must be a port', src_of(app_node))
   p = as_port(args[0])
   if p.is_open and p.file_h is not None:
      try:
         p.file_h.close()
      except OSError:
         pass
   p.is_open = False
   return VOID_VALUE


def _prim_close_input_port(ctx, env, args, app_node):
   _check_input_port(args[0], 'close-input-port', app_node)
   return _prim_close_port(ctx, env, args, app_node)


def _prim_close_output_port(ctx, env, args, app_node):
   _check_output_port(args[0], 'close-output-port', app_node)
   return _prim_close_port(ctx, env, args, app_node)


# ── Read primitives ───────────────────────────────────────────────────────

def _read_one_char(p):
   """Return the next char from a textual input port, or None at eof."""
   if p.pos >= len(p.buf):
      return None
   c = p.buf[p.pos]
   p.pos = p.pos + 1
   return c


def _peek_one_char(p):
   if p.pos >= len(p.buf):
      return None
   return p.buf[p.pos]


def _prim_read_char(ctx, env, args, app_node):
   if len(args) == 0:
      port_val = _get_current_input(ctx)
   else:
      port_val = args[0]
   p = _check_input_port(port_val, 'read-char', app_node)
   c = _read_one_char(p)
   if c is None:
      return make_eof()
   return make_character(c)


def _prim_peek_char(ctx, env, args, app_node):
   if len(args) == 0:
      port_val = _get_current_input(ctx)
   else:
      port_val = args[0]
   p = _check_input_port(port_val, 'peek-char', app_node)
   c = _peek_one_char(p)
   if c is None:
      return make_eof()
   return make_character(c)


def _prim_read(ctx, env, args, app_node):
   from pyscheme.Parser import Parser, tokenize, TOK_EOF, SchemeSyntaxError
   if len(args) == 0:
      port_val = _get_current_input(ctx)
   else:
      port_val = args[0]
   p = _check_input_port(port_val, 'read', app_node)
   remaining = p.buf[p.pos:]
   if not remaining.strip():
      return make_eof()
   try:
      tokens = tokenize(remaining, p.name)
   except SchemeSyntaxError:
      raise
   parser = Parser(tokens)
   if parser._peek().kind == TOK_EOF:
      return make_eof()
   form = parser.parse_expr()
   # Advance the port past the consumed text.  Tokens carry source
   # positions but not byte offsets; the simplest reliable advance is
   # to compute how many characters were consumed by re-tokenizing
   # everything before the next unconsumed token.
   nxt = parser._peek()
   if nxt.kind == TOK_EOF:
      p.pos = len(p.buf)
   elif nxt.src is not None:
      # Translate (line, col) to a character offset within `remaining`.
      line = nxt.src.line
      col  = nxt.src.col
      lines_seen = 0
      i = 0
      while lines_seen < line - 1 and i < len(remaining):
         if remaining[i] == '\n':
            lines_seen = lines_seen + 1
         i = i + 1
      i = i + col - 1
      p.pos = p.pos + i
   else:
      # Fallback: skip to end so the port reports eof on next read.
      p.pos = len(p.buf)
   return form


def _prim_read_line(ctx, env, args, app_node):
   if len(args) == 0:
      port_val = _get_current_input(ctx)
   else:
      port_val = args[0]
   p = _check_input_port(port_val, 'read-line', app_node)
   if p.pos >= len(p.buf):
      return make_eof()
   end = p.buf.find('\n', p.pos)
   if end < 0:
      line = p.buf[p.pos:]
      p.pos = len(p.buf)
   else:
      line = p.buf[p.pos:end]
      p.pos = end + 1
   return make_string(line)


def _prim_read_string(ctx, env, args, app_node):
   if not is_integer(args[0]):
      raise SchemeTypeError(
         'read-string: count must be an integer', src_of(app_node))
   k = as_integer(args[0])
   if k < 0:
      raise SchemeTypeError(
         'read-string: count must be non-negative', src_of(app_node))
   if len(args) == 1:
      port_val = _get_current_input(ctx)
   else:
      port_val = args[1]
   p = _check_input_port(port_val, 'read-string', app_node)
   if p.pos >= len(p.buf) and k > 0:
      return make_eof()
   end = min(p.pos + k, len(p.buf))
   s   = p.buf[p.pos:end]
   p.pos = end
   return make_string(s)


# ── Write primitives ──────────────────────────────────────────────────────

def _emit_to_port(p, text):
   """Write text to an output port.  Files write through their file_h
   directly; string ports accumulate chunks for later get-output-string."""
   if p.file_h is not None:
      p.file_h.write(text)
   else:
      p.buf.append(text)


def _resolve_output_port(ctx, args, idx_default_offset):
   """If args has more elements, treat args[idx_default_offset] as a
   port; otherwise return the current output port."""
   if len(args) > idx_default_offset:
      return args[idx_default_offset]
   return _get_current_output(ctx)


def _prim_write_char(ctx, env, args, app_node):
   c = args[0]
   if not is_character(c):
      raise SchemeTypeError(
         'write-char: first argument must be a character', src_of(app_node))
   port_val = _resolve_output_port(ctx, args, 1)
   p = _check_output_port(port_val, 'write-char', app_node, 2)
   _emit_to_port(p, as_character(c))
   return VOID_VALUE


def _prim_write_string(ctx, env, args, app_node):
   s = args[0]
   if not is_string(s):
      raise SchemeTypeError(
         'write-string: first argument must be a string', src_of(app_node))
   port_val = _resolve_output_port(ctx, args, 1)
   p = _check_output_port(port_val, 'write-string', app_node, 2)
   _emit_to_port(p, as_string(s))
   return VOID_VALUE


def _prim_newline(ctx, env, args, app_node):
   port_val = _resolve_output_port(ctx, args, 0)
   p = _check_output_port(port_val, 'newline', app_node, 1)
   _emit_to_port(p, '\n')
   return VOID_VALUE


def _render_display(val):
   """display: strings without quotes, characters as bare glyph,
   lists rendered with display semantics for elements."""
   from pyscheme.AST import (
      is_string, is_character, is_void, is_cons, is_nil,
      as_string, as_character,
   )
   from pyscheme.PrettyPrinter import pretty_print
   if is_string(val):
      return as_string(val)
   if is_character(val):
      return as_character(val)
   if is_void(val):
      return ''
   if is_cons(val):
      items = []
      cur = val
      while is_cons(cur):
         items.append(_render_display(cur.car))
         cur = cur.cdr
      if is_nil(cur):
         return '(' + ' '.join(items) + ')'
      return '(' + ' '.join(items) + ' . ' + _render_display(cur) + ')'
   return pretty_print(val)


def _prim_display(ctx, env, args, app_node):
   v = args[0]
   port_val = _resolve_output_port(ctx, args, 1)
   p = _check_output_port(port_val, 'display', app_node, 2)
   _emit_to_port(p, _render_display(v))
   return VOID_VALUE


def _prim_write(ctx, env, args, app_node):
   from pyscheme.PrettyPrinter import pretty_print
   v = args[0]
   port_val = _resolve_output_port(ctx, args, 1)
   p = _check_output_port(port_val, 'write', app_node, 2)
   _emit_to_port(p, pretty_print(v))
   return VOID_VALUE


def _prim_flush_output_port(ctx, env, args, app_node):
   port_val = _resolve_output_port(ctx, args, 0)
   p = _check_output_port(port_val, 'flush-output-port', app_node, 1)
   if p.file_h is not None:
      try:
         p.file_h.flush()
      except OSError:
         pass
   return VOID_VALUE


# ── current-input-port / current-output-port parameters ────────────────────

def _prim_current_input_port(ctx, env, args, app_node):
   return _get_current_input(ctx)


def _prim_current_output_port(ctx, env, args, app_node):
   return _get_current_output(ctx)


def _prim_current_error_port(ctx, env, args, app_node):
   if _current_error_param[0] is None:
      _current_error_param[0] = make_parameter(_stderr_port(), None)
   from pyscheme.AST import as_parameter_value
   return as_parameter_value(_current_error_param[0])


def register():
   # Predicates
   register_primitive('port?', (1, 1), _prim_port_p,
      doc='Return #t if obj is a port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('input-port?', (1, 1), _prim_input_port_p,
      doc='Return #t if obj is an input port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('output-port?', (1, 1), _prim_output_port_p,
      doc='Return #t if obj is an output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('textual-port?', (1, 1), _prim_textual_port_p,
      doc='Return #t if obj is a textual port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('binary-port?', (1, 1), _prim_binary_port_p,
      doc='Return #t if obj is a binary port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('input-port-open?', (1, 1), _prim_input_port_open_p,
      doc='Return #t if obj is an open input port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('output-port-open?', (1, 1), _prim_output_port_open_p,
      doc='Return #t if obj is an open output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('eof-object?', (1, 1), _prim_eof_object_p,
      doc='Return #t if obj is the eof object.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('eof-object', (0, 0), _prim_eof_object,
      doc='Return the eof object.  R7RS 6.13.',
      category=CATEGORY)
   # Constructors / closers
   register_primitive('open-input-file', (1, 1), _prim_open_input_file,
      doc=('(open-input-file path) opens the file and slurps its contents '
           'into the port\'s buffer.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('open-output-file', (1, 1), _prim_open_output_file,
      doc='(open-output-file path) opens a file for writing.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('open-input-string', (1, 1), _prim_open_input_string,
      doc='(open-input-string string) returns an input port reading from string.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('open-output-string', (0, 0), _prim_open_output_string,
      doc='Return a fresh output string port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('get-output-string', (1, 1), _prim_get_output_string,
      doc=('(get-output-string port) returns the accumulated string from a '
           'string output port.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('close-port', (1, 1), _prim_close_port,
      doc='Close any port (input or output).  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('close-input-port', (1, 1), _prim_close_input_port,
      doc='Close an input port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('close-output-port', (1, 1), _prim_close_output_port,
      doc='Close an output port.  R7RS 6.13.',
      category=CATEGORY)
   # Read primitives
   register_primitive('read-char', (0, 1), _prim_read_char,
      doc=('(read-char [port]) reads one character.  Returns the eof object '
           'at end of file.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('peek-char', (0, 1), _prim_peek_char,
      doc=('(peek-char [port]) returns the next character without consuming '
           'it.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('read', (0, 1), _prim_read,
      doc=('(read [port]) reads and returns one S-expression from the port.  '
           'Returns the eof object at end of file.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('read-line', (0, 1), _prim_read_line,
      doc=('(read-line [port]) reads and returns characters up to (but not '
           'including) the next newline; returns the eof object at end '
           'of file.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('read-string', (1, 2), _prim_read_string,
      doc=('(read-string k [port]) reads up to k characters and returns '
           'them as a string.  R7RS 6.13.'),
      category=CATEGORY)
   # Write primitives (override the textless versions in primitives/io.py)
   register_primitive('write-char', (1, 2), _prim_write_char,
      doc='(write-char char [port]) writes char to the output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('write-string', (1, 2), _prim_write_string,
      doc='(write-string string [port]) writes string to the output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('newline', (0, 1), _prim_newline,
      doc='(newline [port]) writes a newline to the output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('display', (1, 2), _prim_display,
      doc=('(display obj [port]) writes a human-readable representation '
           'of obj to the output port.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('write', (1, 2), _prim_write,
      doc=('(write obj [port]) writes a machine-readable representation '
           'of obj to the output port.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('flush-output-port', (0, 1), _prim_flush_output_port,
      doc='(flush-output-port [port]) flushes any buffered output.  R7RS 6.13.',
      category=CATEGORY)
   # Current-port accessors
   register_primitive('current-input-port', (0, 0), _prim_current_input_port,
      doc='Return the current input port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('current-output-port', (0, 0), _prim_current_output_port,
      doc='Return the current output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('current-error-port', (0, 0), _prim_current_error_port,
      doc='Return the current error port.  R7RS 6.13.',
      category=CATEGORY)
