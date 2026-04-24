"""Meta-operations: error, apply, eval, force, make-promise, values, call-with-values.

R7RS library procedures that transcend normal procedure-call semantics:

    (error <message> <irritant>...)     6.11  raise a user error
    (apply <proc> <arg>... <list>)      6.10  apply proc to combined args
    (eval <datum> [<env-spec>])         6.12  evaluate a datum in the global env
    (force <promise>)                   6.10  force a promise's value
    (make-promise <obj>)                6.10  wrap a value as an already-forced promise
    (values <obj>...)                   6.10  return multiple values
    (call-with-values <producer> <consumer>) 6.10  consume producer's multi-values

Record plumbing for define-record-type (R7RS 5.5).  These %-prefixed
primitives are emitted by the Expander; users do not call them directly:

    (%make-record-type name field-names)    build a RecordType descriptor
    (%make-record record-type field-values) build a record from field list
    (%record-of-type? obj record-type)      predicate for one record type
    (%record-ref record record-type index)  type-checked field read
    (%record-set! record record-type idx v) type-checked field mutate
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_cons, is_nil, is_string, is_primitive, is_closure, is_case_closure,
   is_promise, is_multi_values, is_symbol, is_record, is_record_type,
   is_parameter, is_error_object,
   as_string, as_primitive_fn, as_symbol,
   as_promise_is_done, as_promise_payload, promise_resolve, promise_become,
   as_multi_values_list, as_record_type, as_record_fields,
   as_record_type_name,
   as_parameter_value, as_parameter_converter, set_parameter_value,
   as_error_object_message, as_error_object_irritants,
   alloc_cons, make_symbol, make_promise_done, make_multi_values,
   make_record_type, make_record, make_parameter, make_string,
   list_from_items,
   VOID_VALUE,
)
from pyscheme.Environment import SchemeTypeError, SchemeUserError, SchemeRaised


CATEGORY = 'meta'


def _prim_error(ctx, env, args, app_node):
   msg_arg = args[0]
   if not is_string(msg_arg):
      raise SchemeTypeError(
         'error: first argument must be a string', app_node)
   msg = as_string(msg_arg)
   irritants = []
   i = 1
   while i < len(args):
      irritants.append(args[i])
      i = i + 1
   raise SchemeUserError(msg, irritants, app_node)


def _prim_raise(ctx, env, args, app_node):
   raise SchemeRaised(args[0], app_node, continuable=False)


def _prim_call_cc_unreached(ctx, env, args, app_node):
   # The evaluator intercepts call/cc at the application dispatch point to
   # capture the K-stack before this body would run.  If this code fires,
   # something has invoked call/cc in a way that bypasses the interceptor
   # (e.g. through apply, which re-enters cek_eval and then calls primitives
   # directly).  Flag it loudly rather than silently misbehaving.
   raise SchemeTypeError(
      'call/cc: cannot be applied through a re-entering primitive '
      '(apply / call-with-values / force) in this implementation',
      app_node)


def _prim_dynamic_wind_unreached(ctx, env, args, app_node):
   # The evaluator intercepts dynamic-wind at the application dispatch
   # point to install a FRAME_DYNAMIC_WIND_AFTER frame and push the wind
   # entry before the body runs.  If this code fires, dynamic-wind was
   # invoked through a path that bypasses the interceptor.
   raise SchemeTypeError(
      'dynamic-wind: cannot be applied through a re-entering primitive '
      '(apply / call-with-values / force) in this implementation',
      app_node)


def _prim_raise_continuable(ctx, env, args, app_node):
   raise SchemeRaised(args[0], app_node, continuable=True)


def _prim_with_exception_handler(ctx, env, args, app_node):
   # (with-exception-handler handler thunk)
   # Install handler for the dynamic extent of thunk's execution.  On
   # SchemeRaised (and its SchemeUserError subclass), call handler with
   # the raised value.  Handler's return value becomes the return value
   # of with-exception-handler whether raise was continuable or not; the
   # control always unwinds to here because we rely on Python try/except
   # to intercept the condition.  (Strict R7RS makes handler-returns-after-
   # non-continuable-raise undefined; we pick the practical behavior because
   # guard's desugaring depends on it.)
   handler = args[0]
   thunk   = args[1]
   try:
      return _apply_scheme_proc(thunk, [], ctx, env, app_node)
   except SchemeRaised as e:
      # Run handler outside the try so a raise inside it propagates.
      return _apply_scheme_proc(handler, [e.value], ctx, env, app_node)


def _prim_error_object_message(ctx, env, args, app_node):
   obj = args[0]
   if not is_error_object(obj):
      raise SchemeTypeError(
         'error-object-message: not an error object', app_node)
   return make_string(as_error_object_message(obj))


def _prim_error_object_irritants(ctx, env, args, app_node):
   obj = args[0]
   if not is_error_object(obj):
      raise SchemeTypeError(
         'error-object-irritants: not an error object', app_node)
   return list_from_items(list(as_error_object_irritants(obj)))


def _prim_apply(ctx, env, args, app_node):
   # (apply proc arg1 arg2 ... argN list)
   # The last argument must be a proper list; it's appended to any
   # preceding leading arguments before proc is called.
   from pyscheme.Evaluator import cek_eval, _apply_value

   proc = args[0]
   combined = []
   i = 1
   while i < len(args) - 1:
      combined.append(args[i])
      i = i + 1
   last = args[len(args) - 1]
   cur = last
   while is_cons(cur):
      combined.append(cur.car)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         'apply: last argument must be a proper list', app_node)

   if is_primitive(proc):
      return as_primitive_fn(proc)(ctx, env, combined, app_node)
   if is_closure(proc) or is_case_closure(proc):
      r = _apply_value(proc, combined, app_node)
      body = r.body
      # Single-form body: evaluate the one expression directly.  Multiple
      # forms: wrap in a synthesized (begin body...) and evaluate that.
      if is_cons(body) and is_nil(body.cdr):
         return cek_eval(body.car, r.new_env, ctx)
      begin_sym  = make_symbol('begin', None)
      begin_form = alloc_cons(begin_sym, body, None)
      return cek_eval(begin_form, r.new_env, ctx)

   raise SchemeTypeError(
      'apply: first argument must be a procedure', app_node)


def _prim_eval(ctx, env, args, app_node):
   # (eval <datum> [<env-spec>])
   # The second argument (R7RS environment specifier) is accepted but
   # ignored; evaluation always runs in the current global environment.
   from pyscheme.Expander   import expand
   from pyscheme.Analyzer   import analyze
   from pyscheme.Evaluator  import cek_eval
   from pyscheme.primitives import PRIMITIVE_ARITIES

   datum      = args[0]
   expanded   = expand(datum)
   static_env = dict(PRIMITIVE_ARITIES)
   analyze(expanded, static_env)
   return cek_eval(expanded, env.getGlobalEnv(), ctx)


def _prim_force(ctx, env, args, app_node):
   # Iterative force: re-enter cek_eval on the thunk's body.  If the body
   # yields another promise, collapse the outer promise into the inner one
   # (this is what gives delay-force its stack-safety property).
   from pyscheme.Evaluator import cek_eval, _apply_value

   p = args[0]
   if not is_promise(p):
      raise SchemeTypeError(
         'force: argument must be a promise', app_node)
   while not as_promise_is_done(p):
      thunk = as_promise_payload(p)
      r = _apply_value(thunk, [], app_node)
      body = r.body
      # delay/delay-force synthesize a single-form body; no begin-wrap needed.
      v = cek_eval(body.car, r.new_env, ctx)
      if is_promise(v):
         promise_become(p, v)
      else:
         promise_resolve(p, v)
   return as_promise_payload(p)


def _prim_make_promise(ctx, env, args, app_node):
   # (make-promise obj) wraps obj in an already-forced promise.
   return make_promise_done(args[0])


def _prim_values(ctx, env, args, app_node):
   # (values) -> empty multi-values
   # (values x) -> x unwrapped (convention; avoids multi-values escape into
   #              single-value positions when caller is passing a single value)
   # (values a b ...) -> multi-values container
   if len(args) == 1:
      return args[0]
   return make_multi_values(list(args))


def _prim_make_record_type(ctx, env, args, app_node):
   # (%make-record-type 'name '(field-name ...))
   name_arg = args[0]
   fields_arg = args[1]
   if not is_symbol(name_arg):
      raise SchemeTypeError(
         '%make-record-type: first argument must be a symbol', app_node)
   field_names = []
   cur = fields_arg
   while is_cons(cur):
      f = cur.car
      if not is_symbol(f):
         raise SchemeTypeError(
            '%make-record-type: field names must be symbols', app_node)
      field_names.append(as_symbol(f))
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         '%make-record-type: field names must be a proper list', app_node)
   return make_record_type(as_symbol(name_arg), field_names)


def _prim_make_record(ctx, env, args, app_node):
   # (%make-record record-type (field-value ...))
   rt = args[0]
   field_list = args[1]
   if not is_record_type(rt):
      raise SchemeTypeError(
         '%make-record: first argument must be a record type', app_node)
   values = []
   cur = field_list
   while is_cons(cur):
      values.append(cur.car)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         '%make-record: field values must be a proper list', app_node)
   return make_record(rt, values)


def _prim_record_of_type_p(ctx, env, args, app_node):
   # (%record-of-type? obj record-type)
   from pyscheme.AST import make_boolean
   obj = args[0]
   rt  = args[1]
   if not is_record_type(rt):
      raise SchemeTypeError(
         '%record-of-type?: second argument must be a record type', app_node)
   if not is_record(obj):
      return make_boolean(False)
   return make_boolean(as_record_type(obj) is rt)


def _prim_record_ref(ctx, env, args, app_node):
   # (%record-ref record record-type index)
   from pyscheme.AST import is_integer, as_integer
   rec = args[0]
   rt  = args[1]
   idx_val = args[2]
   if not is_record_type(rt):
      raise SchemeTypeError(
         '%record-ref: second argument must be a record type', app_node)
   if not is_record(rec) or as_record_type(rec) is not rt:
      raise SchemeTypeError(
         '%record-ref: record is not of the expected type ' +
         as_record_type_name(rt), app_node)
   if not is_integer(idx_val):
      raise SchemeTypeError(
         '%record-ref: index must be an integer', app_node)
   idx = as_integer(idx_val)
   return as_record_fields(rec)[idx]


def _prim_record_set(ctx, env, args, app_node):
   # (%record-set! record record-type index value)
   from pyscheme.AST import is_integer, as_integer
   rec = args[0]
   rt  = args[1]
   idx_val = args[2]
   new_val = args[3]
   if not is_record_type(rt):
      raise SchemeTypeError(
         '%record-set!: second argument must be a record type', app_node)
   if not is_record(rec) or as_record_type(rec) is not rt:
      raise SchemeTypeError(
         '%record-set!: record is not of the expected type ' +
         as_record_type_name(rt), app_node)
   if not is_integer(idx_val):
      raise SchemeTypeError(
         '%record-set!: index must be an integer', app_node)
   idx = as_integer(idx_val)
   as_record_fields(rec)[idx] = new_val
   return VOID_VALUE


def _apply_scheme_proc(proc, arg_values, ctx, env, app_node):
   """Invoke any callable Scheme value with a Python arg list, returning
   the value.  Handles primitives, closures, case-closures; also applies
   the body via cek_eval for closure targets.  Shared helper for
   call-with-values, %with-parameters, and make-parameter's converter."""
   from pyscheme.Evaluator import cek_eval, _apply_value
   if is_primitive(proc):
      return as_primitive_fn(proc)(ctx, env, arg_values, app_node)
   if is_closure(proc) or is_case_closure(proc):
      r = _apply_value(proc, arg_values, app_node)
      body = r.body
      if is_cons(body) and is_nil(body.cdr):
         return cek_eval(body.car, r.new_env, ctx)
      begin_sym  = make_symbol('begin', None)
      begin_form = alloc_cons(begin_sym, body, None)
      return cek_eval(begin_form, r.new_env, ctx)
   raise SchemeTypeError('expected a procedure', app_node)


