"""R7RS library system (5.6): define-library, import, export.

Runtime support for library definitions and imports.  The structural forms
(define-library, import) are dispatched in the Evaluator; this module
provides the state and helpers they use.

    _LIBRARY_REGISTRY      - dict: key -> Environment holding exports
    library_name_to_key    - serialize a library name to a registry key
    library_register       - register a library under a key
    library_lookup         - retrieve a library's exports env by key
    resolve_import_set     - walk an import-set expression and return
                             { name: value } for each export to install
    register_standard_libraries - populate (scheme base) etc. at startup

Library keys: a library name like `(scheme base)` serializes to
`"scheme.base"`.  Name parts may be symbols or integers; integers become
decimal digits (e.g. `(srfi 1)` -> `"srfi.1"`).
"""

from pyscheme.AST import (
   is_cons, is_nil, is_symbol, is_integer,
   as_symbol, as_integer,
   intern_symbol, symbol_name,
)
from pyscheme.Environment import Environment


# Module-level library registry.  Populated at startup by
# register_standard_libraries and at runtime by define-library.
_LIBRARY_REGISTRY = {}


def library_name_to_key(name_sexpr):
   """Serialize a library-name list to a dotted string key.
   Parts are symbols or integers; empty names raise."""
   parts = []
   cur = name_sexpr
   while is_cons(cur):
      part = cur.car
      if is_symbol(part):
         parts.append(as_symbol(part))
      elif is_integer(part):
         parts.append(str(as_integer(part)))
      else:
         raise ValueError(
            'library name parts must be symbols or integers')
      cur = cur.cdr
   if not is_nil(cur):
      raise ValueError('library name must be a proper list')
   if not parts:
      raise ValueError('empty library name')
   return '.'.join(parts)


def library_register(key, exports_env):
   _LIBRARY_REGISTRY[key] = exports_env


def library_lookup(key):
   return _LIBRARY_REGISTRY.get(key)


def library_registered_p(key):
   return key in _LIBRARY_REGISTRY


def _render_name(name_sexpr):
   """Render a library-name list as `(scheme base)` for diagnostics."""
   parts = []
   cur = name_sexpr
   while is_cons(cur):
      part = cur.car
      if is_symbol(part):
         parts.append(as_symbol(part))
      elif is_integer(part):
         parts.append(str(as_integer(part)))
      else:
         parts.append('?')
      cur = cur.cdr
   return '(' + ' '.join(parts) + ')'


def resolve_import_set(import_set):
   """Walk an import-set expression and return { export_name: value }.
   Supports (only ...), (except ...), (rename ...), (prefix ...), and
   bare library names.  Raises ValueError on shape errors or unknown
   libraries; callers re-raise as positioned SchemeSyntaxError."""
   if not is_cons(import_set):
      raise ValueError('import set must be a list')

   head = import_set.car
   tail = import_set.cdr

   if is_symbol(head):
      name = as_symbol(head)

      if name == 'only':
         if not is_cons(tail):
            raise ValueError("malformed 'only' import set")
         base = resolve_import_set(tail.car)
         result = {}
         names_cur = tail.cdr
         while is_cons(names_cur):
            if not is_symbol(names_cur.car):
               raise ValueError("'only' filter must be a symbol")
            n = as_symbol(names_cur.car)
            if n not in base:
               raise ValueError(
                  "'only' references name not in library: " + n)
            result[n] = base[n]
            names_cur = names_cur.cdr
         return result

      if name == 'except':
         if not is_cons(tail):
            raise ValueError("malformed 'except' import set")
         result = dict(resolve_import_set(tail.car))
         names_cur = tail.cdr
         while is_cons(names_cur):
            if not is_symbol(names_cur.car):
               raise ValueError("'except' filter must be a symbol")
            result.pop(as_symbol(names_cur.car), None)
            names_cur = names_cur.cdr
         return result

      if name == 'rename':
         if not is_cons(tail):
            raise ValueError("malformed 'rename' import set")
         base = resolve_import_set(tail.car)
         result = dict(base)
         pairs_cur = tail.cdr
         while is_cons(pairs_cur):
            pair = pairs_cur.car
            pairs_cur = pairs_cur.cdr
            if (not is_cons(pair) or not is_symbol(pair.car)
                  or not is_cons(pair.cdr) or not is_symbol(pair.cdr.car)):
               raise ValueError(
                  "'rename' pair must be (old-name new-name)")
            old_name = as_symbol(pair.car)
            new_name = as_symbol(pair.cdr.car)
            if old_name not in base:
               raise ValueError(
                  "'rename' references name not in library: " + old_name)
            result.pop(old_name, None)
            result[new_name] = base[old_name]
         return result

      if name == 'prefix':
         if (not is_cons(tail) or not is_cons(tail.cdr)
               or not is_symbol(tail.cdr.car)):
            raise ValueError(
               "'prefix' must be (prefix <import-set> <symbol>)")
         base = resolve_import_set(tail.car)
         pfx = as_symbol(tail.cdr.car)
         result = {}
         for n in base:
            result[pfx + n] = base[n]
         return result

   # Plain library name: look up in registry.
   key = library_name_to_key(import_set)
   exports_env = library_lookup(key)
   if exports_env is None:
      raise ValueError('library not found: ' + _render_name(import_set))

   # Copy every binding from exports_env.
   result = {}
   for sid, val in exports_env._bindings.items():
      result[symbol_name(sid)] = val
   return result


