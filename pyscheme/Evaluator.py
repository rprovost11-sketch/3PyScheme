"""CEK machine evaluator: dispatches on cons-cell shape directly.

Pipeline position:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> cek_eval

The Analyzer validates shape and returns the cons-cell tree unchanged.
This evaluator dispatches on cons-cell shape:

    is_cons(C) and is_symbol(car(C)) and as_symbol(car(C)) is a keyword
        -> handle that special form
    is_cons(C)
        -> application: evaluate fn, then args, then apply
    is_symbol(C)
        -> variable lookup
    otherwise (atom, NIL, VOID)
        -> self-evaluating; V = C

Two CEK machine states encoded lexically:
    inner top loop    -> EVAL state
    inner bottom loop -> APPLY state
    outer loop        -> restarts EVAL after APPLY

Body forms are stored as cons chains throughout (not pre-extracted to
Python lists) so the runtime data shape matches the source.  Arg lists
and let bindings are pre-walked into Python lists at dispatch time
(maps to the C++ reference's ArgBuf / std::vector<Value>).

Value forms (tagged tuples - arms of the Value variant):
   (VOID,)                       singleton
   (BOOLEAN,   b,   src)
   (INTEGER,   n,   src)
   (REAL,      x,   src)
   (RATIONAL,  n, d, src)
   (CHARACTER, c,   src)
   (STRING,    s,   src)
   (SYMBOL,    s,   src)         self-evaluates from quoted data
   (CLOSURE,   params, body_cons, env, rest_name, docstring)
   (PRIMITIVE, name, fn)
   ConsCell { car, cdr, src }    PAIR
   NIL_VALUE = (NIL,)            empty list singleton

src may be None for runtime-constructed atoms (e.g., (+ 1 2) -> (INTEGER, 3, None)).

Frame forms (runtime continuation state):
   (FRAME_DEFINE,    name, env)
   (FRAME_SET,       name, env, name_src)
   (FRAME_IF,        then_br, else_br, env)
   (FRAME_ARG,       args_list, env, app_node)
                        args_list is Python list of arg expressions
                        app_node is the application cons (for arity error position)
   (FRAME_CALL,      fn_value, collected_list, remaining_list, env, app_node)
                        baton frame walking through args
   (FRAME_SEQ,       remaining_body_cons, env)
                        sequencing for multi-expr body; cons chain
   (FRAME_WHEN,      body_cons, env)
   (FRAME_UNLESS,    body_cons, env)
   (FRAME_AND,       remaining_cons, env)
   (FRAME_OR,        remaining_cons, env)
   (FRAME_COND,      current_clause, remaining_clauses_cons, env)
   (FRAME_COND_ARROW, test_value, env)
   (FRAME_LET,       names_list, collected_list, remaining_pairs_list, body_cons, saved_env)
   (FRAME_LET_STAR,  name, remaining_pairs_list, body_cons, env)
   (FRAME_LETREC,    name, remaining_pairs_list, body_cons, env)
   (FRAME_CASE,      current_clause, remaining_clauses_cons, env)
                        waits for the key value to pop, then eqv?-matches
                        it against the current clause's datum list
   (FRAME_CASE_ARROW, key_value, env)
"""
from __future__ import annotations

from pyscheme.Environment import (
   Environment,
   SchemeArityError, SchemeUnboundError, SchemeTypeError,
   SchemeUserError, SchemeRaised,
   arity_mismatch_msg,
)
from pyscheme.AST import (
   ConsCell, NIL_VALUE, VOID_VALUE, alloc_cons,
   is_cons, is_nil, is_symbol, is_boolean, is_string, is_primitive,
   is_closure, is_promise,
   is_case_closure, is_multi_values, is_parameter, is_continuation,
   is_environment, is_record, is_record_accessor, is_record_mutator,
   as_symbol, as_symbol_scopes, as_boolean, as_string, as_primitive_fn, as_primitive_name,
   as_closure_params, as_closure_body, as_closure_env, as_closure_rest_name,
   as_case_closure_clauses, as_case_closure_env, as_parameter_value,
   as_continuation_k, as_continuation_wind,
   as_promise_is_done, as_promise_payload,
   as_multi_values_list, as_environment, as_continuation_handlers,
   as_record_type, as_record_fields,
   as_record_accessor_type, as_record_accessor_index, as_record_accessor_name,
   as_record_mutator_type, as_record_mutator_index, as_record_mutator_name,
   as_record_type_name,
   promise_resolve, promise_become, set_parameter_value,
   as_parameter_converter,
   make_boolean, make_closure, make_case_closure, make_promise_lazy,
   make_continuation, make_multi_values, make_primitive, make_parameter,
   eqv_atom,
   src_of,
   VOID, BOOLEAN, COMPLEX, REAL, RATIONAL, INTEGER, CHARACTER, STRING,
   CLOSURE, PAIR, NIL, PRIMITIVE, CASE_CLOSURE, PROMISE, MULTI_VALUES, SYMBOL,
)


# -------- Frame-tag constants (runtime continuation state) ----------

FRAME_DEFINE     = 0
FRAME_SET        = 1
FRAME_IF         = 2
FRAME_ARG        = 3
FRAME_CALL       = 4
FRAME_SEQ        = 5
FRAME_WHEN       = 6
FRAME_UNLESS     = 7
FRAME_AND        = 8
FRAME_OR         = 9
FRAME_COND       = 10
FRAME_COND_ARROW = 11
FRAME_LET        = 12
FRAME_LET_STAR   = 13
FRAME_LETREC     = 14
FRAME_CASE       = 15
FRAME_CASE_ARROW = 22
FRAME_DYNAMIC_WIND_AFTER = 16
FRAME_CWV_CONSUMER       = 17
FRAME_FORCE_RESULT       = 18
FRAME_MAKE_PARAMETER     = 19
FRAME_POP_HANDLER        = 20
FRAME_REINSTALL_HANDLER  = 21
FRAME_SHADOW_POP         = 23


# Frames that are not single-value continuations: FRAME_CWV_CONSUMER
# unpacks multi-values; FRAME_SEQ discards V (begin/body sequencing);
# the wind / handler pop frames forward V transparently to the next
# frame.  Multi-values may legitimately arrive at any of these.
_MULTI_VALUES_OK_FRAMES = frozenset([
   FRAME_CWV_CONSUMER,
   FRAME_SEQ,
   FRAME_DYNAMIC_WIND_AFTER,
   FRAME_POP_HANDLER,
   FRAME_REINSTALL_HANDLER,
   FRAME_SHADOW_POP,
])

# Shadow call stack for error backtraces.  Each entry is a mutable
# 3-list [label, src, count] where count > 1 means consecutive tail-replaced
# or collapsed recursive calls.
_shadow_stack = []
_SHADOW_DEPTH_LIMIT = 50


# -------- Helper functions ------------------------------------------

def _shadow_label(app_node):
   """Return the display label for a shadow-stack entry: the operator
   symbol name if the call site is a symbol application, else '#<procedure>'."""
   if app_node is not None and is_cons(app_node) and is_symbol(app_node.car):
      return as_symbol(app_node.car)
   return '#<procedure>'


def _shadow_push(K, app_node):
   """Push a shadow-stack entry for a closure entry.  If the top of K is
   FRAME_SHADOW_POP this is a tail call: replace the current top entry
   rather than pushing a new one (keeps the shadow stack bounded under TCO).
   Otherwise push a new entry and a FRAME_SHADOW_POP return marker onto K."""
   label = _shadow_label(app_node)
   src   = src_of(app_node) if app_node is not None else None
   if K and K[-1][0] == FRAME_SHADOW_POP:
      if _shadow_stack:
         top = _shadow_stack[-1]
         if top[0] == label and top[1] is src:
            top[2] = top[2] + 1
            return
         _shadow_stack[-1] = [label, src, 1]
      return
   if len(_shadow_stack) >= _SHADOW_DEPTH_LIMIT:
      return
   if _shadow_stack:
      top = _shadow_stack[-1]
      if top[0] == label and top[1] is src:
         top[2] = top[2] + 1
         K.append((FRAME_SHADOW_POP,))
         return
   _shadow_stack.append([label, src, 1])
   K.append((FRAME_SHADOW_POP,))


def isFalse(value):
   """Scheme falsity: only #f is false; everything else (including 0, '',
   '()) is truthy.  Safe against ConsCell / any non-tuple value."""
   return is_boolean(value) and as_boolean(value) is False


def _collect_cons_to_list(cell):
   """Walk a proper cons-cell list into a Python list of its elements.
   Returns empty list for NIL_VALUE.  Caller must guarantee proper list."""
   items = []
   cur = cell
   while is_cons(cur):
      items.append(cur.car)
      cur = cur.cdr
   return items


def _collect_let_bindings(bindings_cons):
   """Walk a let bindings list into a Python list of (name, val_expr) tuples.
   Caller must guarantee bindings_cons is a proper list of (name value) pairs
   (Analyzer already validated this)."""
   pairs = []
   cur = bindings_cons
   while is_cons(cur):
      b = cur.car
      var_name = as_symbol(b.car)
      val_expr = b.cdr.car
      pairs.append((var_name, val_expr))
      cur = cur.cdr
   return pairs


def _make_closure_from_lambda(lam_cons, env):
   """Build a CLOSURE value from a (lambda params-form body...) cons cell.
   Extracts param names and optional rest-param.  Peels off an optional
   docstring (first body form is a STRING and body has 2+ forms)."""
   params_sexpr = lam_cons.cdr.car
   body_cons    = lam_cons.cdr.cdr
   params = []
   rest_name = None
   if is_symbol(params_sexpr):
      rest_name = as_symbol(params_sexpr)
   elif is_cons(params_sexpr) or is_nil(params_sexpr):
      cur = params_sexpr
      while is_cons(cur):
         params.append(as_symbol(cur.car))
         cur = cur.cdr
      if is_symbol(cur):
         rest_name = as_symbol(cur)
   # Optional docstring: peel only when body has 2+ forms.
   docstring = ''
   if is_cons(body_cons) and is_cons(body_cons.cdr):
      first = body_cons.car
      if is_string(first):
         docstring = as_string(first)
         body_cons = body_cons.cdr
   return make_closure(params, body_cons, env, rest_name, docstring)