def _prim_make_parameter(ctx, env, args, app_node):
   # (make-parameter init) or (make-parameter init converter)
   init = args[0]
   if len(args) == 2:
      converter = args[1]
      if not (is_primitive(converter) or is_closure(converter)
              or is_case_closure(converter)):
         raise SchemeTypeError(
            'make-parameter: converter must be a procedure', app_node)
      converted = _apply_scheme_proc(converter, [init], ctx, env, app_node)
      return make_parameter(converted, converter)
   return make_parameter(init, None)


def _prim_with_parameters(ctx, env, args, app_node):
   # (%with-parameters params-list values-list thunk)
   # Dynamically bind each parameter to the corresponding value for the
   # duration of thunk's execution.  Converter, if any, is applied to each
   # new value before binding.  Restores original values on any exit
   # (normal or exception) via Python try/finally.
   params_list = args[0]
   values_list = args[1]
   thunk       = args[2]

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

   new_values = []
   cur = values_list
   while is_cons(cur):
      new_values.append(cur.car)
      cur = cur.cdr
   if not is_nil(cur):
      raise SchemeTypeError(
         '%with-parameters: value list must be proper', app_node)

   if len(params) != len(new_values):
      raise SchemeTypeError(
         '%with-parameters: parameter / value count mismatch', app_node)

   # Apply converters for each parameter that has one; freeze the final
   # to-be-installed values before touching any parameter.
   installed = []
   i = 0
   while i < len(params):
      conv = as_parameter_converter(params[i])
      if conv is None:
         installed.append(new_values[i])
      else:
         installed.append(_apply_scheme_proc(conv, [new_values[i]], ctx, env,
                                             app_node))
      i = i + 1

   # Save old values, install new ones, run thunk, restore on any exit.
   saved = []
   i = 0
   while i < len(params):
      saved.append(as_parameter_value(params[i]))
      i = i + 1
   i = 0
   while i < len(params):
      set_parameter_value(params[i], installed[i])
      i = i + 1
   try:
      return _apply_scheme_proc(thunk, [], ctx, env, app_node)
   finally:
      i = 0
      while i < len(params):
         set_parameter_value(params[i], saved[i])
         i = i + 1


