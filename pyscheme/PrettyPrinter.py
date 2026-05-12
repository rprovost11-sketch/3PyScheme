"""Pretty printer for CEK values and quoted data.

Converts values produced by the CEK evaluator into human-readable
Scheme surface syntax.  Mirrors the lexer's literal forms so that the
printed output can be re-read by the parser where applicable.

Atoms are tagged tuples; pairs are ConsCell instances.

    (INTEGER, 42, src)              ->  "42"
    (REAL, 3.14, src)               ->  "3.14"
    (RATIONAL, 3, 4, src)           ->  "3/4"
    (BOOLEAN, True, src)            ->  "#t"
    (CHARACTER, 'a', src)           ->  "#\\a"
    (STRING, "hi", src)             ->  '"hi"'
    (CLOSURE, ...)                  ->  "#<procedure>"
    (PRIMITIVE, name, fn)           ->  "#<primitive name>"
    (SYMBOL, "foo", src)            ->  "foo"
    (NIL,)                          ->  "()"
    ConsCell(car, cdr, src)         ->  "(a b c)" or "(a . b)"
"""
import math as _math

from pyscheme.AST import (
   is_cons, is_nil, is_void, is_boolean, is_integer, is_real, is_rational,
   is_complex, is_exact_complex, is_character, is_string, is_symbol,
   is_closure, is_primitive, is_case_closure, is_promise, is_multi_values,
   is_record, is_parameter, is_error_object, is_continuation,
   is_syntax_transformer, is_environment,
   is_record_accessor, is_record_mutator, is_vector, is_bytevector,
   is_port, is_eof,
   as_boolean, as_integer, as_real, as_character, as_string, as_symbol,
   as_rational_num, as_rational_den, as_complex_real, as_complex_imag,
   as_exact_complex_real, as_exact_complex_imag,
   as_primitive_name, as_promise_is_done, as_multi_values_list,
   as_record_type, as_record_fields, as_record_type_name,
   as_error_object_message, as_error_object_irritants,
   as_syntax_transformer_name, as_vector_items, as_bytevector_items,
   make_string,
   INTEGER, REAL, RATIONAL, BOOLEAN, CHARACTER, STRING, CLOSURE, SYMBOL,
   NIL_VALUE,
)


def _format_float(f):
   """Render a Python float as R7RS surface syntax."""
   if _math.isnan(f):
      return '+nan.0'
   if f == float('inf'):
      return '+inf.0'
   if f == float('-inf'):
      return '-inf.0'
   s = repr(f)
   if '.' not in s and 'e' not in s:
      s = s + '.0'
   return s


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


