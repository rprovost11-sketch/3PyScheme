"""Debug primitives: describe, inspect, debug.

Ported from pythonslisp/extensions/debug.py.

All code follows C-port discipline (no f-strings, no tuple unpacking on LHS,
no comprehensions, explicit type-tag dispatch via is_X/as_X helpers).
"""

from pyscheme.AST import (
   NIL_VALUE, VOID_VALUE,
   VOID, BOOLEAN, COMPLEX, REAL, RATIONAL, INTEGER, CHARACTER, STRING,
   CLOSURE, PAIR, NIL, PRIMITIVE, CASE_CLOSURE, PROMISE, MULTI_VALUES,
   CONTINUATION, ENVIRONMENT, RECORD, RECORD_ACCESSOR, RECORD_MUTATOR,
   PARAMETER, VECTOR, BYTEVECTOR, PORT, SYMBOL,
   is_cons, is_nil, is_symbol, is_closure, is_primitive,
   is_case_closure, is_continuation, is_promise, is_multi_values,
   is_record, is_parameter,
   as_symbol, as_closure_params, as_closure_body, as_closure_rest_name,
   as_primitive_name, as_case_closure_clauses,
   as_promise_is_done, as_promise_payload,
   as_multi_values_list,
   as_record_type, as_record_type_name, as_record_fields,
   as_parameter_value,
   as_vector_items, as_bytevector_items, as_port,
   make_primitive,
)
from pyscheme.Environment import SchemeUnboundError, SchemeTypeError
from pyscheme.PrettyPrinter import pretty_print
from pyscheme.primitives import register_primitive


# ── Type name helper ─────────────────────────────────────────────────────

def _scheme_type_name(val):
   """Return a display type name string for any Scheme value."""
   if val is NIL_VALUE:
      return 'null'
   if val is VOID_VALUE:
      return 'void'
   if isinstance(val, tuple):
      tag = val[0]
      if tag == BOOLEAN:
         return 'boolean'
      if tag == INTEGER:
         return 'integer'
      if tag == REAL:
         return 'real'
      if tag == RATIONAL:
         return 'rational'
      if tag == COMPLEX:
         return 'complex'
      if tag == CHARACTER:
         return 'character'
      if tag == STRING:
         return 'string'
      if tag == SYMBOL:
         return 'symbol'
      if tag == CLOSURE:
         return 'procedure'
      if tag == CASE_CLOSURE:
         return 'procedure'
      if tag == PRIMITIVE:
         return 'primitive'
      if tag == PROMISE:
         return 'promise'
      if tag == MULTI_VALUES:
         return 'values'
      if tag == CONTINUATION:
         return 'continuation'
      if tag == ENVIRONMENT:
         return 'environment'
      if tag == RECORD:
         return 'record'
      if tag == RECORD_ACCESSOR:
         return 'record-accessor'
      if tag == RECORD_MUTATOR:
         return 'record-mutator'
      if tag == PARAMETER:
         return 'parameter'
      if tag == VECTOR:
         return 'vector'
      if tag == BYTEVECTOR:
         return 'bytevector'
      if tag == PORT:
         return 'port'
      return 'unknown'
   if is_cons(val):
      return 'pair'
   return 'unknown'


# ── Object description ───────────────────────────────────────────────────