def _prim_call_with_values(ctx, env, args, app_node):
   # (call-with-values producer consumer)
   # Invoke producer with no args; its result (unwrapped if multi-values) is
   # used as the argument list for consumer.  Reuses _apply_value so we
   # cover CLOSURE, CASE_CLOSURE, and PRIMITIVE producers/consumers.
   from pyscheme.Evaluator import cek_eval, _apply_value

   producer = args[0]
   consumer = args[1]
   if is_primitive(producer):
      produced = as_primitive_fn(producer)(ctx, env, [], app_node)
   elif is_closure(producer) or is_case_closure(producer):
      r = _apply_value(producer, [], app_node)
      body = r.body
      if is_cons(body) and is_nil(body.cdr):
         produced = cek_eval(body.car, r.new_env, ctx)
      else:
         begin_sym  = make_symbol('begin', None)
         begin_form = alloc_cons(begin_sym, body, None)
         produced = cek_eval(begin_form, r.new_env, ctx)
   else:
      raise SchemeTypeError(
         'call-with-values: producer must be a procedure', app_node)

   if is_multi_values(produced):
      consumer_args = as_multi_values_list(produced)
   else:
      consumer_args = [produced]

   if is_primitive(consumer):
      return as_primitive_fn(consumer)(ctx, env, consumer_args, app_node)
   if is_closure(consumer) or is_case_closure(consumer):
      r = _apply_value(consumer, consumer_args, app_node)
      body = r.body
      if is_cons(body) and is_nil(body.cdr):
         return cek_eval(body.car, r.new_env, ctx)
      begin_sym  = make_symbol('begin', None)
      begin_form = alloc_cons(begin_sym, body, None)
      return cek_eval(begin_form, r.new_env, ctx)
   raise SchemeTypeError(
      'call-with-values: consumer must be a procedure', app_node)