def pretty_print(val):
   """Render a CEK value or quoted datum as Scheme surface syntax."""
   if is_cons(val):
      return _print_list(val)
   if is_nil(val):
      return '()'
   if is_void(val):
      return '#<void>'
   if is_integer(val):
      return str(as_integer(val))
   if is_real(val):
      return _format_float(as_real(val))
   if is_rational(val):
      return str(as_rational_num(val)) + '/' + str(as_rational_den(val))
   if is_complex(val):
      re = as_complex_real(val)
      im = as_complex_imag(val)
      re_s = _format_float(re)
      im_s = _format_float(im)
      if _math.isnan(im) or im >= 0:
         return re_s + '+' + im_s + 'i'
      return re_s + im_s + 'i'
   if is_exact_complex(val):
      re_s = pretty_print(as_exact_complex_real(val))
      im_v = as_exact_complex_imag(val)
      im_s = pretty_print(im_v)
      im_py = as_integer(im_v) if is_integer(im_v) else as_rational_num(im_v)
      if im_py >= 0:
         return re_s + '+' + im_s + 'i'
      return re_s + im_s + 'i'
   if is_boolean(val):
      if as_boolean(val):
         return '#t'
      return '#f'
   if is_character(val):
      c = as_character(val)
      if c in _CHAR_NAMES_REVERSE:
         return '#\\' + _CHAR_NAMES_REVERSE[c]
      return '#\\' + c
   if is_string(val):
      return '"' + _escape_string(as_string(val)) + '"'
   if is_closure(val):
      return '#<procedure>'
   if is_case_closure(val):
      return '#<procedure>'
   if is_record_accessor(val) or is_record_mutator(val):
      return '#<procedure>'
   if is_primitive(val):
      return '#<primitive ' + as_primitive_name(val) + '>'
   if is_promise(val):
      if as_promise_is_done(val):
         return '#<promise forced>'
      return '#<promise>'
   if is_multi_values(val):
      vs = as_multi_values_list(val)
      if len(vs) == 0:
         return '#<values>'
      parts = []
      i = 0
      while i < len(vs):
         parts.append(pretty_print(vs[i]))
         i = i + 1
      return '#<values ' + ' '.join(parts) + '>'
   if is_record(val):
      rt = as_record_type(val)
      fs = as_record_fields(val)
      tname = as_record_type_name(rt)
      if len(fs) == 0:
         return '#<' + tname + '>'
      parts = []
      i = 0
      while i < len(fs):
         parts.append(pretty_print(fs[i]))
         i = i + 1
      return '#<' + tname + ' ' + ' '.join(parts) + '>'
   if is_parameter(val):
      return '#<parameter>'
   if is_continuation(val):
      return '#<continuation>'
   if is_environment(val):
      return '#<environment>'
   if is_vector(val):
      items = as_vector_items(val)
      parts = []
      i = 0
      while i < len(items):
         parts.append(pretty_print(items[i]))
         i = i + 1
      return '#(' + ' '.join(parts) + ')'
   if is_bytevector(val):
      items = as_bytevector_items(val)
      parts = []
      i = 0
      while i < len(items):
         parts.append(str(items[i]))
         i = i + 1
      return '#u8(' + ' '.join(parts) + ')'
   if is_port(val):
      return '#<port>'
   if is_eof(val):
      return '#<eof>'
   if is_syntax_transformer(val):
      return '#<syntax-rules ' + as_syntax_transformer_name(val) + '>'
   if is_error_object(val):
      msg = as_error_object_message(val)
      irs = as_error_object_irritants(val)
      if len(irs) == 0:
         return '#<error-object "' + _escape_string(msg) + '">'
      parts = []
      i = 0
      while i < len(irs):
         parts.append(pretty_print(irs[i]))
         i = i + 1
      return ('#<error-object "' + _escape_string(msg) + '" (' +
              ' '.join(parts) + ')>')
   if is_symbol(val):
      name = _display_symbol_name(as_symbol(val))
      if _needs_vertical_bars(name):
         return '|' + _escape_symbol_name(name) + '|'
      return name
   return repr(val)


def _print_list(pair):
   """Render a (possibly improper) ConsCell chain as '(a b c)' or '(a b . c)'."""
   items = []
   cur = pair
   while is_cons(cur):
      items.append(pretty_print(cur.car))
      cur = cur.cdr
   if is_nil(cur):
      return '(' + ' '.join(items) + ')'
   return '(' + ' '.join(items) + ' . ' + pretty_print(cur) + ')'


def _shared_scan(val, counts):
   """First pass for write-shared: count how many times each mutable
   object (cons cell, vector) is reachable.  counts maps id -> [val, n]."""
   if is_cons(val):
      k = id(val)
      if k in counts:
         counts[k][1] = counts[k][1] + 1
         return
      counts[k] = [val, 1]
      _shared_scan(val.car, counts)
      _shared_scan(val.cdr, counts)
      return
   if is_vector(val):
      k = id(val)
      if k in counts:
         counts[k][1] = counts[k][1] + 1
         return
      counts[k] = [val, 1]
      items = as_vector_items(val)
      i = 0
      while i < len(items):
         _shared_scan(items[i], counts)
         i = i + 1