def register_standard_libraries(global_env):
   """Populate (scheme base) and other R7RS library names with whatever
   matching bindings exist in global_env.  Names without a binding are
   silently skipped so a given library can be partially populated as the
   interpreter gains more primitives."""
   _register_filtered(global_env, 'scheme.base', _SCHEME_BASE_NAMES)
   _register_filtered(global_env, 'scheme.lazy',
      ['force', 'make-promise', 'promise?'])
   _register_filtered(global_env, 'scheme.eval', ['eval', 'environment'])
   # Empty env registration: the library's forms are special syntax
   # handled by the evaluator / expander, so there are no procedures to
   # export, but import should still succeed.
   _empty_lib = Environment(parent=None)
   _empty_lib.freeze()
   library_register('scheme.case-lambda', _empty_lib)
   _register_filtered(global_env, 'scheme.r5rs', _SCHEME_R5RS_NAMES)
   _register_filtered(global_env, 'scheme.inexact', _SCHEME_INEXACT_NAMES)
   _register_filtered(global_env, 'scheme.complex', _SCHEME_COMPLEX_NAMES)
   _register_filtered(global_env, 'scheme.char',    _SCHEME_CHAR_NAMES)
   _register_filtered(global_env, 'scheme.write',
      ['display', 'newline', 'write', 'write-char', 'write-shared', 'write-simple'])
   _register_filtered(global_env, 'scheme.read',  ['read'])
   _register_filtered(global_env, 'scheme.load',  ['load'])
   _register_filtered(global_env, 'scheme.file',  _SCHEME_FILE_NAMES)
   _register_filtered(global_env, 'scheme.repl',  ['interaction-environment'])
   _register_filtered(global_env, 'scheme.process-context',
      ['command-line', 'emergency-exit', 'exit',
       'get-environment-variable', 'get-environment-variables'])
   _register_filtered(global_env, 'scheme.time',
      ['current-jiffy', 'current-second', 'jiffies-per-second'])
   _register_filtered(global_env, 'scheme.cxr',  _SCHEME_CXR_NAMES)
   _register_filtered(global_env, 'srfi.39',     _SRFI_39_NAMES)


def _register_filtered(global_env, key, names):
   """Register a library under `key` containing bindings for each name in
   `names` that exists in global_env.  Missing names are skipped.  The
   resulting export environment is frozen: R7RS forbids modifying it."""
   exports_env = Environment(parent=None)
   i = 0
   while i < len(names):
      n = names[i]
      if intern_symbol(n) in global_env._bindings:
         exports_env.bind(n, global_env.lookup(n))
      i = i + 1
   exports_env.freeze()
   library_register(key, exports_env)