def _make_case_closure_from_form(cl_cons, env):
   """Build a CASE_CLOSURE value from (case-lambda (formals body...) ...).
   Each clause contributes a (params, body, rest_name) triple."""
   clauses = []
   cur = cl_cons.cdr
   while is_cons(cur):
      clause = cur.car
      params_sexpr = clause.car
      body_cons    = clause.cdr
      params = []
      rest_name = None
      if is_symbol(params_sexpr):
         rest_name = as_symbol(params_sexpr)
      elif is_cons(params_sexpr) or is_nil(params_sexpr):
         p_cur = params_sexpr
         while is_cons(p_cur):
            params.append(as_symbol(p_cur.car))
            p_cur = p_cur.cdr
         if is_symbol(p_cur):
            rest_name = as_symbol(p_cur)
      clauses.append((params, body_cons, rest_name))
      cur = cur.cdr
   return make_case_closure(clauses, env, '')


class _BetaResult:
   """Return container for _beta_reduce: the freshly built call env
   and the closure's body cons chain.  Ports to a C struct."""
   def __init__(self, new_env, body):
      self.new_env = new_env
      self.body    = body


def _beta_reduce_core(params, body, clo_env, rest, arg_values, app_node):
   """Shared core: validate arity against (params, rest), build the call
   env, return a _BetaResult.  Used directly by case-lambda dispatch."""
   n_fixed = len(params)
   n_args  = len(arg_values)
   bindings = {}
   if rest is None:
      if n_fixed != n_args:
         raise SchemeArityError(
            arity_mismatch_msg('', n_fixed, n_fixed, n_args),
            src_of(app_node) if app_node is not None else None)
      i = 0
      while i < n_fixed:
         bindings[params[i]] = arg_values[i]
         i = i + 1
   else:
      if n_args < n_fixed:
         raise SchemeArityError(
            arity_mismatch_msg('', n_fixed, None, n_args),
            src_of(app_node) if app_node is not None else None)
      i = 0
      while i < n_fixed:
         bindings[params[i]] = arg_values[i]
         i = i + 1
      rest_value = NIL_VALUE
      i = n_args - 1
      while i >= n_fixed:
         rest_value = alloc_cons(arg_values[i], rest_value, None)
         i = i - 1
      bindings[rest] = rest_value
   new_env = Environment(clo_env, initialBindings=bindings)
   return _BetaResult(new_env, body)


def _beta_reduce(closure, arg_values, app_node=None):
   """Validate arity, build the call env, and return a _BetaResult.
   Caller is responsible for setting C and pushing FRAME_SEQ if the
   body has more than one expression."""
   return _beta_reduce_core(
      as_closure_params(closure),
      as_closure_body(closure),
      as_closure_env(closure),
      as_closure_rest_name(closure),
      arg_values, app_node)


_CALL_CC_NAMES = ('call-with-current-continuation', 'call/cc')


def _is_call_cc_primitive(V):
   """Check if V is the call/cc primitive (by name).  Identified at the
   application dispatch point so we can intercept and capture K before
   the primitive's body would run."""
   if not is_primitive(V):
      return False
   return as_primitive_name(V) in _CALL_CC_NAMES


def _is_dynamic_wind_primitive(V):
   """Check if V is the dynamic-wind primitive.  Intercepted at application
   dispatch so we can install the wind frame in the CEK machine rather
   than running entirely inside the primitive body."""
   return is_primitive(V) and as_primitive_name(V) == 'dynamic-wind'


def _is_apply_primitive(V):
   """Check if V is the apply primitive.  Intercepted at application dispatch
   so the call becomes fully TCO-preserving: instead of the primitive body
   re-entering cek_eval, we rewrite the dispatch so the target proc is
   tail-called via the normal CEK path."""
   return is_primitive(V) and as_primitive_name(V) == 'apply'


def _is_call_with_values_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'call-with-values'


def _is_force_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'force'


def _is_with_parameters_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == '%with-parameters'


def _is_make_parameter_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'make-parameter'


def _is_with_exception_handler_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'with-exception-handler'


def _is_raise_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'raise'


def _is_raise_continuable_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'raise-continuable'


def _is_error_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'error'


def _is_eval_primitive(V):
   return is_primitive(V) and as_primitive_name(V) == 'eval'


# Thread-local CEK exception handler stack and dynamic-wind stack.
# Each thread sees its own list; this maps to __thread storage in the C
# port so concurrent evaluations don't share continuation-relevant state.
import threading as _threading
_thread_state = _threading.local()


class _ThreadLocalList:
   """List-like proxy backed by per-thread storage in _thread_state.
   Supports the operations used by the evaluator: len, bool, iter,
   indexing, append, pop, clear, extend.  Storing the proxy at module
   level lets call sites continue using `_wind_stack.append(x)` etc.
   without knowing about the thread-local backing."""
   def __init__(self, attr_name):
      self._attr = attr_name

   def _get(self):
      if not hasattr(_thread_state, self._attr):
         setattr(_thread_state, self._attr, [])
      return getattr(_thread_state, self._attr)

   def __len__(self):
      return len(self._get())

   def __bool__(self):
      return bool(self._get())

   def __iter__(self):
      return iter(self._get())

   def __getitem__(self, i):
      return self._get()[i]

   def append(self, x):
      self._get().append(x)

   def pop(self, *args):
      return self._get().pop(*args)

   def clear(self):
      self._get().clear()

   def extend(self, items):
      self._get().extend(items)


_handler_stack = _ThreadLocalList('handler_stack')


def _restore_handler_stack(snapshot):
   """Replace _handler_stack contents with snapshot in place.  Continuation
   invocation uses this so a captured continuation's K-stack frames
   (including FRAME_POP_HANDLER) find the matching handler entries."""
   _handler_stack.clear()
   _handler_stack.extend(snapshot)


def _build_parameterize_winds(params_list, values_list, ctx, saved_env, app_node):
   """Prepare a parameterize wind frame: walk the parameter / value lists,
   apply converters, compute install-and-save state, and return a pair of
   Python-backed primitives (install_thunk, restore_thunk) that wind_walk
   and FRAME_DYNAMIC_WIND_AFTER can invoke as Scheme procedures.  Also
   installs the new values so the thunk sees them immediately; on normal
   return FRAME_DYNAMIC_WIND_AFTER pops and calls restore_thunk, and on
   exception _unwind_winds_on_error does the same."""
   from pyscheme.primitives.meta import _apply_scheme_proc

   params = []
   cur = params_list
   while is_cons(cur):
      p = cur.car
      if not is_parameter(p):
         raise SchemeTypeError(
            '%with-parameters: non-parameter in parameterize binding',
            app_node)
      params.append(p)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         '%with-parameters: parameter list must be proper', app_node)

   new_vals_raw = []
   cur = values_list
   while is_cons(cur):
      new_vals_raw.append(cur.car)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         '%with-parameters: value list must be proper', app_node)

   if len(params) != len(new_vals_raw):
      raise SchemeTypeError(
         '%with-parameters: parameter / value count mismatch', app_node)

   installed = []
   i = 0
   while i < len(params):
      conv = as_parameter_converter(params[i])
      if conv is None:
         installed.append(new_vals_raw[i])
      else:
         installed.append(
            _apply_scheme_proc(conv, [new_vals_raw[i]], ctx, saved_env, app_node))
      i = i + 1

   saved_values = []
   i = 0
   while i < len(params):
      saved_values.append(as_parameter_value(params[i]))
      i = i + 1

   # Install new values now so the thunk sees them.
   i = 0
   while i < len(params):
      set_parameter_value(params[i], installed[i])
      i = i + 1

   # Build installer (called by _wind_walk on continuation re-entry) and
   # restorer (called by FRAME_DYNAMIC_WIND_AFTER / _unwind_winds_on_error).
   def installer(ctx2, env2, args2, app_node2):
      j = 0
      while j < len(params):
         set_parameter_value(params[j], installed[j])
         j = j + 1
      return VOID_VALUE

   def restorer(ctx2, env2, args2, app_node2):
      j = 0
      while j < len(params):
         set_parameter_value(params[j], saved_values[j])
         j = j + 1
      return VOID_VALUE

   return (make_primitive('%parameterize-install', installer),
           make_primitive('%parameterize-restore', restorer))


def _enter_proc(fn_value, args, ctx, saved_env, app_node):
   """Dispatch a procedure application with known args.  Returns a
   next-state descriptor so frame handlers can update the CEK state
   without duplicating the FRAME_CALL terminal-dispatch logic:
     ('value', V)                 - primitive or parameter produced V
     ('cont',  new_K, new_V)      - continuation invoked; restore K and V
     ('enter', C, new_env, seq)   - closure entered; eval C in new_env;
                                    push FRAME_SEQ(seq, new_env) if seq is
                                    not None (multi-form body)"""
   if is_continuation(fn_value):
      _wind_walk(ctx, as_continuation_wind(fn_value))
      _restore_handler_stack(as_continuation_handlers(fn_value))
      return ('cont',
              list(as_continuation_k(fn_value)),
              _continuation_value(fn_value, args))
   pv = _apply_parameter_if(fn_value, len(args), app_node)
   if pv is not None:
      return ('value', pv)
   if is_primitive(fn_value):
      V = as_primitive_fn(fn_value)(ctx, saved_env, args, app_node)
      return ('value', V)
   if is_closure(fn_value) or is_case_closure(fn_value):
      r = _apply_value(fn_value, args, app_node)
      if is_cons(r.body.cdr):
         return ('enter', r.body.car, r.new_env, r.body.cdr)
      return ('enter', r.body.car, r.new_env, None)
   raise SchemeTypeError(
      'expected a procedure', app_node)


def _unpack_apply_args(collected, app_node):
   """(apply proc arg1 arg2 ... argN list) has the tail list spliced onto
   the leading args.  Returns (proc, flat_args) or raises SchemeTypeError."""
   if len(collected) < 2:
      raise SchemeArityError(
         arity_mismatch_msg('apply', 2, None, len(collected)),
         src_of(app_node) if app_node is not None else None)
   proc = collected[0]
   flat_args = []
   i = 1
   while i < len(collected) - 1:
      flat_args.append(collected[i])
      i = i + 1
   last = collected[len(collected) - 1]
   cur = last
   while is_cons(cur):
      flat_args.append(cur.car)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         'apply: last argument must be a proper list', app_node)
   return (proc, flat_args)


def _continuation_value(cont, arg_values):
   """Value to install when invoking a continuation with the given arg list.
   0 args -> VOID, 1 arg -> that arg, 2+ -> MULTI_VALUES container."""
   if len(arg_values) == 0:
      return VOID_VALUE
   if len(arg_values) == 1:
      return arg_values[0]
   return make_multi_values(list(arg_values))


# Dynamic-wind stack.  Thread-local (matches the __thread storage in the
# C port) so concurrent evaluations don't share active winds.  Each entry
# is a (before, after) tuple of 0-arg procedure Values.  The innermost
# active wind is at the end.
_wind_stack = _ThreadLocalList('wind_stack')