def _shared_render(val, labels, next_label, seen):
   """Render val with datum labels for any object that appears more than
   once in the structure.  labels: id -> int; next_label: [int] (mutable
   counter); seen: set of ids already printed (emitted with #n=)."""
   if is_cons(val):
      k = id(val)
      if k in labels:
         n = labels[k]
         if k in seen:
            return '#' + str(n) + '#'
         seen.add(k)
         prefix = '#' + str(n) + '='
      else:
         prefix = ''
      items = []
      cur = val
      tail = NIL_VALUE
      while is_cons(cur):
         ck = id(cur)
         if cur is not val and ck in labels:
            tail = cur
            break
         items.append(_shared_render(cur.car, labels, next_label, seen))
         cur = cur.cdr
         if is_cons(cur) and id(cur) in seen:
            tail = cur
            break
      if not is_nil(tail):
         body = '(' + ' '.join(items) + ' . ' + _shared_render(tail, labels, next_label, seen) + ')'
      elif is_nil(cur):
         body = '(' + ' '.join(items) + ')'
      else:
         body = '(' + ' '.join(items) + ' . ' + _shared_render(cur, labels, next_label, seen) + ')'
      return prefix + body
   if is_vector(val):
      k = id(val)
      if k in labels:
         n = labels[k]
         if k in seen:
            return '#' + str(n) + '#'
         seen.add(k)
         prefix = '#' + str(n) + '='
      else:
         prefix = ''
      items_list = as_vector_items(val)
      parts = []
      i = 0
      while i < len(items_list):
         parts.append(_shared_render(items_list[i], labels, next_label, seen))
         i = i + 1
      return prefix + '#(' + ' '.join(parts) + ')'
   return pretty_print(val)


def pretty_print_shared(val):
   """Like pretty_print but uses #n=/#n# datum labels for shared structure.
   This is the rendering used by write-shared (R7RS §6.13)."""
   counts = {}
   _shared_scan(val, counts)
   labels = {}
   next_label = [0]
   k = id(val)
   for obj_id in counts:
      if counts[obj_id][1] > 1:
         labels[obj_id] = next_label[0]
         next_label[0] = next_label[0] + 1
   seen = set()
   return _shared_render(val, labels, next_label, seen)


_GENSYM_PFX = '\x01h.'


def _display_symbol_name(name):
   """Strip hygiene gensym prefix for display.  Format: \x01h.BASE.DIGITS -> BASE."""
   if not name.startswith(_GENSYM_PFX):
      return name
   rest = name[len(_GENSYM_PFX):]
   dot = rest.rfind('.')
   if dot >= 0 and rest[dot + 1:].isdigit():
      return rest[:dot]
   return rest


def _is_safe_symbol_initial(c):
   return (('a' <= c <= 'z') or ('A' <= c <= 'Z') or
           c in '!$%&*/:<=>?^_~')


def _is_safe_symbol_subsequent(c):
   return _is_safe_symbol_initial(c) or ('0' <= c <= '9') or c in '+-@.'


def _needs_vertical_bars(name):
   if not name:
      return True
   if not _is_safe_symbol_initial(name[0]) and name[0] not in '+-@.':
      return True
   i = 0
   while i < len(name):
      if not _is_safe_symbol_subsequent(name[i]):
         return True
      i = i + 1
   return False


def _escape_symbol_name(name):
   result = []
   i = 0
   while i < len(name):
      c = name[i]
      if c == '|':
         result.append('\\|')
      elif c == '\\':
         result.append('\\\\')
      else:
         result.append(c)
      i = i + 1
   return ''.join(result)


def _escape_string(s):
   result = []
   i = 0
   while i < len(s):
      c = s[i]
      if c == '\n':
         result.append('\\n')
      elif c == '\t':
         result.append('\\t')
      elif c == '\r':
         result.append('\\r')
      elif c == '\\':
         result.append('\\\\')
      elif c == '"':
         result.append('\\"')
      else:
         result.append(c)
      i = i + 1
   return ''.join(result)


if __name__ == '__main__':
   samples = [
      (INTEGER, 42),
      (INTEGER, -7),
      (REAL, 3.14),
      (REAL, 3.0),
      (REAL, 1e10),
      (RATIONAL, 3, 4),
      (BOOLEAN, True),
      (BOOLEAN, False),
      (CHARACTER, 'a'),
      (CHARACTER, ' '),
      (CHARACTER, '\n'),
      make_string('hello'),
      make_string('line1\nline2'),
      make_string('she said "hi"'),
      (CLOSURE, 'x', (None,), None),
      (SYMBOL, 'foo'),
      [(SYMBOL, 'a'), (SYMBOL, 'b'), (INTEGER, 3)],
   ]
   i = 0
   while i < len(samples):
      v = samples[i]
      print('%-60r  ->  %s' % (v, pretty_print(v)))
      i = i + 1