# R7RS (scheme base) procedures (syntax forms aren't in the export table -
# they live in the evaluator / expander).  Names not implemented are
# silently skipped by _register_filtered.
_SCHEME_BASE_NAMES = [
   # Arithmetic
   '*', '+', '-', '/', '<', '<=', '=', '>', '>=',
   'abs', 'ceiling', 'denominator', 'exact', 'exact-integer?', 'exact?',
   'expt', 'floor', 'floor-quotient', 'floor-remainder', 'floor/',
   'gcd', 'inexact', 'inexact?', 'integer?', 'lcm', 'max', 'min',
   'modulo', 'negative?', 'number->string', 'number?', 'numerator',
   'odd?', 'even?', 'positive?', 'quotient', 'rational?', 'real?',
   'remainder', 'round', 'square', 'string->number',
   'truncate', 'truncate-quotient', 'truncate-remainder', 'truncate/',
   'zero?',
   # Booleans
   'boolean=?', 'boolean?', 'not',
   # Pairs and lists
   'append', 'assoc', 'assq', 'assv',
   'caar', 'cadr', 'cdar', 'cddr',
   'car', 'cdr', 'cons',
   'for-each', 'length', 'list', 'list->string', 'list->vector',
   'list-copy', 'list-ref', 'list-set!', 'list-tail', 'list?',
   'make-list', 'map', 'member', 'memq', 'memv',
   'null?', 'pair?', 'reverse', 'set-car!', 'set-cdr!',
   # Strings
   'make-string', 'string', 'string->list', 'string->symbol',
   'string->vector', 'string-append', 'string-copy', 'string-copy!',
   'string-fill!', 'string-for-each', 'string-length', 'string-map',
   'string-ref', 'string-set!',
   'string<=?', 'string<?', 'string=?', 'string>=?', 'string>?', 'string?',
   'substring', 'symbol->string', 'symbol=?', 'symbol?',
   # Vectors
   'list->vector', 'make-vector', 'vector', 'vector->list', 'vector->string',
   'vector-append', 'vector-copy', 'vector-copy!', 'vector-fill!',
   'vector-for-each', 'vector-length', 'vector-map',
   'vector-ref', 'vector-set!', 'vector?',
   # Control
   'apply', 'call-with-values', 'dynamic-wind', 'values',
   'call-with-current-continuation', 'call/cc',
   # Predicates
   'procedure?',
   # Parameters
   'make-parameter',
   # Equality
   'eq?', 'equal?', 'eqv?',
   # Errors and exceptions
   'error', 'error-object-irritants', 'error-object-message', 'error-object?',
   'raise', 'raise-continuable', 'with-exception-handler',
   # Help
   'help', 'apropos',
]

_SCHEME_R5RS_NAMES = [
   '*', '+', '-', '/', '<', '<=', '=', '>', '>=',
   'abs', 'append', 'apply', 'assoc', 'assq', 'assv',
   'boolean?', 'caar', 'cadr', 'cdar', 'cddr',
   'exact->inexact', 'inexact->exact',
   'car', 'cdr', 'cons',
   'denominator', 'dynamic-wind',
   'eq?', 'equal?', 'eqv?',
   'error', 'eval', 'even?', 'expt',
   'floor', 'for-each', 'force',
   'gcd',
   'integer?',
   'lcm', 'length', 'list', 'list-ref', 'list-tail', 'list?',
   'make-vector', 'map', 'max', 'member', 'memq', 'memv', 'min', 'modulo',
   'negative?', 'not', 'null-environment', 'null?', 'number?', 'numerator',
   'odd?',
   'pair?', 'positive?', 'procedure?',
   'quotient', 'rational?', 'real?', 'remainder', 'reverse', 'round',
   'scheme-report-environment',
   'set-car!', 'set-cdr!',
   'symbol?',
   'truncate', 'values', 'call-with-values',
   'vector', 'vector->list', 'vector-length', 'vector-ref', 'vector-set!',
   'vector?',
   'with-exception-handler',
   'zero?',
]

_SCHEME_INEXACT_NAMES = [
   'acos', 'asin', 'atan', 'cos', 'exp',
   'finite?', 'infinite?', 'log', 'nan?',
   'sin', 'sqrt', 'tan',
   'floor', 'ceiling', 'truncate', 'round', 'exact', 'inexact',
]

_SCHEME_COMPLEX_NAMES = [
   'angle', 'imag-part', 'magnitude', 'make-polar', 'make-rectangular',
   'real-part',
]

_SCHEME_CHAR_NAMES = [
   'char-alphabetic?', 'char-ci<=?', 'char-ci<?', 'char-ci=?',
   'char-ci>=?', 'char-ci>?', 'char-downcase', 'char-foldcase',
   'char-lower-case?', 'char-numeric?', 'char-upcase', 'char-upper-case?',
   'char-whitespace?', 'digit-value',
   'string-ci<=?', 'string-ci<?', 'string-ci=?', 'string-ci>=?', 'string-ci>?',
   'string-downcase', 'string-foldcase', 'string-upcase',
]

_SCHEME_FILE_NAMES = [
   'call-with-input-file', 'call-with-output-file',
   'delete-file', 'file-exists?', 'rename-file',
   'open-binary-input-file', 'open-binary-output-file',
   'open-input-file', 'open-output-file',
   'with-input-from-file', 'with-output-to-file',
]