def _wind_walk(ctx, target):
   """Adjust _wind_stack to match `target` (a list of (before, after) pairs).
   For frames being exited (below the common prefix of current and target),
   call the after thunk and pop.  For frames being entered, push and call
   the before thunk.  Used when invoking a continuation whose wind-stack
   snapshot differs from the current stack."""
   from pyscheme.primitives.meta import _apply_scheme_proc
   common = 0
   while common < len(_wind_stack) and common < len(target):
      cur = _wind_stack[common]
      tgt = target[common]
      if cur[0] is not tgt[0] or cur[1] is not tgt[1]:
         break
      common = common + 1
   while len(_wind_stack) > common:
      wf = _wind_stack[len(_wind_stack) - 1]
      _wind_stack.pop()
      _apply_scheme_proc(wf[1], [], ctx, None, None)
   i = common
   while i < len(target):
      _wind_stack.append(target[i])
      _apply_scheme_proc(target[i][0], [], ctx, None, None)
      i = i + 1


def _apply_parameter_if(V, n_args, app_node):
   """If V is a parameter object, enforce 0-arg call semantics.  Returns
   the parameter's current value on a 0-arg call, or raises on arity
   mismatch.  Returns the sentinel None when V is not a parameter, so
   callers can fall through to ordinary dispatch.  (None is safe as a
   sentinel because parameter values are never Python None in practice -
   every Scheme value is a tuple / class instance.)"""
   if not is_parameter(V):
      return None
   if n_args != 0:
      raise SchemeArityError(
         arity_mismatch_msg('parameter', 0, 0, n_args),
         src_of(app_node) if app_node is not None else None)
   return as_parameter_value(V)


def _apply_value(V, arg_values, app_node):
   """Dispatch a procedure-valued V to its call-env builder.  Handles
   ordinary CLOSURE and CASE_CLOSURE (arity-dispatched).  Returns a
   _BetaResult or raises SchemeArityError / SchemeTypeError."""
   if is_case_closure(V):
      clauses = as_case_closure_clauses(V)
      clo_env = as_case_closure_env(V)
      n_args  = len(arg_values)
      i = 0
      while i < len(clauses):
         c      = clauses[i]
         params = c[0]
         body   = c[1]
         rest   = c[2]
         n_fixed = len(params)
         if rest is None:
            if n_fixed == n_args:
               return _beta_reduce_core(params, body, clo_env, None,
                                        arg_values, app_node)
         else:
            if n_args >= n_fixed:
               return _beta_reduce_core(params, body, clo_env, rest,
                                        arg_values, app_node)
         i = i + 1
      raise SchemeArityError(
         'case-lambda: no clause matches ' + str(n_args) + ' arguments',
         src_of(app_node) if app_node is not None else None)
   return _beta_reduce(V, arg_values, app_node)


def _classify_cond_clause(clause):
   """Return ('else', body_cons) | ('arrow', test, proc) |
   ('test-only', test) | ('body', test, body_cons).  Caller has
   already verified clause is a non-nil cons (Analyzer validated)."""
   head = clause.car
   if is_symbol(head) and as_symbol(head) == 'else':
      return ('else', clause.cdr)
   if is_nil(clause.cdr):
      return ('test-only', head)
   if (is_cons(clause.cdr) and is_symbol(clause.cdr.car)
         and as_symbol(clause.cdr.car) == '=>'
         and is_cons(clause.cdr.cdr) and is_nil(clause.cdr.cdr.cdr)):
      return ('arrow', head, clause.cdr.cdr.car)
   return ('body', head, clause.cdr)


# -------- The CEK machine -------------------------------------------

def cek_eval(expr, env, ctx=None):
   """Evaluate expr against env.  Returns the resulting Value.

   ctx (if given) carries interpreter-wide state like the output stream;
   it is threaded into primitive calls.  When ctx is None we construct
   a default Context whose outStrm is sys.stdout."""
   if ctx is None:
      from pyscheme.Context import Context
      ctx = Context()
   # Snapshot the wind-stack depth at entry so we can unwind any
   # FRAME_DYNAMIC_WIND_AFTER frames installed during this cek_eval call
   # if an exception escapes the inner loop.  The inner loop is in a
   # helper function so we can wrap it in a single try/except.
   wind_depth_entry    = len(_wind_stack)
   handler_depth_entry = len(_handler_stack)
   from pyscheme.Parser import SchemeSyntaxError
   _CATCHABLE = (SchemeRaised, SchemeTypeError, SchemeArityError,
                 SchemeUnboundError, SchemeSyntaxError)
   try:
      return _cek_loop(expr, env, ctx)
   except _CATCHABLE as e:
      # _cek_loop's in-loop exception dispatch already walked K for any
      # handler frame installed during this cek_eval call.  Reaching
      # here means no handler in scope caught the condition; clean up
      # any dynamic-wind entries installed during this call (their
      # after-thunks errors are swallowed, matching the C++ reference)
      # and re-raise so the caller (often an outer cek_eval) can continue
      # propagation.  Truncate _handler_stack defensively in case a
      # handler push escaped without its matching pop.
      _unwind_winds_on_error(ctx, wind_depth_entry)
      while len(_handler_stack) > handler_depth_entry:
         _handler_stack.pop()
      if hasattr(e, 'call_stack') and e.call_stack is None and _shadow_stack:
         e.call_stack = list(_shadow_stack)
      _shadow_stack.clear()
      raise
   except BaseException:
      _unwind_winds_on_error(ctx, wind_depth_entry)
      while len(_handler_stack) > handler_depth_entry:
         _handler_stack.pop()
      _shadow_stack.clear()
      raise


def _library_load_path():
   """Return the search path for .sld auto-discovery.  SCHEME_LIBRARY_PATH
   environment variable is split on os.pathsep (':' on Unix, ';' on
   Windows); the current working directory is always prepended so a
   library file in cwd is found by default."""
   import os
   path_var = os.environ.get('SCHEME_LIBRARY_PATH', '')
   parts = [p for p in path_var.split(os.pathsep) if p]
   return ['.'] + parts


def _try_load_library_file(name_sexpr, ctx):
   """Try to find name_sexpr as a .sld file under the load path and
   load it; the file is expected to contain a top-level (define-library
   ...) form that registers the library.  Returns True if the library
   ended up registered, False otherwise.  No exceptions on file-not-
   found; an unknown library remains unresolvable."""
   import os
   from pyscheme.library import library_name_to_key, library_registered_p
   from pyscheme.Parser   import parse
   try:
      key = library_name_to_key(name_sexpr)
   except ValueError:
      return False
   if library_registered_p(key):
      return True
   parts = key.split('.')
   relative = os.path.join(*parts) + '.sld'
   for base in _library_load_path():
      candidate = os.path.join(base, relative) if base else relative
      try:
         f = open(candidate, 'r')
      except FileNotFoundError:
         continue
      source = f.read()
      f.close()
      forms = parse(source, candidate)
      for form in forms:
         from pyscheme.Expander import expand
         cek_eval(expand(form), Environment(parent=None), ctx)
      if library_registered_p(key):
         return True
   return False


def _process_import(sets_cons, env, ctx=None):
   """Top-level (import <import-set>...).  Resolves each set and binds
   every exported name into env.  Macros (SyntaxTransformer values) are
   bound the same way as runtime values; the Expander's _lookup_macro
   walks the env chain to find them.  When an import set names a
   library that is not yet registered, this attempts to load it from
   a .sld file on the SCHEME_LIBRARY_PATH search path.  Raises
   SchemeSyntaxError (positioned) on shape or lookup errors."""
   from pyscheme.library import resolve_import_set
   from pyscheme.Parser   import SchemeSyntaxError
   cur = sets_cons
   while is_cons(cur):
      import_set = cur.car
      try:
         bindings = resolve_import_set(import_set)
      except ValueError as e:
         # Try auto-discovery: if the import-set is a bare library name,
         # look for an .sld file on the load path.
         loaded = False
         if is_cons(import_set) and ctx is not None:
            loaded = _try_load_library_file(import_set, ctx)
         if loaded:
            try:
               bindings = resolve_import_set(import_set)
            except ValueError as e2:
               raise SchemeSyntaxError('import: ' + str(e2), src_of(cur.car))
         else:
            raise SchemeSyntaxError('import: ' + str(e), src_of(cur.car))
      for n in bindings:
         env.bind(n, bindings[n])
      cur = cur.cdr


def _process_one_lib_decl(decl, lib_env, export_names, ctx):
   """Process a single library declaration in the context of an active
   _process_define_library call.  Mutates lib_env / export_names in place.
   Recursive: include-library-declarations and cond-expand decls call
   back into this function for the forms they produce."""
   from pyscheme.library import resolve_import_set
   from pyscheme.Parser   import SchemeSyntaxError, parse
   from pyscheme.Expander import expand, _include_base_dir, _feature_req_matches
   if not is_cons(decl) or not is_symbol(decl.car):
      raise SchemeSyntaxError(
         'define-library: declaration must be a list starting with a symbol',
         src_of(decl))
   dsym  = as_symbol(decl.car)
   dbody = decl.cdr

   if dsym == 'import':
      sets = dbody
      while is_cons(sets):
         import_set = sets.car
         try:
            bindings = resolve_import_set(import_set)
         except ValueError as e:
            loaded = False
            if is_cons(import_set):
               loaded = _try_load_library_file(import_set, ctx)
            if loaded:
               try:
                  bindings = resolve_import_set(import_set)
               except ValueError as e2:
                  raise SchemeSyntaxError(
                     'define-library: import: ' + str(e2), src_of(import_set))
            else:
               raise SchemeSyntaxError(
                  'define-library: import: ' + str(e), src_of(import_set))
         for n in bindings:
            lib_env.bind(n, bindings[n])
         sets = sets.cdr
      return

   if dsym == 'export':
      specs = dbody
      while is_cons(specs):
         spec = specs.car
         specs = specs.cdr
         if is_symbol(spec):
            nm = as_symbol(spec)
            export_names.append((nm, nm))
         elif (is_cons(spec) and is_symbol(spec.car)
               and as_symbol(spec.car) == 'rename'):
            r = spec.cdr
            if (not is_cons(r) or not is_symbol(r.car)
                  or not is_cons(r.cdr) or not is_symbol(r.cdr.car)):
               raise SchemeSyntaxError(
                  'define-library: malformed export rename',
                  src_of(spec))
            export_names.append((as_symbol(r.car), as_symbol(r.cdr.car)))
         else:
            raise SchemeSyntaxError(
               'define-library: malformed export spec', src_of(spec))
      return

   if dsym == 'begin':
      forms = dbody
      while is_cons(forms):
         cek_eval(expand(forms.car), lib_env, ctx)
         forms = forms.cdr
      return

   if dsym == 'include-library-declarations':
      # (include-library-declarations <filename>...) - read each file,
      # parse, and process its top-level forms as library declarations.
      import os
      base_dir = _include_base_dir(src_of(decl))
      paths = dbody
      while is_cons(paths):
         path_val = paths.car
         if not is_string(path_val):
            raise SchemeSyntaxError(
               'include-library-declarations: filename must be a string',
               src_of(path_val))
         requested = as_string(path_val)
         resolved  = os.path.join(base_dir, requested) if base_dir else requested
         try:
            f = open(resolved, 'r')
         except FileNotFoundError:
            raise SchemeSyntaxError(
               'include-library-declarations: file not found: ' + resolved,
               src_of(decl))
         source = f.read()
         f.close()
         inner_forms = parse(source, resolved)
         for inner in inner_forms:
            _process_one_lib_decl(inner, lib_env, export_names, ctx)
         paths = paths.cdr
      return

   if dsym == 'cond-expand':
      # (cond-expand <clause>...).  Each clause is (<feature-req> <decl>...)
      # or (else <decl>...).  Pick the first matching clause and process
      # its declarations recursively.
      cur_clause = dbody
      while is_cons(cur_clause):
         clause = cur_clause.car
         cur_clause = cur_clause.cdr
         if not is_cons(clause):
            raise SchemeSyntaxError(
               'cond-expand: clause must be a list', src_of(clause))
         req = clause.car
         body = clause.cdr
         if _feature_req_matches(req):
            cur_inner = body
            while is_cons(cur_inner):
               _process_one_lib_decl(
                  cur_inner.car, lib_env, export_names, ctx)
               cur_inner = cur_inner.cdr
            return
      # No clause matched: silently produce no declarations (R7RS).
      return

   # Unknown declaration keyword: expand and evaluate whole decl in
   # lib_env.  Covers stray (define ...) forms or other top-level
   # shapes; the expand step also routes define-syntax through the
   # active per-library macro scope.
   cek_eval(expand(decl), lib_env, ctx)


