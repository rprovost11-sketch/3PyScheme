"""AST.py - cons-cell AST and source-position infrastructure.

Single representation used by every stage of the pipeline: parser,
expander, analyzer, evaluator.  Cons cells are the heap spine.  Atoms
(symbols, numbers, strings, booleans, characters) and the NIL / VOID
sentinels are tagged tuples - immutable arms of the Value variant.

C port layout:
   ConsCell     -> struct ConsCell { GcHeader h; Value car; Value cdr; SourceInfo* src; };
   SourceInfo   -> struct SourceInfo { int line; int col; char* source_line; char* filename; };
   (SYMBOL, s)  -> Value with tag SYMBOL, payload Symbol*
   (NIL,)       -> Value with tag NIL, no payload (singleton)
   (VOID,)      -> Value with tag VOID, no payload (singleton)

All heap/tagged values are built via make_X allocators and examined
via is_X / as_X accessors - callers outside this module must not
index the underlying tuples directly.
"""


# --- Value tag constants -----------------------------------------------
# Tags for the tagged-tuple Value arms and the GcHeader.type field
# in the C port.  ConsCell does not need a tag in the Python prototype
# because isinstance() discriminates it from tuples; in C its
# GcHeader.type carries PAIR.

VOID         = 0
BOOLEAN      = 1
COMPLEX      = 2
REAL         = 3
RATIONAL     = 4
INTEGER      = 5
CHARACTER    = 6
STRING       = 7
CLOSURE      = 8
PAIR         = 9
NIL          = 10
PRIMITIVE    = 11
CASE_CLOSURE       = 12
PROMISE            = 13
MULTI_VALUES       = 14
RECORD             = 15
PARAMETER          = 16
CONTINUATION       = 17
SYNTAX_TRANSFORMER = 18
ENVIRONMENT        = 19
RECORD_ACCESSOR    = 20
RECORD_MUTATOR     = 21
VECTOR             = 22
BYTEVECTOR         = 23
SYMBOL             = 100


# --- REPL filename sentinel --------------------------------------------
# Listener / Interpreter pass this as the filename for every REPL input
# so diagnostics can distinguish REPL from loaded files.

REPL_FILENAME = '<repl>'


# --- SourceInfo --------------------------------------------------------

class SourceInfo:
   """Source position attached to a cons cell.  POD; no methods.
   Owned 1:1 by its ConsCell - freed with the cell in the C port."""
   def __init__(self, line, col, source_line, filename):
      self.line        = line
      self.col         = col
      self.source_line = source_line
      self.filename    = filename


# --- ConsCell ----------------------------------------------------------

class ConsCell:
   """Cons cell.  POD; no methods.  Mutable: set_car / set_cdr update
   fields in place.  src is owned by this cell."""
   def __init__(self, car, cdr, src=None):
      self.car = car
      self.cdr = cdr
      self.src = src


class Promise:
   """R7RS promise box.  POD; mutable.  When is_done is False, payload is
   a zero-arg CLOSURE that produces the value on demand.  When True,
   payload is the memoized value.  force mutates in place."""
   def __init__(self, is_done, payload):
      self.is_done = is_done
      self.payload = payload


class RecordType:
   """Descriptor for a define-record-type type.  POD; one instance per
   `(define-record-type ...)` form, shared by identity across all records
   of that type."""
   def __init__(self, name, field_names):
      self.name = name
      self.field_names = field_names


class Parameter:
   """R7RS parameter object.  POD; mutable.  `value` is the current dynamic
   value; `converter` is None or a callable Value applied at make-parameter
   time and each parameterize binding.  Parameters are callable with 0 args
   to read the value; other arities error."""
   def __init__(self, value, converter):
      self.value = value
      self.converter = converter


class ErrorObject:
   """R7RS error object (6.11): carries a message string and a Python list
   of irritants.  Produced by the `error` primitive; retrieved by
   error-object-message and error-object-irritants."""
   def __init__(self, message, irritants):
      self.message = message
      self.irritants = irritants