def _describe_object(val, out):
   """Print a structured description of val to out."""
   if val is NIL_VALUE:
      print('() is the empty list (null)', file=out)
      return
   if val is VOID_VALUE:
      print('#<void> is void', file=out)
      return
   if is_cons(val):
      preview = pretty_print(val)
      if len(preview) > 60:
         preview = preview[:57] + '...'
      # Count length of proper list portion.
      length = 0
      cur    = val
      while is_cons(cur):
         length = length + 1
         cur    = cur.cdr
      if is_nil(cur):
         print(preview + ' is a proper list, length ' + str(length), file=out)
      else:
         print(preview + ' is an improper pair', file=out)
      return
   if not isinstance(val, tuple):
      print(pretty_print(val) + ' is of unknown type', file=out)
      return
   tag = val[0]
   if tag == BOOLEAN:
      bstr = '#t' if val[1] else '#f'
      print(bstr + ' is a boolean', file=out)
      return
   if tag == INTEGER:
      print(str(val[1]) + ' is an integer', file=out)
      return
   if tag == REAL:
      print(pretty_print(val) + ' is a real', file=out)
      return
   if tag == RATIONAL:
      print(pretty_print(val) + ' is a rational', file=out)
      print('  Numerator:   ' + str(val[1]), file=out)
      print('  Denominator: ' + str(val[2]), file=out)
      return
   if tag == COMPLEX:
      print(pretty_print(val) + ' is a complex', file=out)
      return
   if tag == CHARACTER:
      print(pretty_print(val) + ' is a character', file=out)
      return
   if tag == STRING:
      s = val[1]
      print(pretty_print(val) + ' is a string', file=out)
      print('  Length: ' + str(len(s)), file=out)
      return
   if tag == SYMBOL:
      print(val[1] + ' is a symbol', file=out)
      return
   if tag == CLOSURE:
      params    = as_closure_params(val)
      rest_name = as_closure_rest_name(val)
      if rest_name is not None and len(params) == 0:
         params_str = rest_name
      elif rest_name is not None:
         params_str = '(' + ' '.join(params) + ' . ' + rest_name + ')'
      elif params:
         params_str = '(' + ' '.join(params) + ')'
      else:
         params_str = '()'
      print('#<procedure ' + params_str + '> is a procedure', file=out)
      docstring = val[5]
      if docstring:
         print('  Documentation:', file=out)
         for line in docstring.splitlines():
            print('    ' + line, file=out)
      return
   if tag == CASE_CLOSURE:
      clauses = as_case_closure_clauses(val)
      print('#<case-lambda, ' + str(len(clauses)) + ' clause(s)> is a procedure', file=out)
      docstring = val[3]
      if docstring:
         print('  Documentation:', file=out)
         for line in docstring.splitlines():
            print('    ' + line, file=out)
      return
   if tag == PRIMITIVE:
      name = as_primitive_name(val)
      print(name + ' is a primitive procedure', file=out)
      from pyscheme.primitives import PRIMITIVE_HELP
      entry = PRIMITIVE_HELP.get(name)
      if entry is not None:
         kind    = entry[0]
         usage   = entry[1]
         doc     = entry[2]
         if kind == 'special':
            print('  Kind: special form', file=out)
         print('  Usage: ' + usage, file=out)
         if doc:
            print('  Documentation:', file=out)
            for line in doc.splitlines():
               print('    ' + line, file=out)
      return
   if tag == PROMISE:
      if as_promise_is_done(val):
         print('#<promise, forced> is a promise', file=out)
         print('  Value: ' + pretty_print(as_promise_payload(val)), file=out)
      else:
         print('#<promise, unforced> is a promise', file=out)
      return
   if tag == CONTINUATION:
      print('#<continuation> is a continuation', file=out)
      return
   if tag == MULTI_VALUES:
      vals = as_multi_values_list(val)
      print('#<values> contains ' + str(len(vals)) + ' value(s)', file=out)
      i = 0
      while i < len(vals):
         print('  ' + str(i) + ': ' + pretty_print(vals[i]), file=out)
         i = i + 1
      return
   if tag == RECORD:
      rt        = as_record_type(val)
      type_name = as_record_type_name(rt)
      fields    = as_record_fields(val)
      print(pretty_print(val) + ' is a ' + type_name + ' (record)', file=out)
      print('  Fields: ' + str(len(fields)), file=out)
      return
   if tag == PARAMETER:
      print('#<parameter> is a parameter object', file=out)
      print('  Value: ' + pretty_print(as_parameter_value(val)), file=out)
      return
   if tag == VECTOR:
      items = as_vector_items(val)
      print('#(' + '...' + ') is a vector, length ' + str(len(items)), file=out)
      return
   if tag == BYTEVECTOR:
      items = as_bytevector_items(val)
      print('#u8(...) is a bytevector, length ' + str(len(items)), file=out)
      return
   if tag == PORT:
      port = as_port(val)
      if hasattr(port, 'closed'):
         status = 'closed' if port.closed else 'open'
      else:
         status = 'open'
      print('#<port> is a port, status: ' + status, file=out)
      return
   print(pretty_print(val) + ' is of type ' + _scheme_type_name(val), file=out)


# ── Inspectable children ─────────────────────────────────────────────────

def _inspectable_children(val):
   """Return a list of [label, child] pairs for navigation, or None."""
   if is_cons(val):
      items = []
      cur   = val
      idx   = 0
      while is_cons(cur):
         items.append([str(idx), cur.car])
         idx = idx + 1
         cur = cur.cdr
      if not is_nil(cur):
         items.append(['tail', cur])
      return items if items else None
   if isinstance(val, tuple):
      tag = val[0]
      if tag == MULTI_VALUES:
         vals   = as_multi_values_list(val)
         result = []
         i = 0
         while i < len(vals):
            result.append([str(i), vals[i]])
            i = i + 1
         return result if result else None
      if tag == RECORD:
         rt     = as_record_type(val)
         fields = as_record_fields(val)
         result = []
         i = 0
         while i < len(fields):
            result.append([str(i), fields[i]])
            i = i + 1
         return result if result else None
      if tag == VECTOR:
         items  = as_vector_items(val)
         result = []
         i = 0
         while i < len(items):
            result.append([str(i), items[i]])
            i = i + 1
         return result if result else None
   return None