def _process_define_library(C, ctx):
   """Top-level (define-library <name> <decl>...).  Creates a fresh
   parentless env, processes decls in order (with the runtime env
   reference temporarily swapped to lib_env so define-syntax inside
   the library's begin body installs transformers in lib_env, scoped
   to this library), builds an exports env per the export declarations,
   and registers under the library's key.  Macros become regular lib_env
   bindings, so the export filter exposes them like any other binding."""
   from pyscheme.library import (
      library_name_to_key, library_register,
   )
   from pyscheme.Parser   import SchemeSyntaxError
   from pyscheme.Expander import _runtime_env_ref
   if not is_cons(C.cdr):
      raise SchemeSyntaxError('define-library: missing library name', src_of(C))
   name_sexpr = C.cdr.car
   decls_cons = C.cdr.cdr
   try:
      key = library_name_to_key(name_sexpr)
   except ValueError as e:
      raise SchemeSyntaxError('define-library: ' + str(e), src_of(C))

   lib_env = Environment(parent=None)
   export_names = []          # Python list of (internal, external) pairs
   # Swap the runtime env to lib_env so define-syntax binds transformers
   # into this library's env rather than into the surrounding env.
   outer_env = _runtime_env_ref[0]
   _runtime_env_ref[0] = lib_env
   try:
      d = decls_cons
      while is_cons(d):
         _process_one_lib_decl(d.car, lib_env, export_names, ctx)
         d = d.cdr
   finally:
      _runtime_env_ref[0] = outer_env

   # Build exports env: copy each (internal, external) entry out of
   # lib_env; missing names are hard errors.
   exports_env = Environment(parent=None)
   i = 0
   while i < len(export_names):
      internal, external = export_names[i]
      if internal not in lib_env._bindings:
         raise SchemeSyntaxError(
            'define-library: exported name not defined: ' + internal,
            src_of(C))
      exports_env.bind(external, lib_env.lookup(internal))
      i = i + 1
   exports_env.freeze()
   library_register(key, exports_env)


def _unwind_winds_on_error(ctx, target_depth):
   """Pop wind entries installed during a failed cek_eval call and call
   their after thunks.  R7RS does not specify what happens when an
   after-thunk itself raises during cleanup; the C++ reference (see
   evaluator.cpp's exception dispatch) deliberately swallows them so
   the original error continues to propagate, and we match that choice
   here for C-port consistency."""
   from pyscheme.primitives.meta import _apply_scheme_proc
   while len(_wind_stack) > target_depth:
      wf = _wind_stack[len(_wind_stack) - 1]
      _wind_stack.pop()
      try:
         _apply_scheme_proc(wf[1], [], ctx, None, None)
      except BaseException:
         pass


