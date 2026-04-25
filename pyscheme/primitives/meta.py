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


def _prim_error_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts error at FRAME_CALL dispatch so that raised
   # errors flow through the CEK-level handler stack rather than Python
   # exceptions.  This body fires only if the interceptor is bypassed.
   raise SchemeTypeError(
      'error: cannot be called through a re-entering path in this '
      'implementation', app_node)


def _prim_raise_unreached(ctx, env, args, app_node):
   raise SchemeTypeError(
      'raise: cannot be called through a re-entering path in this '
      'implementation', app_node)


def _prim_raise_continuable_unreached(ctx, env, args, app_node):
   raise SchemeTypeError(
      'raise-continuable: cannot be called through a re-entering path '
      'in this implementation', app_node)


def _prim_with_exception_handler_unreached(ctx, env, args, app_node):
   raise SchemeTypeError(
      'with-exception-handler: cannot be called through a re-entering '
      'path in this implementation', app_node)


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


def _prim_apply_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts apply at FRAME_CALL dispatch to tail-call
   # the target procedure through the normal CEK path.  This body only
   # fires if the interception was bypassed (e.g., apply substituted
   # via macro hygiene into a position other than operator position,
   # which isn't well-defined).
   raise SchemeTypeError(
      'apply: cannot be called through a re-entering path in this '
      'implementation', app_node)


def _prim_eval_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts eval at FRAME_CALL dispatch so the expanded
   # datum runs in the same cek_eval call (preserving outer TCO).  This
   # body fires only if the interception was bypassed.
   raise SchemeTypeError(
      'eval: cannot be called through a re-entering path in this '
      'implementation', app_node)


def _prim_force_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts force at FRAME_CALL dispatch to keep the
   # thunk evaluation tail-position and drive the delay-force chain via
   # FRAME_FORCE_RESULT frames rather than a Python while loop.  This body
   # fires only if the interception was bypassed.
   raise SchemeTypeError(
      'force: cannot be called through a re-entering path in this '
      'implementation', app_node)


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


def _prim_make_parameter_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts make-parameter at FRAME_CALL dispatch so
   # the converter is tail-called through the CEK path, driven by
   # FRAME_MAKE_PARAMETER.  This body fires only if interception was
   # bypassed.
   raise SchemeTypeError(
      'make-parameter: cannot be called through a re-entering path '
      'in this implementation', app_node)


def _prim_with_parameters_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts %with-parameters at FRAME_CALL dispatch to
   # run the thunk in tail position and integrate with the wind stack for
   # continuation / exception handling.  This body fires only if the
   # interception was bypassed.
   raise SchemeTypeError(
      '%with-parameters: cannot be called through a re-entering path '
      'in this implementation', app_node)


def _prim_call_with_values_unreached(ctx, env, args, app_node):
   # The Evaluator intercepts call-with-values at FRAME_CALL dispatch so
   # the producer and consumer are tail-called through the CEK path,
   # driven by FRAME_CWV_CONSUMER.  This body fires only if interception
   # was bypassed.
   raise SchemeTypeError(
      'call-with-values: cannot be called through a re-entering path '
      'in this implementation', app_node)


def register():
   register_primitive('error', (1, None), _prim_error_unreached,
      doc=(
         "Raise a user error.  The first argument is a string message;\n"
         "any trailing arguments are appended to the message as irritants\n"
         "separated by spaces.  Does not return."),
      category=CATEGORY)

   register_primitive('apply', (2, None), _prim_apply_unreached,
      usage='(apply <proc> <arg>... <list>)',
      doc=(
         "Call <proc> with the elements of <list> as its arguments, optionally\n"
         "prepended by any leading <arg>s.  The last argument must be a\n"
         "proper list.  Equivalent to (proc arg1 ... arg_m elem1 ... elem_n)\n"
         "when <list> has elements elem1..elem_n."),
      category=CATEGORY)

   register_primitive('eval', (1, 2), _prim_eval_unreached,
      usage='(eval <datum> [<env-spec>])',
      doc=(
         "Evaluate <datum> as a Scheme expression in the current global\n"
         "environment.  The second argument (R7RS environment specifier) is\n"
         "accepted for compatibility but ignored in this implementation."),
      category=CATEGORY)

   register_primitive('force', (1, 1), _prim_force_unreached,
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

   register_primitive('call-with-values', (2, 2), _prim_call_with_values_unreached,
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
   register_primitive('make-parameter', (1, 2), _prim_make_parameter_unreached,
      usage='(make-parameter <init> [<converter>])',
      doc=(
         "Return a new parameter object with initial value <init>.  When\n"
         "called with zero arguments, a parameter returns its current\n"
         "dynamic value; use parameterize to bind a different value for\n"
         "a dynamic extent.  If <converter> is supplied, it is applied\n"
         "to <init> (and to each later parameterize value) to produce the\n"
         "stored value.  R7RS 4.2.6."),
      category=CATEGORY)

   register_primitive('%with-parameters', (3, 3), _prim_with_parameters_unreached,
      doc=(
         "Dynamically bind parameters for the extent of a thunk.  Internal:\n"
         "the Expander emits calls to %with-parameters for parameterize."),
      category=CATEGORY)

   # Exception handling
   register_primitive('raise', (1, 1), _prim_raise_unreached,
      doc=(
         "Raise a non-continuable exception carrying the given value.\n"
         "If a with-exception-handler (or guard) handler catches it, the\n"
         "handler's return value is discarded and a secondary error fires\n"
         "because there is no valid continuation to return to.  R7RS 6.11."),
      category=CATEGORY)

   register_primitive('raise-continuable', (1, 1), _prim_raise_continuable_unreached,
      doc=(
         "Raise a continuable exception carrying the given value.  When\n"
         "caught by with-exception-handler, the handler's return value\n"
         "becomes the return value of (raise-continuable ...).  R7RS 6.11."),
      category=CATEGORY)

   register_primitive('with-exception-handler', (2, 2),
      _prim_with_exception_handler_unreached,
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