class Continuation:
   """First-class continuation (R7RS 6.10).  POD; immutable.  k_snapshot is
   a Python list holding a copy of the CEK K-stack at the call/cc call
   site; wind_snapshot is a copy of the dynamic-wind stack at the same
   point; handler_snapshot is a copy of the handler stack so a continuation
   captured inside with-exception-handler keeps its handlers when invoked
   from elsewhere (and stale FRAME_POP_HANDLER frames in k_snapshot find
   the matching handler entries to pop).  Invoking the continuation walks
   the wind stack from current to wind_snapshot, restores the handler
   stack from handler_snapshot, replaces K with a copy of k_snapshot, and
   resumes the CEK machine."""
   def __init__(self, k_snapshot, wind_snapshot, handler_snapshot):
      self.k_snapshot = k_snapshot
      self.wind_snapshot = wind_snapshot
      self.handler_snapshot = handler_snapshot


class SyntaxTransformer:
   """syntax-rules transformer (R7RS 4.3).  POD; immutable after parse.
      name      - str, used in diagnostics only
      literals  - list of str, literal identifiers in patterns
      ellipsis  - str, the ellipsis identifier (default '...')
      rules     - list of (pattern_sexpr, template_sexpr) pairs
      def_env   - dict: macro definition-time env (for hygiene lookup)
      is a regular-Python dict mapping name -> SyntaxTransformer; non-macro
      references fall through to the runtime env at evaluation."""
   def __init__(self, name, literals, ellipsis, rules, def_env):
      self.name = name
      self.literals = literals
      self.ellipsis = ellipsis
      self.rules = rules
      self.def_env = def_env


# --- Singletons --------------------------------------------------------
# NIL_VALUE and VOID_VALUE are module-level singletons.  Historically
# some callers construct (NIL,) / (VOID,) inline; the is_nil / is_void
# predicates accept either form.  New code should prefer the singletons
# (or make_nil / make_void).

NIL_VALUE  = (NIL,)
VOID_VALUE = (VOID,)


# --- Allocators --------------------------------------------------------

def alloc_cons(car_val, cdr_val, src=None):
   return ConsCell(car_val, cdr_val, src)

def make_nil(src=None):
   """Return the empty-list value.  With no src, returns the shared
   NIL_VALUE singleton (runtime-constructed nils).  With src, returns
   a positioned tuple (NIL, src) that carries the source position of
   a literal () in user code."""
   if src is None:
      return NIL_VALUE
   return (NIL, src)

def make_void():
   return VOID_VALUE

def make_boolean(b, src=None):
   return (BOOLEAN, b, src)

def make_integer(n, src=None):
   return (INTEGER, n, src)

def make_real(x, src=None):
   return (REAL, x, src)

def make_rational(num, den, src=None):
   return (RATIONAL, num, den, src)

def make_complex(re, im, src=None):
   return (COMPLEX, re, im, src)

def make_character(c, src=None):
   return (CHARACTER, c, src)

def make_string(s, src=None):
   return (STRING, s, src)

def make_symbol(name, src=None):
   return (SYMBOL, name, src)

def make_closure(params, body, env, rest_name, docstring):
   return (CLOSURE, params, body, env, rest_name, docstring)

def make_primitive(name, fn):
   return (PRIMITIVE, name, fn)

def make_case_closure(clauses, env, docstring):
   """Build a case-lambda closure.  `clauses` is a Python list of
   (params, body_cons, rest_name) triples.  At call time the evaluator
   dispatches to the first clause whose param shape accepts the
   argument count."""
   return (CASE_CLOSURE, clauses, env, docstring)

def make_promise_lazy(thunk):
   return Promise(False, thunk)

def make_promise_done(value):
   return Promise(True, value)

def make_multi_values(values_list, src=None):
   return (MULTI_VALUES, values_list, src)

def make_record_type(name, field_names):
   return RecordType(name, field_names)

def make_record(record_type, field_values):
   return (RECORD, record_type, field_values)

def make_parameter(value, converter):
   return Parameter(value, converter)

def make_error_object(message, irritants):
   return ErrorObject(message, irritants)

def make_continuation(k_snapshot, wind_snapshot, handler_snapshot):
   return Continuation(k_snapshot, wind_snapshot, handler_snapshot)

def make_syntax_transformer(name, literals, ellipsis, rules, def_env):
   return SyntaxTransformer(name, literals, ellipsis, rules, def_env)

def make_environment(env):
   return (ENVIRONMENT, env)

def make_record_accessor(record_type, index, name):
   """Build a record accessor value.  Applied at a call site, performs
   a type-checked field read; the type-error position is the call site,
   not the define-record-type form, because the Evaluator dispatches
   this value inline using the app_node's src."""
   return (RECORD_ACCESSOR, record_type, index, name)

