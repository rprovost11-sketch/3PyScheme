"""I/O primitives: display, write, newline.

R7RS 6.13 (Input and output) - core write side only.  Ports, read, and
the rest of the I/O subsystem are still TODO.

display - human-readable: strings without quotes, characters as the bare
  glyph, lists with their elements rendered the same way.
write   - machine-readable: strings with quotes and escapes, characters
  with the #\\ prefix, lists with quoted elements.

Both write to ctx.outStrm (the listener's stdout, by default).  Both
return an unspecified value (#<void>).
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_cons, is_nil, is_string, is_character, is_symbol, is_void,
   as_string, as_character,
   VOID_VALUE,
)


CATEGORY = 'io'


_CHAR_NAMES_REVERSE = {
   ' ':    'space',
   '\n':   'newline',
   '\t':   'tab',
   '\r':   'return',
   '\0':   'null',
   '\a':   'alarm',
   '\b':   'backspace',
   '\x7f': 'delete',
   '\x1b': 'escape',
}


def _render_display(val):
   """display: strings without quotes, characters as bare glyph."""
   if is_string(val):
      return as_string(val)
   if is_character(val):
      return as_character(val)
   if is_void(val):
      return ''
   from pyscheme.PrettyPrinter import pretty_print
   return _walk_display(val, pretty_print)


def _walk_display(val, pretty_print):
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
   text = _render_display(args[0])
   ctx.outStrm.write(text)
   return VOID_VALUE


def _prim_write(ctx, env, args, app_node):
   from pyscheme.PrettyPrinter import pretty_print
   ctx.outStrm.write(pretty_print(args[0]))
   return VOID_VALUE


def _prim_newline(ctx, env, args, app_node):
   ctx.outStrm.write('\n')
   return VOID_VALUE


def _prim_write_char(ctx, env, args, app_node):
   from pyscheme.Environment import SchemeTypeError
   from pyscheme.AST          import src_of
   v = args[0]
   if not is_character(v):
      raise SchemeTypeError(
         'write-char: argument must be a character', src_of(app_node))
   ctx.outStrm.write(as_character(v))
   return VOID_VALUE


def _prim_write_string(ctx, env, args, app_node):
   from pyscheme.Environment import SchemeTypeError
   from pyscheme.AST          import src_of
   v = args[0]
   if not is_string(v):
      raise SchemeTypeError(
         'write-string: argument must be a string', src_of(app_node))
   ctx.outStrm.write(as_string(v))
   return VOID_VALUE


def register():
   register_primitive('display', (1, 1), _prim_display,
      doc=('(display obj) writes a human-readable representation of obj '
           'to the current output port.  Strings appear without quotes; '
           'characters as bare glyphs.  Returns an unspecified value.  '
           'R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('write', (1, 1), _prim_write,
      doc=('(write obj) writes a machine-readable representation of obj '
           'to the current output port.  Strings have surrounding quotes '
           'with escapes; characters use the #\\ prefix.  R7RS 6.13.'),
      category=CATEGORY)
   register_primitive('newline', (0, 0), _prim_newline,
      doc='Write a newline character to the current output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('write-char', (1, 1), _prim_write_char,
      doc='Write a single character to the current output port.  R7RS 6.13.',
      category=CATEGORY)
   register_primitive('write-string', (1, 1), _prim_write_string,
      doc='Write a string (without quotes) to the current output port.  R7RS 6.13.',
      category=CATEGORY)