_SRFI_39_NAMES = [
   'make-parameter', 'parameter?',
]

_SCHEME_CXR_NAMES = [
   'caaar', 'caadr', 'cadar', 'caddr',
   'cdaar', 'cdadr', 'cddar', 'cdddr',
   'caaaar', 'caaadr', 'caadar', 'caaddr',
   'cadaar', 'cadadr', 'caddar', 'cadddr',
   'cdaaar', 'cdaadr', 'cdadar', 'cdaddr',
   'cddaar', 'cddadr', 'cdddar', 'cddddr',
]


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
   from pyscheme.AST import (
      alloc_cons, NIL_VALUE, make_symbol, make_integer,
   )

   n_pass = 0
   n_fail = 0

   def check(label, cond):
      global n_pass, n_fail
      if cond:
         print('[ OK ] ' + label)
         n_pass = n_pass + 1
      else:
         print('[FAIL] ' + label)
         n_fail = n_fail + 1

   def make_name(*parts):
      # Build (sym1 sym2 ...) as a proper list.
      items = []
      i = 0
      while i < len(parts):
         p = parts[i]
         if isinstance(p, int):
            items.append(make_integer(p))
         else:
            items.append(make_symbol(p))
         i = i + 1
      result = NIL_VALUE
      i = len(items) - 1
      while i >= 0:
         result = alloc_cons(items[i], result)
         i = i - 1
      return result

   # library_name_to_key
   check('key scheme.base',
         library_name_to_key(make_name('scheme', 'base')) == 'scheme.base')
   check('key srfi.1',
         library_name_to_key(make_name('srfi', 1)) == 'srfi.1')
   check('key single',
         library_name_to_key(make_name('foo')) == 'foo')

   try:
      library_name_to_key(NIL_VALUE)
      check('empty name raises', False)
   except ValueError:
      check('empty name raises', True)

   # register / lookup
   env = Environment()
   env.bind('x', 42)
   library_register('test.lib', env)
   check('lookup finds',       library_lookup('test.lib') is env)
   check('registered_p',       library_registered_p('test.lib'))
   check('lookup missing None', library_lookup('nope') is None)

   # resolve_import_set: plain name
   bindings = resolve_import_set(make_name('test', 'lib'))
   # test.lib not registered; create one
   env2 = Environment()
   env2.bind('hello', 'world')
   library_register('mylib', env2)
   bindings = resolve_import_set(make_name('mylib'))
   check('plain-name x',       bindings == {'hello': 'world'})

   # only
   env3 = Environment()
   env3.bind('a', 1); env3.bind('b', 2); env3.bind('c', 3)
   library_register('abc', env3)
   only_form = alloc_cons(make_symbol('only'),
                         alloc_cons(make_name('abc'),
                                   alloc_cons(make_symbol('a'),
                                             alloc_cons(make_symbol('c'), NIL_VALUE))))
   check('only filter',        resolve_import_set(only_form) == {'a': 1, 'c': 3})

   # except
   except_form = alloc_cons(make_symbol('except'),
                           alloc_cons(make_name('abc'),
                                     alloc_cons(make_symbol('b'), NIL_VALUE)))
   check('except filter',      resolve_import_set(except_form) == {'a': 1, 'c': 3})

   # rename
   rename_pair = alloc_cons(make_symbol('a'),
                            alloc_cons(make_symbol('alpha'), NIL_VALUE))
   rename_form = alloc_cons(make_symbol('rename'),
                           alloc_cons(make_name('abc'),
                                     alloc_cons(rename_pair, NIL_VALUE)))
   renamed = resolve_import_set(rename_form)
   check('rename keeps b,c',   renamed.get('b') == 2 and renamed.get('c') == 3)
   check('rename adds alpha',  renamed.get('alpha') == 1)
   check('rename drops a',     'a' not in renamed)

   # prefix
   prefix_form = alloc_cons(make_symbol('prefix'),
                           alloc_cons(make_name('abc'),
                                     alloc_cons(make_symbol('my-'), NIL_VALUE)))
   prefixed = resolve_import_set(prefix_form)
   check('prefix all',         prefixed == {'my-a': 1, 'my-b': 2, 'my-c': 3})

   # unknown library
   try:
      resolve_import_set(make_name('no', 'such', 'lib'))
      check('unknown lib raises', False)
   except ValueError:
      check('unknown lib raises', True)

   print()
   print('%d passed, %d failed' % (n_pass, n_fail))