def make_record_mutator(record_type, index, name):
   """Build a record mutator value.  Same dispatch story as record-accessor."""
   return (RECORD_MUTATOR, record_type, index, name)

def make_vector(items):
   """Build a vector value.  items is a Python list of element values;
   it is stored by reference, so mutations via vector-set! are visible
   to all sharers (matching Scheme vector semantics)."""
   return (VECTOR, items)

def make_bytevector(items):
   """Build a bytevector value.  items is a Python bytearray of u8
   values (0-255); stored by reference for in-place mutation."""
   return (BYTEVECTOR, items)


# --- Predicates --------------------------------------------------------

def is_cons(val):
   return isinstance(val, ConsCell)

def is_nil(val):
   if val is NIL_VALUE:
      return True
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == NIL

def is_void(val):
   if val is VOID_VALUE:
      return True
   return isinstance(val, tuple) and len(val) == 1 and val[0] == VOID

def is_boolean(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == BOOLEAN

def is_integer(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == INTEGER

def is_real(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == REAL

def is_rational(val):
   return isinstance(val, tuple) and len(val) >= 3 and val[0] == RATIONAL

def is_complex(val):
   return isinstance(val, tuple) and len(val) >= 3 and val[0] == COMPLEX

def is_character(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == CHARACTER

def is_string(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == STRING

def is_symbol(val):
   return isinstance(val, tuple) and len(val) >= 2 and val[0] == SYMBOL

def is_closure(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == CLOSURE

def is_primitive(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == PRIMITIVE

def is_case_closure(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == CASE_CLOSURE

def is_promise(val):
   return isinstance(val, Promise)

def is_multi_values(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == MULTI_VALUES

def is_record_type(val):
   return isinstance(val, RecordType)

def is_record(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == RECORD

def is_parameter(val):
   return isinstance(val, Parameter)

def is_error_object(val):
   return isinstance(val, ErrorObject)

def is_continuation(val):
   return isinstance(val, Continuation)

def is_syntax_transformer(val):
   return isinstance(val, SyntaxTransformer)

def is_environment(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == ENVIRONMENT

def is_record_accessor(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == RECORD_ACCESSOR

def is_record_mutator(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == RECORD_MUTATOR

def is_vector(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == VECTOR

def is_bytevector(val):
   return isinstance(val, tuple) and len(val) >= 1 and val[0] == BYTEVECTOR


# --- Accessors ---------------------------------------------------------

def car(val):
   return val.car

def cdr(val):
   return val.cdr

def src_of(val):
   """Return the SourceInfo for any AST value, or None if none is attached.
   Cons cells hold src as a field; atoms carry src as the last element of
   their tagged tuple.  A literal () parsed from source is (NIL, src); the
   runtime NIL_VALUE singleton and other payload-free arms return None."""
   if isinstance(val, ConsCell):
      return val.src
   if isinstance(val, Promise):
      return None
   if isinstance(val, RecordType):
      return None
   if isinstance(val, Parameter):
      return None
   if isinstance(val, ErrorObject):
      return None
   if isinstance(val, Continuation):
      return None
   if isinstance(val, SyntaxTransformer):
      return None
   if not isinstance(val, tuple) or len(val) == 0:
      return None
   tag = val[0]
   if tag == SYMBOL:
      return val[2]
   if tag == INTEGER:
      return val[2]
   if tag == REAL:
      return val[2]
   if tag == RATIONAL:
      return val[3]
   if tag == COMPLEX:
      return val[3]
   if tag == CHARACTER:
      return val[2]
   if tag == STRING:
      return val[2]
   if tag == BOOLEAN:
      return val[2]
   if tag == NIL:
      if len(val) >= 2:
         return val[1]
      return None
   if tag == MULTI_VALUES:
      if len(val) >= 3:
         return val[2]
      return None
   return None

def as_boolean(val):
   return val[1]

def as_integer(val):
   return val[1]

def as_real(val):
   return val[1]

def as_rational_num(val):
   return val[1]

def as_rational_den(val):
   return val[2]

def as_complex_real(val):
   return val[1]

def as_complex_imag(val):
   return val[2]

def as_character(val):
   return val[1]

def as_string(val):
   return val[1]

def as_symbol(val):
   return val[1]

def as_closure_params(val):
   return val[1]

def as_closure_body(val):
   return val[2]

def as_closure_env(val):
   return val[3]

def as_closure_rest_name(val):
   return val[4]

def as_closure_docstring(val):
   return val[5]

def as_primitive_name(val):
   return val[1]

def as_primitive_fn(val):
   return val[2]

def as_case_closure_clauses(val):
   return val[1]

def as_case_closure_env(val):
   return val[2]

def as_case_closure_docstring(val):
   return val[3]

def as_promise_is_done(val):
   return val.is_done

def as_promise_payload(val):
   return val.payload

def promise_resolve(p, value):
   p.is_done = True
   p.payload = value

def promise_become(p, other):
   p.is_done = other.is_done
   p.payload = other.payload

def as_multi_values_list(val):
   return val[1]

def as_record_type(val):
   return val[1]

def as_record_fields(val):
   return val[2]

def as_record_type_name(rt):
   return rt.name

def as_record_type_field_names(rt):
   return rt.field_names

def as_parameter_value(p):
   return p.value

def as_parameter_converter(p):
   return p.converter

def set_parameter_value(p, v):
   p.value = v

def as_error_object_message(e):
   return e.message

def as_error_object_irritants(e):
   return e.irritants

def as_continuation_k(c):
   return c.k_snapshot

def as_continuation_wind(c):
   return c.wind_snapshot

def as_continuation_handlers(c):
   return c.handler_snapshot

def as_syntax_transformer_name(t):
   return t.name

def as_syntax_transformer_literals(t):
   return t.literals

def as_syntax_transformer_ellipsis(t):
   return t.ellipsis

def as_syntax_transformer_rules(t):
   return t.rules

def as_syntax_transformer_def_env(t):
   return t.def_env

def as_environment(val):
   return val[1]

def as_record_accessor_type(val):
   return val[1]
def as_record_accessor_index(val):
   return val[2]
def as_record_accessor_name(val):
   return val[3]

def as_record_mutator_type(val):
   return val[1]
def as_record_mutator_index(val):
   return val[2]
def as_record_mutator_name(val):
   return val[3]

def as_vector_items(val):
   return val[1]

def as_bytevector_items(val):
   return val[1]


# --- Value equality ---------------------------------------------------
# eqv_atom implements R7RS eqv? for atoms: identity + symbols/booleans/nil
# + numeric same-tag-and-value.  Returns a plain Python bool so callers
# can use it in condition tests.  Primitives/equivalence.py wraps this
# into a BOOLEAN Value; the Evaluator uses it directly in `case` dispatch.

def eqv_atom(a, b):
   if a is b:
      return True
   if is_symbol(a) and is_symbol(b):
      return as_symbol(a) == as_symbol(b)
   if is_boolean(a) and is_boolean(b):
      return as_boolean(a) is as_boolean(b)
   if is_nil(a) and is_nil(b):
      return True
   if is_integer(a) and is_integer(b):
      return as_integer(a) == as_integer(b)
   if is_real(a) and is_real(b):
      return as_real(a) == as_real(b)
   if is_rational(a) and is_rational(b):
      if as_rational_num(a) != as_rational_num(b):
         return False
      return as_rational_den(a) == as_rational_den(b)
   if is_complex(a) and is_complex(b):
      if as_complex_real(a) != as_complex_real(b):
         return False
      return as_complex_imag(a) == as_complex_imag(b)
   if is_character(a) and is_character(b):
      return as_character(a) == as_character(b)
   return False


# --- Mutators ----------------------------------------------------------

def set_car(val, new_car):
   val.car = new_car

def set_cdr(val, new_cdr):
   val.cdr = new_cdr


# --- List construction helper -----------------------------------------

def list_from_items(items, src=None):
   """Build a proper list (right-folded cons chain) from a Python list
   of Values.  All intermediate cells share the same src; the parser
   sets per-cell srcs directly, so this is mainly for macro expansion
   and synthesized forms."""
   result = NIL_VALUE
   i = len(items) - 1
   while i >= 0:
      result = alloc_cons(items[i], result, src)
      i = i - 1
   return result


# --- Diagnostics -------------------------------------------------------

def format_with_caret(msg, src):
   """Render a diagnostic with filename + position + source line + caret.
   src may be None for EOF-style errors with no position."""
   if src is None:
      return msg
   if src.filename:
      prefix = '"%s" line %d, col %d: %s' % (src.filename, src.line, src.col, msg)
   else:
      prefix = 'line %d, col %d: %s' % (src.line, src.col, msg)
   if not src.source_line:
      return prefix
   caret = ' ' * max(0, src.col - 1) + '^'
   return prefix + '\n' + src.source_line + '\n' + caret


# --- Module self-test --------------------------------------------------

if __name__ == '__main__':
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

   si = SourceInfo(3, 5, 'abc', None)
   check('SourceInfo fields',  si.line == 3 and si.col == 5 and si.source_line == 'abc' and si.filename is None)

   c1 = alloc_cons(1, NIL_VALUE, si)
   check('ConsCell car',        c1.car == 1)
   check('ConsCell cdr is nil', is_nil(c1.cdr))
   check('ConsCell src',        c1.src is si)
   check('is_cons',             is_cons(c1))
   check('is_nil on cell',      not is_nil(c1))
   check('is_nil on NIL_VALUE', is_nil(NIL_VALUE))
   check('is_nil on fresh (NIL,)',     is_nil((NIL,)))
   check('is_nil on positioned nil',   is_nil((NIL, si)))

   set_car(c1, 99)
   check('set_car mutation', c1.car == 99)
   set_cdr(c1, 42)
   check('set_cdr mutation', c1.cdr == 42)

   # NIL and VOID singletons
   check('make_nil returns singleton',  make_nil() is NIL_VALUE)
   pnil = make_nil(si)
   check('make_nil(src) is positioned', is_nil(pnil) and src_of(pnil) is si)
   check('make_nil(src) not singleton', pnil is not NIL_VALUE)
   check('make_void returns singleton', make_void() is VOID_VALUE)
   check('is_void on VOID_VALUE',       is_void(VOID_VALUE))
   check('is_void on fresh (VOID,)',    is_void((VOID,)))
   check('is_void on nil',              not is_void(NIL_VALUE))
   check('is_void on cons',             not is_void(c1))

   # make_X / is_X / as_X for each atom
   b = make_boolean(True, si)
   check('is_boolean',   is_boolean(b))
   check('as_boolean',   as_boolean(b) is True)
   check('src_of bool',  src_of(b) is si)
   check('not is_int on bool', not is_integer(b))

   n = make_integer(42, si)
   check('is_integer',   is_integer(n))
   check('as_integer',   as_integer(n) == 42)
   check('src_of int',   src_of(n) is si)

   x = make_real(3.14, si)
   check('is_real',      is_real(x))
   check('as_real',      as_real(x) == 3.14)
   check('src_of real',  src_of(x) is si)

   r = make_rational(3, 4, si)
   check('is_rational',       is_rational(r))
   check('as_rational_num',   as_rational_num(r) == 3)
   check('as_rational_den',   as_rational_den(r) == 4)
   check('src_of rational',   src_of(r) is si)

   z = make_complex(1.0, 2.0, si)
   check('is_complex',        is_complex(z))
   check('as_complex_real',   as_complex_real(z) == 1.0)
   check('as_complex_imag',   as_complex_imag(z) == 2.0)
   check('src_of complex',    src_of(z) is si)

   ch = make_character('a', si)
   check('is_character',      is_character(ch))
   check('as_character',      as_character(ch) == 'a')
   check('src_of character',  src_of(ch) is si)

   s = make_string('hi', si)
   check('is_string',         is_string(s))
   check('as_string',         as_string(s) == 'hi')
   check('src_of string',     src_of(s) is si)

   sym = make_symbol('x', None)
   check('is_symbol',        is_symbol(sym))
   check('as_symbol',        as_symbol(sym) == 'x')
   check('symbol not cons',  not is_cons(sym))
   check('src_of sym None',  src_of(sym) is None)

   sym2 = make_symbol('y', si)
   check('src_of sym with src', src_of(sym2) is si)

   # Closure
   cls = make_closure(('x',), NIL_VALUE, None, None, 'doc')
   check('is_closure',              is_closure(cls))
   check('as_closure_params',       as_closure_params(cls) == ('x',))
   check('as_closure_body',         is_nil(as_closure_body(cls)))
   check('as_closure_env',          as_closure_env(cls) is None)
   check('as_closure_rest_name',    as_closure_rest_name(cls) is None)
   check('as_closure_docstring',    as_closure_docstring(cls) == 'doc')

   # Primitive
   def dummy_fn():
      return None
   p = make_primitive('prim', dummy_fn)
   check('is_primitive',     is_primitive(p))
   check('as_primitive_name', as_primitive_name(p) == 'prim')
   check('as_primitive_fn',   as_primitive_fn(p) is dummy_fn)

   # Case closure
   case_cls = make_case_closure([(('x',), NIL_VALUE, None)], None, 'doc')
   check('is_case_closure',          is_case_closure(case_cls))
   check('as_case_closure_clauses',  len(as_case_closure_clauses(case_cls)) == 1)
   check('as_case_closure_env',      as_case_closure_env(case_cls) is None)
   check('as_case_closure_docstring', as_case_closure_docstring(case_cls) == 'doc')

   # Continuation
   ksnap = [('FRAME_TEST', 1), ('FRAME_TEST', 2)]
   wsnap = [('before', 'after')]
   hsnap = ['handler1', 'handler2']
   cont = make_continuation(ksnap, wsnap, hsnap)
   check('is_continuation',             is_continuation(cont))
   check('continuation k_snapshot',     as_continuation_k(cont) is ksnap)
   check('continuation wind_snapshot',  as_continuation_wind(cont) is wsnap)
   check('continuation handler_snapshot', as_continuation_handlers(cont) is hsnap)
   check('continuation not closure',    not is_closure(cont))
   check('continuation not promise',    not is_promise(cont))

   # Environment value (wraps an opaque env object)
   env_obj = object()
   ev = make_environment(env_obj)
   check('is_environment',           is_environment(ev))
   check('as_environment',           as_environment(ev) is env_obj)
   check('environment not closure',  not is_closure(ev))
   check('environment not record',   not is_record(ev))
   check('src_of environment',       src_of(ev) is None)

   # SyntaxTransformer
   st = make_syntax_transformer('swap!', ['set!'], '...', [('pat', 'tmpl')], {})
   check('is_syntax_transformer',           is_syntax_transformer(st))
   check('syntax_transformer name',         as_syntax_transformer_name(st) == 'swap!')
   check('syntax_transformer literals',     as_syntax_transformer_literals(st) == ['set!'])
   check('syntax_transformer ellipsis',     as_syntax_transformer_ellipsis(st) == '...')
   check('syntax_transformer rules len',    len(as_syntax_transformer_rules(st)) == 1)
   check('syntax_transformer def_env',      as_syntax_transformer_def_env(st) == {})
   check('syntax_transformer not cont',     not is_continuation(st))

   # ErrorObject
   eo = make_error_object('oops', [make_integer(1), make_integer(2)])
   check('is_error_object',         is_error_object(eo))
   check('as_error_object_message', as_error_object_message(eo) == 'oops')
   check('as_error_object_irritants len', len(as_error_object_irritants(eo)) == 2)
   check('error_object not record', not is_record(eo))
   check('error_object not promise', not is_promise(eo))

   # Parameter
   pm = make_parameter(42, None)
   check('is_parameter',             is_parameter(pm))
   check('parameter value',          as_parameter_value(pm) == 42)
   check('parameter converter None', as_parameter_converter(pm) is None)
   set_parameter_value(pm, 99)
   check('parameter value mutated',  as_parameter_value(pm) == 99)
   check('parameter not closure',    not is_closure(pm))
   check('parameter not cons',       not is_cons(pm))

   pm2 = make_parameter(10, cls)
   check('parameter with converter', as_parameter_converter(pm2) is cls)

   # Record
   rt = make_record_type('point', ['x', 'y'])
   check('is_record_type',          is_record_type(rt))
   check('record_type name',        as_record_type_name(rt) == 'point')
   check('record_type field_names', as_record_type_field_names(rt) == ['x', 'y'])

   rec = make_record(rt, [10, 20])
   check('is_record',              is_record(rec))
   check('as_record_type',         as_record_type(rec) is rt)
   check('as_record_fields',       as_record_fields(rec) == [10, 20])
   check('record not cons',        not is_cons(rec))
   check('record not case_closure', not is_case_closure(rec))
   # fields list is mutable in place
   as_record_fields(rec)[1] = 99
   check('record field mutation',  as_record_fields(rec)[1] == 99)
   # identity: two records of same type share the same descriptor
   rec2 = make_record(rt, [1, 2])
   check('record type identity',   as_record_type(rec2) is as_record_type(rec))

   # Record accessor / mutator
   ra = make_record_accessor(rt, 0, 'point-x')
   check('is_record_accessor',         is_record_accessor(ra))
   check('accessor type',              as_record_accessor_type(ra) is rt)
   check('accessor index',             as_record_accessor_index(ra) == 0)
   check('accessor name',              as_record_accessor_name(ra) == 'point-x')
   check('accessor not record',        not is_record(ra))
   check('accessor not procedure',     not is_closure(ra))
   rm = make_record_mutator(rt, 1, 'point-y-set!')
   check('is_record_mutator',          is_record_mutator(rm))
   check('mutator type',               as_record_mutator_type(rm) is rt)
   check('mutator index',              as_record_mutator_index(rm) == 1)
   check('mutator name',               as_record_mutator_name(rm) == 'point-y-set!')
   check('mutator not accessor',       not is_record_accessor(rm))

   # MULTI_VALUES
   mv0 = make_multi_values([])
   check('is_multi_values 0',          is_multi_values(mv0))
   check('as_multi_values_list 0',     as_multi_values_list(mv0) == [])
   check('src_of MULTI_VALUES None',   src_of(mv0) is None)
   mv3 = make_multi_values(
      [make_integer(1), make_integer(2), make_integer(3)], si)
   check('is_multi_values 3',          is_multi_values(mv3))
   check('as_multi_values_list len',   len(as_multi_values_list(mv3)) == 3)
   check('src_of MULTI_VALUES with src', src_of(mv3) is si)
   check('multi_values not promise',   not is_promise(mv3))

   # Promise
   lazy_p = make_promise_lazy(cls)
   check('is_promise lazy',          is_promise(lazy_p))
   check('lazy not done',            as_promise_is_done(lazy_p) is False)
   check('lazy payload is thunk',    as_promise_payload(lazy_p) is cls)
   check('promise not closure',      not is_closure(lazy_p))
   check('promise not cons',         not is_cons(lazy_p))

   done_p = make_promise_done(42)
   check('is_promise done',          is_promise(done_p))
   check('done is done',             as_promise_is_done(done_p) is True)
   check('done payload is value',    as_promise_payload(done_p) == 42)

   promise_resolve(lazy_p, 99)
   check('resolve sets done',        as_promise_is_done(lazy_p) is True)
   check('resolve sets payload',     as_promise_payload(lazy_p) == 99)

   p2 = make_promise_lazy(cls)
   p3 = make_promise_done('inner')
   promise_become(p2, p3)
   check('become copies is_done',    as_promise_is_done(p2) is True)
   check('become copies payload',    as_promise_payload(p2) == 'inner')

   # src_of on cons / nil / void / closure / primitive
   check('src_of cons',     src_of(c1) is si)
   check('src_of nil',      src_of(NIL_VALUE) is None)
   check('src_of void',     src_of(VOID_VALUE) is None)
   check('src_of closure',  src_of(cls) is None)
   check('src_of primitive', src_of(p) is None)
   check('src_of promise',  src_of(done_p) is None)
   check('src_of record',   src_of(rec) is None)
   check('src_of record_type', src_of(rt) is None)
   check('src_of parameter', src_of(pm) is None)
   check('src_of error_object', src_of(eo) is None)
   check('src_of continuation', src_of(cont) is None)
   check('src_of syntax_transformer', src_of(st) is None)

   # list_from_items
   L = list_from_items([10, 20, 30])
   check('list structure', is_cons(L) and is_cons(cdr(L)) and is_cons(cdr(cdr(L))) and is_nil(cdr(cdr(cdr(L)))))
   check('list elements',  car(L) == 10 and car(cdr(L)) == 20 and car(cdr(cdr(L))) == 30)

   # format_with_caret
   out = format_with_caret('bad thing', SourceInfo(1, 3, 'hello', REPL_FILENAME))
   check('3-line render with filename',    out == '"<repl>" line 1, col 3: bad thing\nhello\n  ^')
   out = format_with_caret('bad thing', SourceInfo(1, 3, 'hello', None))
   check('3-line render no filename',      out == 'line 1, col 3: bad thing\nhello\n  ^')
   out = format_with_caret('bad', SourceInfo(2, 5, '', REPL_FILENAME))
   check('1-line fallback with filename',  out == '"<repl>" line 2, col 5: bad')
   out = format_with_caret('bad', SourceInfo(2, 5, '', None))
   check('1-line fallback no filename',    out == 'line 2, col 5: bad')
   out = format_with_caret('bare', None)
   check('no-src render', out == 'bare')

   print()
   print('%d passed, %d failed' % (n_pass, n_fail))