def _print_inspect(val, out):
   """Print inspect-style view with numbered children."""
   children = _inspectable_children(val)
   if children is None:
      _describe_object(val, out)
      return
   type_name = _scheme_type_name(val)
   if is_cons(val):
      length = 0
      cur    = val
      while is_cons(cur):
         length = length + 1
         cur    = cur.cdr
      if is_nil(cur):
         print('pair (proper list), ' + str(length) + ' elements', file=out)
      else:
         print('pair (improper), ' + str(length) + ' car(s) + tail', file=out)
   elif isinstance(val, tuple) and val[0] == MULTI_VALUES:
      vals = as_multi_values_list(val)
      print('values, ' + str(len(vals)) + ' element(s)', file=out)
   elif isinstance(val, tuple) and val[0] == RECORD:
      rt   = as_record_type(val)
      name = as_record_type_name(rt)
      print(name + ' (record), ' + str(len(children)) + ' field(s)', file=out)
   elif isinstance(val, tuple) and val[0] == VECTOR:
      items = as_vector_items(val)
      print('vector, ' + str(len(items)) + ' element(s)', file=out)
   i = 0
   while i < len(children):
      label = children[i][0]
      child = children[i][1]
      print('  ' + label + ': ' + pretty_print(child), file=out)
      i = i + 1


def _run_inspect(val, out, input_fn=None):
   """Run the interactive inspect loop on val.  Returns when the user exits.
   input_fn defaults to the built-in input() if not provided."""
   if input_fn is None:
      input_fn = input

   history = []

   children = _inspectable_children(val)
   if children is None:
      _describe_object(val, out)
      return

   while True:
      _print_inspect(val, out)
      children = _inspectable_children(val)
      if children is None:
         if history:
            val = history.pop()
            continue
         break

      try:
         line = input_fn('inspect> ').strip()
      except (EOFError, KeyboardInterrupt):
         print(file=out)
         break

      if line == '' or line == 'q' or line == 'quit':
         break
      if line == 'u' or line == 'up':
         if history:
            val = history.pop()
         else:
            break
         continue
      if line.isdigit():
         idx = int(line)
         # Find the child with matching label.
         found = False
         k = 0
         while k < len(children):
            if children[k][0] == line:
               history.append(val)
               val   = children[k][1]
               found = True
               break
            k = k + 1
         if not found:
            print('No element at index ' + line, file=out)
         continue
      print('Enter a number, u (up), or q (quit)', file=out)


# ── Tracer stubs (special forms implemented in Evaluator) ────────────────

def _stub_trace(ctx, env, args, app_node):
   raise Exception('trace: not callable as a procedure')


def _stub_untrace(ctx, env, args, app_node):
   raise Exception('untrace: not callable as a procedure')


# ── Primitive functions ───────────────────────────────────────────────────

def _prim_describe(ctx, env, args, app_node):
   """Prints a structured description of its argument: type, attributes, and
documentation (for procedures).  Returns void."""
   _describe_object(args[0], ctx.outStrm)
   return VOID_VALUE


def _prim_inspect(ctx, env, args, app_node):
   """Interactively inspect a structured value.  For pairs, vectors, records,
and multiple-values, displays numbered elements for navigation.
  <number>  - navigate into element at that index
  u         - go back up to the parent value
  q         - exit the inspector
For atoms, prints a description and returns immediately."""
   _run_inspect(args[0], ctx.outStrm)
   return VOID_VALUE


def _prim_debug(ctx, env, args, app_node):
   """Opens the interactive debugger.  Set breakpoints and watches, then use
rd to run expressions with debugging active.  Type h at the debug> prompt
for a full command reference."""
   ctx.debugger.run_debugger_repl(ctx, env)
   return VOID_VALUE


# ── Registration ─────────────────────────────────────────────────────────

def register():
   register_primitive('describe', (1, 1), _prim_describe,
                      doc=_prim_describe.__doc__, category='debug')
   register_primitive('inspect',  (1, 1), _prim_inspect,
                      doc=_prim_inspect.__doc__, category='debug')
   register_primitive('debug',    (0, 0), _prim_debug,
                      doc=_prim_debug.__doc__,  category='debug')
   register_primitive('trace',   (0, None), _stub_trace,
                      usage='(trace . names)',
                      doc='Enable call tracing for each named function.\n'
                          'With no arguments, return the list of currently traced functions.',
                      category='debug', kind='special')
   register_primitive('untrace', (0, None), _stub_untrace,
                      usage='(untrace . names)',
                      doc='Disable call tracing for each named function.\n'
                          'With no arguments, disable all tracing.',
                      category='debug', kind='special')