def register():
   register_primitive('error', (1, None), _prim_error,
      doc=(
         "Raise a user error.  The first argument is a string message;\n"
         "any trailing arguments are appended to the message as irritants\n"
         "separated by spaces.  Does not return."),
      category=CATEGORY)

   register_primitive('apply', (2, None), _prim_apply,
      usage='(apply <proc> <arg>... <list>)',
      doc=(
         "Call <proc> with the elements of <list> as its arguments, optionally\n"
         "prepended by any leading <arg>s.  The last argument must be a\n"
         "proper list.  Equivalent to (proc arg1 ... arg_m elem1 ... elem_n)\n"
         "when <list> has elements elem1..elem_n."),
      category=CATEGORY)

   register_primitive('eval', (1, 2), _prim_eval,
      usage='(eval <datum> [<env-spec>])',
      doc=(
         "Evaluate <datum> as a Scheme expression in the current global\n"
         "environment.  The second argument (R7RS environment specifier) is\n"
         "accepted for compatibility but ignored in this implementation."),
      category=CATEGORY)

   register_primitive('force', (1, 1), _prim_force,
      doc=(
         "Force a promise, returning its value.  The promise's thunk runs\n"
         "at most once; subsequent forces return the cached value.  If the\n"
         "thunk yields another promise, force follows the chain iteratively,\n"
         "so (delay-force ...) promise chains run in constant stack."),
      category=CATEGORY)

   register_primitive('make-promise', (1, 1), _prim_make_promise,
      doc=(
         "Return a promise whose forced value is obj.  Unlike delay,\n"
         "make-promise is a procedure, not a special form: its argument\n"
         "is evaluated eagerly and the resulting promise is already forced."),
      category=CATEGORY)

   register_primitive('values', (0, None), _prim_values,
      usage='(values <obj>...)',
      doc=(
         "Return the arguments as multiple values.  With zero arguments,\n"
         "returns an empty multi-values container.  With one argument,\n"
         "returns that value unchanged (no wrapper).  With two or more,\n"
         "returns a multi-values container that only call-with-values\n"
         "(and a few related forms) can consume; delivering multi-values\n"
         "to a single-value context is an error."),
      category=CATEGORY)

   register_primitive('call-with-values', (2, 2), _prim_call_with_values,
      usage='(call-with-values <producer> <consumer>)',
      doc=(
         "Call <producer> with no arguments.  Pass its return value(s)\n"
         "to <consumer>: if <producer> returned a multi-values container,\n"
         "each value becomes a separate argument to <consumer>; otherwise\n"
         "<consumer> is called with the single value.  Returns <consumer>'s\n"
         "result."),
      category=CATEGORY)

   # Record plumbing: emitted by the Expander for (define-record-type ...).
   # Not intended for direct user calls - the %-prefix signals internal use.
   register_primitive('%make-record-type', (2, 2), _prim_make_record_type,
      doc='Build a record-type descriptor.  Internal: used by define-record-type.',
      category=CATEGORY)

   register_primitive('%make-record', (2, 2), _prim_make_record,
      doc='Build a record of a given type from a field-values list.  Internal.',
      category=CATEGORY)

   register_primitive('%record-of-type?', (2, 2), _prim_record_of_type_p,
      doc='#t if obj is a record of the given record type.  Internal.',
      category=CATEGORY)

   register_primitive('%record-ref', (3, 3), _prim_record_ref,
      doc='Read the i-th field of a record; type-checks against the given record-type.  Internal.',
      category=CATEGORY)

   register_primitive('%record-set!', (4, 4), _prim_record_set,
      doc='Mutate the i-th field of a record; type-checks against the given record-type.  Internal.',
      category=CATEGORY)

   # Parameter objects
   register_primitive('make-parameter', (1, 2), _prim_make_parameter,
      usage='(make-parameter <init> [<converter>])',
      doc=(
         "Return a new parameter object with initial value <init>.  When\n"
         "called with zero arguments, a parameter returns its current\n"
         "dynamic value; use parameterize to bind a different value for\n"
         "a dynamic extent.  If <converter> is supplied, it is applied\n"
         "to <init> (and to each later parameterize value) to produce the\n"
         "stored value.  R7RS 4.2.6."),
      category=CATEGORY)

   register_primitive('%with-parameters', (3, 3), _prim_with_parameters,
      doc=(
         "Dynamically bind parameters for the extent of a thunk.  Internal:\n"
         "the Expander emits calls to %with-parameters for parameterize."),
      category=CATEGORY)

   # Exception handling
   register_primitive('raise', (1, 1), _prim_raise,
      doc=(
         "Raise a non-continuable exception carrying the given value.\n"
         "If a with-exception-handler (or guard) handler catches it, the\n"
         "handler's return value is discarded and a secondary error fires\n"
         "because there is no valid continuation to return to.  R7RS 6.11."),
      category=CATEGORY)

   register_primitive('raise-continuable', (1, 1), _prim_raise_continuable,
      doc=(
         "Raise a continuable exception carrying the given value.  When\n"
         "caught by with-exception-handler, the handler's return value\n"
         "becomes the return value of (raise-continuable ...).  R7RS 6.11."),
      category=CATEGORY)

   register_primitive('with-exception-handler', (2, 2),
      _prim_with_exception_handler,
      usage='(with-exception-handler <handler> <thunk>)',
      doc=(
         "Install <handler> for the dynamic extent of (<thunk>).  When a\n"
         "raise or raise-continuable fires inside <thunk>, call <handler>\n"
         "with the raised value.  <handler> is a 1-arg procedure; <thunk>\n"
         "is a 0-arg procedure.  R7RS 6.11."),
      category=CATEGORY)

   register_primitive('error-object-message', (1, 1),
      _prim_error_object_message,
      doc='Return the message string of an error object.  R7RS 6.11.',
      category=CATEGORY)

   register_primitive('error-object-irritants', (1, 1),
      _prim_error_object_irritants,
      doc='Return the irritants list of an error object.  R7RS 6.11.',
      category=CATEGORY)

   # First-class continuations
   register_primitive('call-with-current-continuation', (1, 1),
      _prim_call_cc_unreached,
      usage='(call-with-current-continuation <proc>)',
      doc=(
         "Capture the current continuation as a first-class procedure and\n"
         "apply <proc> to it.  Invoking the continuation with zero or more\n"
         "values abandons the current context and returns to call/cc's\n"
         "caller with those values.  R7RS 6.10."),
      category=CATEGORY)

   register_primitive('call/cc', (1, 1), _prim_call_cc_unreached,
      usage='(call/cc <proc>)',
      doc='Alias for call-with-current-continuation.',
      category=CATEGORY)

   register_primitive('dynamic-wind', (3, 3), _prim_dynamic_wind_unreached,
      usage='(dynamic-wind <before> <thunk> <after>)',
      doc=(
         "Call <before> for effect, then <thunk> for value, then <after>\n"
         "for effect.  The after thunk runs whether <thunk> returns normally\n"
         "or control leaves via a continuation invocation or an exception.\n"
         "If the dynamic extent is later re-entered via a continuation,\n"
         "<before> runs again.  R7RS 6.10."),
      category=CATEGORY)