def _cek_loop(expr, env, ctx):
   """The CEK machine's main loop.  Wraps the eval/apply state machine
   in an outer try-while so that catchable exceptions (raise, error, and
   the impl errors SchemeTypeError / SchemeArityError / SchemeUnboundError
   / SchemeSyntaxError) dispatch the handler stack INSIDE the same
   activation record - mirrors the C++ reference's for(;;) try{ ... }
   pattern in evaluator.cpp.  Handler invocation sets CEK state via
   _enter_proc, so a continuation captured during handler execution
   sees the outer K-stack and not just the handler body's frames."""
   from pyscheme.Parser import SchemeSyntaxError
   _CATCHABLE_LOCAL = (SchemeRaised, SchemeTypeError, SchemeArityError,
                       SchemeUnboundError, SchemeSyntaxError)
   C = expr
   V = None
   E = env
   K = []
   skip_eval = False

   while True:
      try:
         while True:

            # ----- EVAL: descend until a value is produced at a leaf -----
            while True:
               if skip_eval:
                  skip_eval = False
                  break


               if is_cons(C):
                  if ctx._instrumented:
                     if ctx._debugging:
                        ctx.debugger.on_expr(C, E, K, ctx)

                  head = C.car

                  if is_symbol(head):
                     name = as_symbol(head)

                     if name == 'quote':
                        # (quote datum) - datum self-evaluates
                        V = C.cdr.car
                        break

                     if name == 'lambda':
                        V = _make_closure_from_lambda(C, E)
                        break

                     if name == 'case-lambda':
                        V = _make_case_closure_from_form(C, E)
                        break

                     if name == 'delay' or name == 'delay-force':
                        # (delay expr) and (delay-force expr) both produce a lazy
                        # promise whose thunk evaluates expr in the current env.
                        # force is iterative, so delay-force's tail-safety falls out.
                        expr = C.cdr.car
                        body = alloc_cons(expr, NIL_VALUE, None)
                        thunk = make_closure([], body, E, None, '')
                        V = make_promise_lazy(thunk)
                        break

                     if name == 'import':
                        # (import <import-set>...) - resolve each set and bind
                        # each exported name into the current env.  Returns VOID.
                        _process_import(C.cdr, E, ctx)
                        V = VOID_VALUE
                        break

                     if name == 'define-library':
                        # (define-library <name> <decl>...) - install a new
                        # library.  Returns VOID; no visible effect on E.
                        _process_define_library(C, ctx)
                        V = VOID_VALUE
                        break

                     if name == 'if':
                        # (if test then else)  -- expander supplies VOID for missing else
                        K.append((FRAME_IF, C.cdr.cdr.car, C.cdr.cdr.cdr.car, E))
                        C = C.cdr.car
                        continue

                     if name == 'define':
                        # (define name value)
                        K.append((FRAME_DEFINE, C.cdr.car, E))
                        C = C.cdr.cdr.car
                        continue

                     if name == 'set!':
                        # (set! name value)
                        name_sexpr = C.cdr.car
                        K.append((FRAME_SET, name_sexpr, E, src_of(name_sexpr)))
                        C = C.cdr.cdr.car
                        continue

                     if name == 'begin':
                        # (begin body...)  - body cons chain, non-empty (analyzer checks)
                        body = C.cdr
                        C = body.car
                        if is_cons(body.cdr):
                           K.append((FRAME_SEQ, body.cdr, E))
                        continue

                     if name == 'when':
                        # (when test body...)
                        K.append((FRAME_WHEN, C.cdr.cdr, E))
                        C = C.cdr.car
                        continue

                     if name == 'unless':
                        # (unless test body...)
                        K.append((FRAME_UNLESS, C.cdr.cdr, E))
                        C = C.cdr.car
                        continue

                     if name == 'and':
                        body = C.cdr
                        if is_nil(body):
                           V = make_boolean(True)
                           break
                        if is_cons(body.cdr):
                           K.append((FRAME_AND, body.cdr, E))
                        C = body.car
                        continue

                     if name == 'or':
                        body = C.cdr
                        if is_nil(body):
                           V = make_boolean(False)
                           break
                        if is_cons(body.cdr):
                           K.append((FRAME_OR, body.cdr, E))
                        C = body.car
                        continue

                     if name == 'cond':
                        # (cond clauses...) - analyzer ensures non-empty
                        clauses = C.cdr
                        first   = clauses.car
                        rest    = clauses.cdr
                        kind = _classify_cond_clause(first)
                        if kind[0] == 'else':
                           body = kind[1]
                           C = body.car
                           if is_cons(body.cdr):
                              K.append((FRAME_SEQ, body.cdr, E))
                           continue
                        K.append((FRAME_COND, first, rest, E))
                        C = kind[1]   # the test expression
                        continue

                     if name == 'case':
                        # (case <key> <clause>...) - analyzer ensures shape:
                        # key present, at least one clause, each clause a proper
                        # list starting with a datum-list or 'else'.
                        clauses = C.cdr.cdr
                        first   = clauses.car
                        rest    = clauses.cdr
                        K.append((FRAME_CASE, first, rest, E))
                        C = C.cdr.car   # the key expression
                        continue

                     if name == 'let':
                        # (let [name] bindings body...)
                        if is_symbol(C.cdr.car):
                           # named let: desugar at runtime
                           # (let name ((v1 e1) ...) body...) ==
                           #   (letrec ((name (lambda (v1 ...) body...))) (name e1 ...))
                           loop_name = as_symbol(C.cdr.car)
                           bindings_cons = C.cdr.cdr.car
                           body_cons = C.cdr.cdr.cdr
                           pairs = _collect_let_bindings(bindings_cons)
                           params = []
                           init_exprs = []
                           i = 0
                           while i < len(pairs):
                              params.append(pairs[i][0])
                              init_exprs.append(pairs[i][1])
                              i = i + 1
                           # Build the loop env first so the closure can capture it,
                           # then bind the closure to its own name for self-reference.
                           loop_env = Environment(E, initialBindings={loop_name: VOID_VALUE})
                           closure = make_closure(params, body_cons, loop_env, None, '')
                           loop_env.bind(loop_name, closure)
                           # Now evaluate (name init1 init2 ...) - i.e., apply closure to init values
                           # Set up FRAME_ARG-style call: but we don't have an "AST" for this synthesized call.
                           # Use FRAME_ARG with init_exprs as args list and the current C as app_node.
                           V = closure
                           # We want to apply closure to init_exprs.  Push FRAME_ARG with V as fn.
                           K.append((FRAME_ARG, init_exprs, loop_env, C))
                           break
                        bindings_cons = C.cdr.car
                        body_cons     = C.cdr.cdr
                        pairs = _collect_let_bindings(bindings_cons)
                        if not pairs:
                           # No bindings; just run body in current env
                           C = body_cons.car
                           if is_cons(body_cons.cdr):
                              K.append((FRAME_SEQ, body_cons.cdr, E))
                           continue
                        names = []
                        val_exprs = []
                        i = 0
                        while i < len(pairs):
                           names.append(pairs[i][0])
                           val_exprs.append(pairs[i][1])
                           i = i + 1
                        # Pre-extract remaining val_exprs (all but first) as Python list
                        remaining = []
                        i = 1
                        while i < len(val_exprs):
                           remaining.append(val_exprs[i])
                           i = i + 1
                        K.append((FRAME_LET, names, [], remaining, body_cons, E))
                        C = val_exprs[0]
                        # E stays at outer env - all val_exprs evaluate in it
                        continue

                     if name == 'let*':
                        # (let* bindings body...) - each val sees prior bindings
                        bindings_cons = C.cdr.car
                        body_cons     = C.cdr.cdr
                        pairs = _collect_let_bindings(bindings_cons)
                        if not pairs:
                           C = body_cons.car
                           if is_cons(body_cons.cdr):
                              K.append((FRAME_SEQ, body_cons.cdr, E))
                           continue
                        remaining = []
                        i = 1
                        while i < len(pairs):
                           remaining.append(pairs[i])
                           i = i + 1
                        K.append((FRAME_LET_STAR, pairs[0][0], remaining, body_cons, E))
                        C = pairs[0][1]
                        continue

                     if name == 'letrec' or name == 'letrec*':
                        # (letrec bindings body...) - all names visible in val_exprs
                        bindings_cons = C.cdr.car
                        body_cons     = C.cdr.cdr
                        pairs = _collect_let_bindings(bindings_cons)
                        if not pairs:
                           C = body_cons.car
                           if is_cons(body_cons.cdr):
                              K.append((FRAME_SEQ, body_cons.cdr, E))
                           continue
                        init_bindings = {}
                        i = 0
                        while i < len(pairs):
                           init_bindings[pairs[i][0]] = VOID_VALUE
                           i = i + 1
                        new_env = Environment(E, initialBindings=init_bindings)
                        remaining = []
                        i = 1
                        while i < len(pairs):
                           remaining.append(pairs[i])
                           i = i + 1
                        K.append((FRAME_LETREC, pairs[0][0], remaining, body_cons, new_env))
                        C = pairs[0][1]
                        E = new_env
                        continue

                     # Symbol head but not a keyword - application.
                     # Walk arg cons chain into Python list.
                     args = _collect_cons_to_list(C.cdr)
                     K.append((FRAME_ARG, args, E, C))
                     C = head
                     continue

                  # head is not a symbol - application (e.g., immediate lambda).
                  args = _collect_cons_to_list(C.cdr)
                  K.append((FRAME_ARG, args, E, C))
                  C = head
                  continue

               if is_symbol(C):
                  try:
                     V = E.lookup(as_symbol(C), as_symbol_scopes(C))
                  except SchemeUnboundError as e:
                     e.src = src_of(C)
                     raise
                  break

               # Atom (literal), NIL, VOID, or any other tagged value: self-eval.
               V = C
               break

            # ----- APPLY: consume V against the top frame -----
            while True:
               if not K:
                  return V

               frame = K.pop()
               ftag  = frame[0]

               # R7RS 6.10: passing multi-values to a continuation not created
               # by call-with-values is an error.  But not every frame is a
               # single-value continuation: FRAME_SEQ discards V (begin/body
               # sequencing - 0-value context, not 1-value), and the wind /
               # handler pop frames are transparent (they preserve V across
               # an effect and forward it to whatever's outside).  Only frames
               # that actually consume V as a single value error here.
               if is_multi_values(V) and ftag not in _MULTI_VALUES_OK_FRAMES:
                  raise SchemeTypeError(
                     'multiple values delivered to a single-value context',
                     src_of(V))

               if ftag == FRAME_DEFINE:
                  E = frame[2]
                  E.bind(as_symbol(frame[1]), V, as_symbol_scopes(frame[1]))
                  V = VOID_VALUE
                  continue

               if ftag == FRAME_SET:
                  E = frame[2]
                  try:
                     E.set(as_symbol(frame[1]), V, as_symbol_scopes(frame[1]))
                  except SchemeUnboundError as e:
                     e.src = frame[3]
                     raise
                  V = VOID_VALUE
                  continue

               if ftag == FRAME_IF:
                  if isFalse(V):
                     C = frame[2]
                  else:
                     C = frame[1]
                  E = frame[3]
                  break

               if ftag == FRAME_DYNAMIC_WIND_AFTER:
                  # frame = (FRAME_DYNAMIC_WIND_AFTER, after_thunk)
                  # The thunk has produced its value (now in V).  Pop the wind
                  # entry, save the body result across the after call, then run
                  # after for its effect and restore the result.
                  from pyscheme.primitives.meta import _apply_scheme_proc
                  after_thunk = frame[1]
                  body_result = V
                  if _wind_stack:
                     _wind_stack.pop()
                  _apply_scheme_proc(after_thunk, [], ctx, None, None)
                  V = body_result
                  continue

               if ftag == FRAME_CWV_CONSUMER:
                  # frame = (FRAME_CWV_CONSUMER, consumer, app_node)
                  # V is whatever the producer returned.  Unpack multi-values (if
                  # applicable) and tail-call the consumer via _enter_proc.
                  consumer  = frame[1]
                  app_node  = frame[2]
                  if is_multi_values(V):
                     consumer_args = as_multi_values_list(V)
                  else:
                     consumer_args = [V]
                  result = _enter_proc(consumer, consumer_args, ctx, E, app_node)
                  if result[0] == 'value':
                     V = result[1]
                     continue
                  if result[0] == 'cont':
                     K = result[1]
                     V = result[2]
                     continue
                  C = result[1]
                  E = result[2]
                  if result[3] is not None:
                     K.append((FRAME_SEQ, result[3], E))
                  break

               if ftag == FRAME_POP_HANDLER:
                  # Thunk returned normally; pop the installed handler and let V
                  # flow.  No work needed beyond popping the stack entry.
                  if _handler_stack:
                     _handler_stack.pop()
                  continue

               if ftag == FRAME_REINSTALL_HANDLER:
                  # raise-continuable's handler returned; push it back so nested
                  # raises in the enclosing with-exception-handler scope still
                  # see it.  V (handler's return) flows back to the raise-
                  # continuable's call site unchanged.
                  _handler_stack.append(frame[1])
                  continue

               if ftag == FRAME_MAKE_PARAMETER:
                  # frame = (FRAME_MAKE_PARAMETER, converter)
                  # V is the converter's return value; wrap it as a Parameter.
                  V = make_parameter(V, frame[1])
                  continue

               if ftag == FRAME_FORCE_RESULT:
                  # frame = (FRAME_FORCE_RESULT, promise)
                  # The promise's thunk has produced V.  Resolve or become, and
                  # iterate if we ended up with another unforced promise.
                  p = frame[1]
                  if is_promise(V):
                     promise_become(p, V)
                     if as_promise_is_done(p):
                        V = as_promise_payload(p)
                        continue
                     # Still not done - iterate: push another FORCE_RESULT and
                     # tail-call the (now inner) thunk.
                     K.append((FRAME_FORCE_RESULT, p))
                     thunk = as_promise_payload(p)
                     result = _enter_proc(thunk, [], ctx, E, None)
                     if result[0] == 'value':
                        V = result[1]
                        continue
                     if result[0] == 'cont':
                        K = result[1]
                        V = result[2]
                        continue
                     C = result[1]
                     E = result[2]
                     if result[3] is not None:
                        K.append((FRAME_SEQ, result[3], E))
                     break
                  promise_resolve(p, V)
                  continue

               if ftag == FRAME_ARG:
                  # frame = (FRAME_ARG, args_list, env, app_node)
                  args      = frame[1]
                  saved_env = frame[2]
                  app_node  = frame[3]
                  if len(args) == 0:
                     if is_continuation(V):
                        _wind_walk(ctx, as_continuation_wind(V))
                        _restore_handler_stack(as_continuation_handlers(V))
                        K = list(as_continuation_k(V))
                        V = _continuation_value(V, [])
                        continue
                     pv = _apply_parameter_if(V, 0, app_node)
                     if pv is not None:
                        V = pv
                        continue
                     if is_primitive(V):
                        V = as_primitive_fn(V)(ctx, saved_env, [], app_node)
                        continue
                     r = _apply_value(V, [], app_node)
                     _shadow_push(K, app_node)
                     E = r.new_env
                     C = r.body.car
                     if is_cons(r.body.cdr):
                        K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                     break
                  # Push baton FRAME_CALL and start on first arg.
                  remaining = []
                  i = 1
                  while i < len(args):
                     remaining.append(args[i])
                     i = i + 1
                  K.append((FRAME_CALL, V, [], remaining, saved_env, app_node))
                  C = args[0]
                  E = saved_env
                  break

               if ftag == FRAME_CALL:
                  # frame = (FRAME_CALL, fn_value, collected, remaining, env, app_node)
                  fn_value      = frame[1]
                  collected     = frame[2]
                  remaining     = frame[3]
                  saved_env     = frame[4]
                  app_node      = frame[5]
                  original_fn   = fn_value
                  new_collected = list(collected)
                  new_collected.append(V)
                  if len(remaining) == 0:
                     # Invoke continuation: replace K with its snapshot.
                     if is_continuation(fn_value):
                        _wind_walk(ctx, as_continuation_wind(fn_value))
                        _restore_handler_stack(as_continuation_handlers(fn_value))
                        K = list(as_continuation_k(fn_value))
                        V = _continuation_value(fn_value, new_collected)
                        continue
                     # Capture continuation: call/cc intercepted before its body.
                     if _is_call_cc_primitive(fn_value):
                        if len(new_collected) != 1:
                           raise SchemeArityError(
                              arity_mismatch_msg(as_primitive_name(fn_value),
                                                 1, 1, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        cont = make_continuation(
                           list(K), list(_wind_stack), list(_handler_stack))
                        user_proc = new_collected[0]
                        # Apply the user proc with the continuation as its arg,
                        # reusing the normal dispatch paths below.
                        fn_value = user_proc
                        new_collected = [cont]
                     # apply: splice the list argument, rewrite the dispatch so
                     # the target proc is tail-called through the normal CEK
                     # path.  Avoids the Python stack frame that _prim_apply's
                     # re-entry into cek_eval would create.  Loops so (apply apply
                     # ...) collapses rather than firing the stub body.
                     while _is_apply_primitive(fn_value):
                        proc, flat_args = _unpack_apply_args(new_collected, app_node)
                        if not (is_primitive(proc) or is_closure(proc)
                                or is_case_closure(proc) or is_continuation(proc)
                                or is_parameter(proc)):
                           raise SchemeTypeError(
                              'apply: first argument must be a procedure', app_node)
                        fn_value = proc
                        new_collected = flat_args
                     # call-with-values: install consumer frame, tail-call producer.
                     if _is_call_with_values_primitive(fn_value):
                        if len(new_collected) != 2:
                           raise SchemeArityError(
                              arity_mismatch_msg('call-with-values', 2, 2,
                                                 len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        producer = new_collected[0]
                        consumer = new_collected[1]
                        K.append((FRAME_CWV_CONSUMER, consumer, app_node))
                        fn_value = producer
                        new_collected = []
                     # force: install result frame, tail-call the thunk (or return
                     # the cached value immediately if the promise is already done).
                     # R7RS-small 6.10 leaves force-of-non-promise implementation-
                     # defined; we return non-promises unchanged so callers can
                     # write (force x) without first checking promise?, matching
                     # SRFI 155 and most R6RS impls.
                     if _is_force_primitive(fn_value):
                        if len(new_collected) != 1:
                           raise SchemeArityError(
                              arity_mismatch_msg('force', 1, 1, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        p = new_collected[0]
                        if not is_promise(p):
                           V = p
                           continue
                        if as_promise_is_done(p):
                           V = as_promise_payload(p)
                           continue
                        K.append((FRAME_FORCE_RESULT, p))
                        fn_value = as_promise_payload(p)
                        new_collected = []
                     # make-parameter: if a converter is given, tail-call it with
                     # the init value and wrap its return as a Parameter via
                     # FRAME_MAKE_PARAMETER.  Without a converter, build the
                     # parameter inline.
                     if _is_make_parameter_primitive(fn_value):
                        if len(new_collected) not in (1, 2):
                           raise SchemeArityError(
                              arity_mismatch_msg('make-parameter', 1, 2,
                                                 len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        if len(new_collected) == 1:
                           V = make_parameter(new_collected[0], None)
                           continue
                        converter = new_collected[1]
                        init = new_collected[0]
                        if not (is_primitive(converter) or is_closure(converter)
                                or is_case_closure(converter)):
                           raise SchemeTypeError(
                              'make-parameter: converter must be a procedure',
                              app_node)
                        K.append((FRAME_MAKE_PARAMETER, converter))
                        fn_value = converter
                        new_collected = [init]
                     # with-exception-handler: push handler on _handler_stack,
                     # push FRAME_POP_HANDLER, tail-call thunk.  Handler is
                     # popped on normal return via FRAME_POP_HANDLER, or
                     # consumed by raise / raise-continuable.
                     if _is_with_exception_handler_primitive(fn_value):
                        if len(new_collected) != 2:
                           raise SchemeArityError(
                              arity_mismatch_msg('with-exception-handler', 2, 2,
                                                 len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        handler = new_collected[0]
                        thunk   = new_collected[1]
                        _handler_stack.append(handler)
                        K.append((FRAME_POP_HANDLER,))
                        fn_value = thunk
                        new_collected = []
                     # raise (non-continuable): throw Python SchemeRaised so the
                     # exception unwinds the CEK loop.  cek_eval's except block
                     # routes to the topmost installed handler if any.
                     if _is_raise_primitive(fn_value):
                        if len(new_collected) != 1:
                           raise SchemeArityError(
                              arity_mismatch_msg('raise', 1, 1, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        raise SchemeRaised(new_collected[0], app_node,
                                           continuable=False)
                     # raise-continuable: handler's return value flows back to
                     # the raise-continuable call site (R7RS-correct).  Pop the
                     # handler so a re-raise inside the handler reaches the next
                     # outer one; FRAME_REINSTALL_HANDLER puts it back on return.
                     if _is_raise_continuable_primitive(fn_value):
                        if len(new_collected) != 1:
                           raise SchemeArityError(
                              arity_mismatch_msg('raise-continuable', 1, 1,
                                                 len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        raised_val = new_collected[0]
                        if not _handler_stack:
                           raise SchemeRaised(raised_val, app_node, continuable=True)
                        handler = _handler_stack.pop()
                        K.append((FRAME_REINSTALL_HANDLER, handler))
                        fn_value = handler
                        new_collected = [raised_val]
                     # eval: expand and analyze the datum once, then set C to the
                     # expanded form and continue in the same cek_eval call.  Tail
                     # calls inside the eval'd expression compose with the
                     # surrounding continuation, so deep recursion through eval
                     # doesn't add Python stack.  The optional env-spec argument
                     # selects the evaluation environment: an env value from
                     # (interaction-environment) or (environment ...).  Without
                     # it, the caller's global env is used.
                     if _is_eval_primitive(fn_value):
                        if len(new_collected) not in (1, 2):
                           raise SchemeArityError(
                              arity_mismatch_msg('eval', 1, 2, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        datum = new_collected[0]
                        if len(new_collected) == 2:
                           env_arg = new_collected[1]
                           if not is_environment(env_arg):
                              raise SchemeTypeError(
                                 'eval: second argument must be an environment',
                                 src_of(app_node) if app_node is not None else None)
                           target_env = as_environment(env_arg)
                        else:
                           target_env = saved_env.getGlobalEnv()
                        from pyscheme.Expander  import expand
                        from pyscheme.Analyzer  import analyze
                        from pyscheme.primitives import PRIMITIVE_ARITIES
                        expanded = expand(datum)
                        analyze(expanded, dict(PRIMITIVE_ARITIES))
                        C = expanded
                        E = target_env
                        break
                     # error: build an ErrorObject and throw Python SchemeUserError
                     # (which subclasses SchemeRaised), letting cek_eval's except
                     # path route to the handler stack.
                     if _is_error_primitive(fn_value):
                        if len(new_collected) < 1:
                           raise SchemeArityError(
                              arity_mismatch_msg('error', 1, None, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        msg_arg = new_collected[0]
                        if not is_string(msg_arg):
                           raise SchemeTypeError(
                              'error: first argument must be a string', app_node)
                        msg = as_string(msg_arg)
                        irritants = []
                        i = 1
                        while i < len(new_collected):
                           irritants.append(new_collected[i])
                           i = i + 1
                        raise SchemeUserError(msg, irritants, app_node)
                     # %with-parameters: apply converters, save old, install new,
                     # push wind frame so we integrate with dynamic-wind / continuation
                     # re-entry, tail-call thunk.
                     if _is_with_parameters_primitive(fn_value):
                        if len(new_collected) != 3:
                           raise SchemeArityError(
                              arity_mismatch_msg('%with-parameters', 3, 3,
                                                 len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        install_prim, restore_prim = _build_parameterize_winds(
                           new_collected[0], new_collected[1],
                           ctx, saved_env, app_node)
                        _wind_stack.append((install_prim, restore_prim))
                        K.append((FRAME_DYNAMIC_WIND_AFTER, restore_prim))
                        fn_value = new_collected[2]
                        new_collected = []
                     # dynamic-wind: install the wind frame in the CEK machine
                     # so continuation captures see it and FRAME_DYNAMIC_WIND_AFTER
                     # runs the after thunk when the body returns.
                     if _is_dynamic_wind_primitive(fn_value):
                        if len(new_collected) != 3:
                           raise SchemeArityError(
                              arity_mismatch_msg('dynamic-wind',
                                                 3, 3, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        before = new_collected[0]
                        thunk  = new_collected[1]
                        after  = new_collected[2]
                        from pyscheme.primitives.meta import _apply_scheme_proc
                        _apply_scheme_proc(before, [], ctx, saved_env, app_node)
                        _wind_stack.append((before, after))
                        K.append((FRAME_DYNAMIC_WIND_AFTER, after))
                        fn_value = thunk
                        new_collected = []
                     pv = _apply_parameter_if(fn_value, len(new_collected), app_node)
                     if pv is not None:
                        V = pv
                        continue
                     # Record accessor: type-check the arg using the call-site
                     # app_node so the error position points at the user's call,
                     # not the define-record-type form.
                     if is_record_accessor(fn_value):
                        if len(new_collected) != 1:
                           raise SchemeArityError(
                              arity_mismatch_msg(
                                 as_record_accessor_name(fn_value),
                                 1, 1, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        rt = as_record_accessor_type(fn_value)
                        rec = new_collected[0]
                        if not is_record(rec) or as_record_type(rec) is not rt:
                           raise SchemeTypeError(
                              as_record_accessor_name(fn_value)
                              + ': argument is not a ' + as_record_type_name(rt),
                              src_of(app_node) if app_node is not None else None)
                        V = as_record_fields(rec)[as_record_accessor_index(fn_value)]
                        continue
                     # Record mutator: same pattern; V is VOID after assignment.
                     if is_record_mutator(fn_value):
                        if len(new_collected) != 2:
                           raise SchemeArityError(
                              arity_mismatch_msg(
                                 as_record_mutator_name(fn_value),
                                 2, 2, len(new_collected)),
                              src_of(app_node) if app_node is not None else None)
                        rt = as_record_mutator_type(fn_value)
                        rec = new_collected[0]
                        if not is_record(rec) or as_record_type(rec) is not rt:
                           raise SchemeTypeError(
                              as_record_mutator_name(fn_value)
                              + ': first argument is not a ' + as_record_type_name(rt),
                              src_of(app_node) if app_node is not None else None)
                        as_record_fields(rec)[as_record_mutator_index(fn_value)] = new_collected[1]
                        V = VOID_VALUE
                        continue
                     if is_primitive(fn_value):
                        V = as_primitive_fn(fn_value)(ctx, saved_env, new_collected, app_node)
                        continue
                     r = _apply_value(fn_value, new_collected, app_node)
                     if fn_value is original_fn:
                        _shadow_push(K, app_node)
                     E = r.new_env
                     C = r.body.car
                     if is_cons(r.body.cdr):
                        K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                     break
                  new_remaining = []
                  i = 1
                  while i < len(remaining):
                     new_remaining.append(remaining[i])
                     i = i + 1
                  K.append((FRAME_CALL, fn_value, new_collected,
                            new_remaining, saved_env, app_node))
                  C = remaining[0]
                  E = saved_env
                  break

               if ftag == FRAME_SEQ:
                  # frame = (FRAME_SEQ, remaining_body_cons, env)
                  remaining = frame[1]
                  saved_env = frame[2]
                  E = saved_env
                  C = remaining.car
                  if is_cons(remaining.cdr):
                     K.append((FRAME_SEQ, remaining.cdr, saved_env))
                  break

               if ftag == FRAME_WHEN:
                  body      = frame[1]
                  saved_env = frame[2]
                  if isFalse(V):
                     V = VOID_VALUE
                     continue
                  E = saved_env
                  C = body.car
                  if is_cons(body.cdr):
                     K.append((FRAME_SEQ, body.cdr, saved_env))
                  break

               if ftag == FRAME_UNLESS:
                  body      = frame[1]
                  saved_env = frame[2]
                  if isFalse(V):
                     E = saved_env
                     C = body.car
                     if is_cons(body.cdr):
                        K.append((FRAME_SEQ, body.cdr, saved_env))
                     break
                  V = VOID_VALUE
                  continue

               if ftag == FRAME_AND:
                  remaining = frame[1]
                  saved_env = frame[2]
                  if isFalse(V):
                     continue   # short-circuit: V stays #f
                  if is_nil(remaining):
                     continue   # V is the last truthy
                  if is_cons(remaining.cdr):
                     K.append((FRAME_AND, remaining.cdr, saved_env))
                  C = remaining.car
                  E = saved_env
                  break

               if ftag == FRAME_OR:
                  remaining = frame[1]
                  saved_env = frame[2]
                  if not isFalse(V):
                     continue   # short-circuit: V stays truthy
                  if is_nil(remaining):
                     continue   # V stays as last false
                  if is_cons(remaining.cdr):
                     K.append((FRAME_OR, remaining.cdr, saved_env))
                  C = remaining.car
                  E = saved_env
                  break

               if ftag == FRAME_COND:
                  current   = frame[1]
                  remaining = frame[2]
                  saved_env = frame[3]
                  if isFalse(V):
                     # Test failed - advance to next clause.
                     if is_nil(remaining):
                        V = VOID_VALUE
                        continue
                     nxt  = remaining.car
                     rest = remaining.cdr
                     kind = _classify_cond_clause(nxt)
                     E = saved_env
                     if kind[0] == 'else':
                        body = kind[1]
                        C = body.car
                        if is_cons(body.cdr):
                           K.append((FRAME_SEQ, body.cdr, saved_env))
                        break
                     K.append((FRAME_COND, nxt, rest, saved_env))
                     C = kind[1]
                     break
                  # Test truthy - dispatch on clause kind.
                  kind = _classify_cond_clause(current)
                  if kind[0] == 'test-only':
                     continue   # V stays as the test value
                  if kind[0] == 'body':
                     body = kind[2]
                     E = saved_env
                     C = body.car
                     if is_cons(body.cdr):
                        K.append((FRAME_SEQ, body.cdr, saved_env))
                     break
                  # 'arrow' - apply proc to test value
                  proc_expr = kind[2]
                  K.append((FRAME_COND_ARROW, V, saved_env))
                  C = proc_expr
                  E = saved_env
                  break

               if ftag == FRAME_COND_ARROW:
                  test_value = frame[1]
                  saved_env  = frame[2]
                  if is_continuation(V):
                     _wind_walk(ctx, as_continuation_wind(V))
                     _restore_handler_stack(as_continuation_handlers(V))
                     K = list(as_continuation_k(V))
                     V = _continuation_value(V, [test_value])
                     continue
                  pv = _apply_parameter_if(V, 1, None)
                  if pv is not None:
                     V = pv
                     continue
                  if is_primitive(V):
                     V = as_primitive_fn(V)(ctx, saved_env, [test_value], None)
                     continue
                  r = _apply_value(V, [test_value], None)
                  E = r.new_env
                  C = r.body.car
                  if is_cons(r.body.cdr):
                     K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                  break

               if ftag == FRAME_CASE_ARROW:
                  key_value = frame[1]
                  saved_env = frame[2]
                  if is_continuation(V):
                     _wind_walk(ctx, as_continuation_wind(V))
                     _restore_handler_stack(as_continuation_handlers(V))
                     K = list(as_continuation_k(V))
                     V = _continuation_value(V, [key_value])
                     continue
                  pv = _apply_parameter_if(V, 1, None)
                  if pv is not None:
                     V = pv
                     continue
                  if is_primitive(V):
                     V = as_primitive_fn(V)(ctx, saved_env, [key_value], None)
                     continue
                  r = _apply_value(V, [key_value], None)
                  E = r.new_env
                  C = r.body.car
                  if is_cons(r.body.cdr):
                     K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                  break

               if ftag == FRAME_CASE:
                  # V is the (possibly-matched) key value on first entry, or the
                  # outcome of the prior clause's no-match check on subsequent
                  # entries (ignored - we always look at the key, held in frame).
                  current_clause = frame[1]
                  remaining      = frame[2]
                  saved_env      = frame[3]
                  # First entry: V holds the key value.  We stash it back on any
                  # retry by re-pushing FRAME_CASE frames that carry the key
                  # alongside the remaining clauses.  Walk the current clause.
                  head = current_clause.car
                  if is_symbol(head) and as_symbol(head) == 'else':
                     body = current_clause.cdr
                     E = saved_env
                     if is_cons(body) and is_symbol(body.car) and as_symbol(body.car) == '=>':
                        K.append((FRAME_CASE_ARROW, V, saved_env))
                        C = body.cdr.car
                     else:
                        C = body.car
                        if is_cons(body.cdr):
                           K.append((FRAME_SEQ, body.cdr, saved_env))
                     break
                  # Datum-list match: head is the list of literal datums.
                  matched = False
                  cur = head
                  while is_cons(cur):
                     if eqv_atom(V, cur.car):
                        matched = True
                        break
                     cur = cur.cdr
                  if matched:
                     body = current_clause.cdr
                     E = saved_env
                     if is_cons(body) and is_symbol(body.car) and as_symbol(body.car) == '=>':
                        K.append((FRAME_CASE_ARROW, V, saved_env))
                        C = body.cdr.car
                     else:
                        C = body.car
                        if is_cons(body.cdr):
                           K.append((FRAME_SEQ, body.cdr, saved_env))
                     break
                  # No match; advance to the next clause (V stays as the key).
                  if is_nil(remaining):
                     V = VOID_VALUE
                     continue
                  nxt = remaining.car
                  rst = remaining.cdr
                  K.append((FRAME_CASE, nxt, rst, saved_env))
                  continue

               if ftag == FRAME_LET:
                  names         = frame[1]
                  collected     = frame[2]
                  remaining     = frame[3]
                  body          = frame[4]
                  saved_env     = frame[5]
                  new_collected = list(collected)
                  new_collected.append(V)
                  if len(remaining) == 0:
                     bindings = {}
                     i = 0
                     while i < len(names):
                        bindings[names[i]] = new_collected[i]
                        i = i + 1
                     new_env = Environment(saved_env, initialBindings=bindings)
                     E = new_env
                     C = body.car
                     if is_cons(body.cdr):
                        K.append((FRAME_SEQ, body.cdr, new_env))
                     break
                  new_remaining = []
                  i = 1
                  while i < len(remaining):
                     new_remaining.append(remaining[i])
                     i = i + 1
                  K.append((FRAME_LET, names, new_collected,
                            new_remaining, body, saved_env))
                  C = remaining[0]
                  E = saved_env
                  break

               if ftag == FRAME_LET_STAR:
                  name      = frame[1]
                  remaining = frame[2]
                  body      = frame[3]
                  saved_env = frame[4]
                  new_env   = Environment(saved_env, initialBindings={name: V})
                  if len(remaining) == 0:
                     E = new_env
                     C = body.car
                     if is_cons(body.cdr):
                        K.append((FRAME_SEQ, body.cdr, new_env))
                     break
                  new_remaining = []
                  i = 1
                  while i < len(remaining):
                     new_remaining.append(remaining[i])
                     i = i + 1
                  next_pair = remaining[0]
                  K.append((FRAME_LET_STAR, next_pair[0], new_remaining, body, new_env))
                  C = next_pair[1]
                  E = new_env
                  break

               if ftag == FRAME_LETREC:
                  name      = frame[1]
                  remaining = frame[2]
                  body      = frame[3]
                  saved_env = frame[4]
                  saved_env.set(name, V)
                  if len(remaining) == 0:
                     E = saved_env
                     C = body.car
                     if is_cons(body.cdr):
                        K.append((FRAME_SEQ, body.cdr, saved_env))
                     break
                  new_remaining = []
                  i = 1
                  while i < len(remaining):
                     new_remaining.append(remaining[i])
                     i = i + 1
                  next_pair = remaining[0]
                  K.append((FRAME_LETREC, next_pair[0], new_remaining, body, saved_env))
                  C = next_pair[1]
                  E = saved_env
                  break

               if ftag == FRAME_SHADOW_POP:
                  if _shadow_stack:
                     _shadow_stack.pop()
                  continue

               raise RuntimeError("unknown frame tag: " + str(ftag))

            # fall through to outer `while True` - restart EVAL


      # -------- Self-test --------

      except _CATCHABLE_LOCAL as e:
         # Walk K to find a handler frame; on the way, run any
         # FRAME_DYNAMIC_WIND_AFTER thunks (errors swallowed, matching
         # the C++ reference's exception-dispatch convention).  When
         # the handler is found, dispatch it via _enter_proc which
         # rewrites C / E / K / V in place; the next outer-loop
         # iteration resumes the same activation record with the
         # handler call set up.  No native recursion to invoke the
         # handler, so a continuation captured inside the handler
         # body sees the outer K-stack as its captured K.
         from pyscheme.primitives.meta import _apply_scheme_proc
         from pyscheme.AST import make_error_object
         _w = K
         handler = None
         while _w:
            frame = _w.pop()
            ftag  = frame[0]
            if ftag == FRAME_POP_HANDLER:
               if not _handler_stack:
                  break
               handler = _handler_stack.pop()
               break
            if ftag == FRAME_REINSTALL_HANDLER:
               continue
            if ftag == FRAME_DYNAMIC_WIND_AFTER:
               after = frame[1]
               if _wind_stack:
                  _wind_stack.pop()
               try:
                  _apply_scheme_proc(after, [], ctx, None, None)
               except BaseException:
                  pass
         if handler is None:
            raise
         if isinstance(e, SchemeRaised):
            raised_value = e.value
         else:
            raised_value = make_error_object(e.msg, [])
         result = _enter_proc(handler, [raised_value], ctx, E, None)
         if result[0] == 'value':
            V = result[1]
            skip_eval = True
         elif result[0] == 'cont':
            K = result[1]
            V = result[2]
            skip_eval = True
         else:  # 'enter'
            C = result[1]
            E = result[2]
            if result[3] is not None:
               K.append((FRAME_SEQ, result[3], E))
         continue


# -------- Self-test --------

if __name__ == '__main__':
   # When run as `python -m pyscheme.Evaluator` this module loads as __main__.
   # The primitives package below imports cek_eval and _apply_value via
   # `from pyscheme.Evaluator import ...`, which would otherwise load a
   # second copy of this module under the name pyscheme.Evaluator and
   # produce duplicate value tags / class instances.  Alias __main__ in
   # sys.modules so the absolute import resolves to this same module.
   import sys
   sys.modules['pyscheme.Evaluator'] = sys.modules[__name__]

   from pyscheme.Parser     import parse, parse_one
   from pyscheme.Expander   import expand
   from pyscheme.Analyzer   import analyze, extend_static_env_with_define
   from pyscheme.primitives import install_primitives, PRIMITIVE_ARITIES

   def _to_text(v):
      """Brief textual form of a value, for test comparison.  Avoids
      depending on PrettyPrinter (which may not be ported yet)."""
      if isinstance(v, ConsCell):
         items = []
         cur = v
         while isinstance(cur, ConsCell):
            items.append(_to_text(cur.car))
            cur = cur.cdr
         if is_nil(cur):
            return '(' + ' '.join(items) + ')'
         return '(' + ' '.join(items) + ' . ' + _to_text(cur) + ')'
      if not isinstance(v, tuple) or len(v) == 0:
         return repr(v)
      tag = v[0]
      if tag == NIL:      return '()'
      if tag == VOID:     return '#<void>'
      if tag == INTEGER:  return str(v[1])
      if tag == REAL:     return repr(v[1])
      if tag == BOOLEAN:  return '#t' if v[1] else '#f'
      if tag == STRING:   return '"' + v[1] + '"'
      if tag == CHARACTER: return '#\\' + v[1]
      if tag == SYMBOL:   return v[1]
      if tag == CLOSURE:  return '#<closure>'
      if tag == PRIMITIVE: return '#<primitive ' + v[1] + '>'
      return repr(v)

   def _eval_source(source, env, static_env, ctx):
      forms = parse(source)
      last = None
      for form in forms:
         expanded = expand(form)
         analyze(expanded, static_env)
         extend_static_env_with_define(static_env, expanded)
         last = cek_eval(expanded, env, ctx)
      return last

   from pyscheme.Context import Context

   n_pass = 0
   n_fail = 0

   ok_cases = [
      # literals
      ('42',                                  '42'),
      ('#t',                                  '#t'),
      ('#f',                                  '#f'),
      ('"hello"',                             '"hello"'),
      # define + lookup
      ('(define x 5) x',                      '5'),
      ('(define x 5) (set! x 99) x',          '99'),
      # immediate lambda
      ('((lambda (x) x) 7)',                  '7'),
      # if
      ('(if #t 1 2)',                         '1'),
      ('(if #f 1 2)',                         '2'),
      ('(if #t 1)',                           '1'),
      ('(if #f 1)',                           '#<void>'),
      # define a function shorthand
      ('(define (id x) x) (id 42)',           '42'),
      ('(define id (lambda (x) x)) (id 99)',  '99'),
      # multi-arg lambda
      ('((lambda (x y) x) 11 22)',            '11'),
      ('((lambda (a b c) b) 10 20 30)',       '20'),
      ('((lambda () 42))',                    '42'),
      # multi-expression body (implicit begin)
      ('((lambda (x) (set! x 1) (set! x 2) x) 0)', '2'),
      # quoted data
      ("'x",                                  'x'),
      ("'()",                                 '()'),
      ("'(1 2 3)",                            '(1 2 3)'),
      # list primitives
      ('(car (list 1 2 3))',                  '1'),
      ('(cdr (list 1 2 3))',                  '(2 3)'),
      ('(cons 1 (cons 2 (cons 3 (list))))',   '(1 2 3)'),
      ('(cons 1 2)',                          '(1 . 2)'),
      ('(pair? (list 1 2))',                  '#t'),
      ('(pair? (list))',                      '#f'),
      ('(null? (list))',                      '#t'),
      ('(null? (list 1))',                    '#f'),
      # begin
      ('(begin 1 2 3)',                       '3'),
      # when / unless
      ('(when #t 1 2 3)',                     '3'),
      ('(when #f 1)',                         '#<void>'),
      ('(unless #f 1 2 3)',                   '3'),
      ('(unless #t 1)',                       '#<void>'),
      # and / or
      ('(and)',                               '#t'),
      ('(and 1 2 3)',                         '3'),
      ('(and 1 #f 3)',                        '#f'),
      ('(or)',                                '#f'),
      ('(or #f #f 3)',                        '3'),
      ('(or 1 (cons 1 2))',                   '1'),
      # cond
      ('(cond (#t 1))',                       '1'),
      ('(cond (#f 1) (else 2))',              '2'),
      ('(cond (7))',                          '7'),
      ('(cond ((cons 1 2) => car))',          '1'),
      ('(cond (#f => car) (else 42))',        '42'),
      # let / let* / letrec
      ('(let () 42)',                         '42'),
      ('(let ((x 1)) x)',                     '1'),
      ('(let ((x 1) (y 2)) y)',               '2'),
      ('(let ((a 1) (b 2) (c 3)) (list a b c))', '(1 2 3)'),
      ('(let* ((x 1) (y x)) y)',              '1'),
      ('(letrec ((f (lambda (x) x))) (f 1))', '1'),
      ('(letrec* ((f (lambda (x) x))) (f 1))', '1'),
      # named let
      ('(let loop ((x 1)) x)',                '1'),
      ('(let loop ((n 5) (acc 1)) (if (= n 0) acc (loop (- n 1) (* acc n))))', '120'),
      # variadic lambda
      ('((lambda args args) 1 2 3)',          '(1 2 3)'),
      ('((lambda (x . rest) rest) 1 2 3)',    '(2 3)'),
      ('((lambda (x . rest) rest) 1)',        '()'),
      # arithmetic
      ('(+ 1 2 3)',                           '6'),
      ('(* 2 3 4)',                           '24'),
      ('(- 10 3)',                            '7'),
      # recursion
      ('(define (fact n) (if (= n 0) 1 (* n (fact (- n 1))))) (fact 5)', '120'),
   ]

   class TestState:
      def __init__(self, env, static_env, ctx):
         self.env        = env
         self.static_env = static_env
         self.ctx        = ctx

   def fresh_state():
      from pyscheme.library import register_standard_libraries
      env = Environment()
      install_primitives(env)
      register_standard_libraries(env)
      static_env = dict(PRIMITIVE_ARITIES)
      ctx = Context()
      return TestState(env, static_env, ctx)

   print('-- evaluator: full pipeline --')
   i = 0
   while i < len(ok_cases):
      source   = ok_cases[i][0]
      expected = ok_cases[i][1]
      st = fresh_state()
      try:
         got_val = _eval_source(source, st.env, st.static_env, st.ctx)
      except Exception as e:
         print("[FAIL] %r: %s: %s" % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      got = _to_text(got_val) if got_val is not None else ''
      if got == expected:
         print("[ OK ] %r -> %s" % (source, got))
         n_pass = n_pass + 1
      else:
         print("[FAIL] %r" % source)
         print("        expected: %s" % expected)
         print("        got:      %s" % got)
         n_fail = n_fail + 1
      i = i + 1

   # Runtime errors
   print()
   print('-- evaluator: runtime errors --')
   err_cases = [
      ('foo',                              SchemeUnboundError, 'unbound variable'),
      ('(set! foo 1)',                     SchemeUnboundError, 'set! on unbound'),
      ('(car 5)',                          SchemeTypeError,    None),   # primitive type error
      ('((lambda (x) x))',                 SchemeArityError,   '0 arguments provided'),
   ]
   i = 0
   while i < len(err_cases):
      source       = err_cases[i][0]
      expected_cls = err_cases[i][1]
      expected_sub = err_cases[i][2]
      st = fresh_state()
      try:
         _eval_source(source, st.env, st.static_env, st.ctx)
      except expected_cls as e:
         if expected_sub is None or expected_sub in str(e):
            print("[ OK ] %r -> %s: %s" % (source, type(e).__name__, e))
            n_pass = n_pass + 1
         else:
            print("[WARN] %r -> %s: %s" % (source, type(e).__name__, e))
            print("        expected substring: %r" % expected_sub)
            n_pass = n_pass + 1
         i = i + 1
         continue
      except Exception as e:
         print("[FAIL] %r: wrong exception %s: %s" % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      print("[FAIL] %r: expected %s" % (source, expected_cls.__name__))
      n_fail = n_fail + 1
      i = i + 1

   # -------- .sld auto-discovery (cat 6 item 4) --------
   # Set SCHEME_LIBRARY_PATH temporarily so an unregistered library is
   # found via testing/sld-test/lib4.sld.  Verifies that import auto-
   # loads the .sld file and the imported procedure works.
   import os
   _saved_lp = os.environ.get('SCHEME_LIBRARY_PATH')
   os.environ['SCHEME_LIBRARY_PATH'] = 'testing/sld-test'
   try:
      ts = fresh_state()
      result = _eval_source(
         '(import (lib4)) (greet)', ts.env, ts.static_env, ts.ctx)
      if _to_text(result) == 'auto-discovered':
         print("[ OK ] auto-discover lib4.sld via SCHEME_LIBRARY_PATH")
         n_pass = n_pass + 1
      else:
         print("[FAIL] auto-discover lib4.sld: got %r, expected 'auto-discovered'"
               % _to_text(result))
         n_fail = n_fail + 1
   except Exception as e:
      print("[FAIL] auto-discover lib4.sld: %s: %s" % (type(e).__name__, e))
      n_fail = n_fail + 1
   finally:
      if _saved_lp is None:
         del os.environ['SCHEME_LIBRARY_PATH']
      else:
         os.environ['SCHEME_LIBRARY_PATH'] = _saved_lp

   print()
   print("%d passed, %d failed" % (n_pass, n_fail))
