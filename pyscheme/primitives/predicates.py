"""Type and numeric predicates.

All predicates are total on any Scheme value and return (BOOLEAN, _).
The set here covers R7RS 6.2 / 6.3 / 6.4 / 6.7 type predicates plus
the small-integer numeric predicates (zero?, positive?, ...).
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_integer, is_real, is_rational, is_complex,
   is_boolean, is_symbol, is_string, is_character,
   is_closure, is_primitive, is_case_closure, is_promise, is_parameter,
   is_error_object, is_continuation,
   is_record_accessor, is_record_mutator,
   as_integer, as_real, make_boolean,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'predicates'


def _is_number(v):
   return is_integer(v) or is_real(v) or is_rational(v) or is_complex(v)


def _prim_number_p(ctx, env, args, app_node):
   return make_boolean(_is_number(args[0]))


def _prim_complex_p(ctx, env, args, app_node):
   # In R7RS every number is complex.
   return make_boolean(_is_number(args[0]))


def _prim_real_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_integer(v) or is_real(v) or is_rational(v))


def _prim_rational_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_integer(v) or is_rational(v))


def _prim_integer_p(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return make_boolean(True)
   if is_real(v):
      r = as_real(v)
      if isinstance(r, float) and r.is_integer():
         return make_boolean(True)
   return make_boolean(False)


def _num_or_error(v, name, app_node):
   if is_integer(v):
      return as_integer(v)
   if is_real(v):
      return as_real(v)
   raise SchemeTypeError(
      '%s: expected number, got non-number value' % name,
      app_node)


def _prim_zero_p(ctx, env, args, app_node):
   return make_boolean(_num_or_error(args[0], 'zero?', app_node) == 0)


def _prim_positive_p(ctx, env, args, app_node):
   return make_boolean(_num_or_error(args[0], 'positive?', app_node) > 0)


def _prim_negative_p(ctx, env, args, app_node):
   return make_boolean(_num_or_error(args[0], 'negative?', app_node) < 0)


def _int_or_error(v, name, app_node):
   if is_integer(v):
      return as_integer(v)
   if is_real(v):
      r = as_real(v)
      if isinstance(r, float) and r.is_integer():
         return int(r)
   raise SchemeTypeError(
      '%s: expected integer, got non-integer value' % name,
      app_node)


def _prim_even_p(ctx, env, args, app_node):
   return make_boolean(_int_or_error(args[0], 'even?', app_node) % 2 == 0)


def _prim_odd_p(ctx, env, args, app_node):
   return make_boolean(_int_or_error(args[0], 'odd?', app_node) % 2 != 0)


def _prim_boolean_p(ctx, env, args, app_node):
   return make_boolean(is_boolean(args[0]))


def _prim_procedure_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(
      is_closure(v) or is_primitive(v) or is_case_closure(v)
      or is_parameter(v) or is_continuation(v)
      or is_record_accessor(v) or is_record_mutator(v))


def _prim_parameter_p(ctx, env, args, app_node):
   return make_boolean(is_parameter(args[0]))


def _prim_error_object_p(ctx, env, args, app_node):
   return make_boolean(is_error_object(args[0]))


def _prim_symbol_p(ctx, env, args, app_node):
   return make_boolean(is_symbol(args[0]))


def _prim_string_p(ctx, env, args, app_node):
   return make_boolean(is_string(args[0]))


def _prim_promise_p(ctx, env, args, app_node):
   return make_boolean(is_promise(args[0]))


def register():
   register_primitive('number?', (1, 1), _prim_number_p,
      doc='Return #t if a is any kind of number.',
      category=CATEGORY)
   register_primitive('complex?', (1, 1), _prim_complex_p,
      doc='Return #t if a is a number.  In the R7RS tower, every number is complex.',
      category=CATEGORY)
   register_primitive('real?', (1, 1), _prim_real_p,
      doc='Return #t if a is a real number (integer, rational, or real).',
      category=CATEGORY)
   register_primitive('rational?', (1, 1), _prim_rational_p,
      doc='Return #t if a is a rational number (integer or rational).',
      category=CATEGORY)
   register_primitive('integer?', (1, 1), _prim_integer_p,
      doc=(
         "Return #t if a is an integer.  A REAL with an integer value\n"
         "(like 3.0) is also considered an integer."),
      category=CATEGORY)
   register_primitive('zero?', (1, 1), _prim_zero_p,
      doc='Return #t if a is numerically equal to zero.',
      category=CATEGORY)
   register_primitive('positive?', (1, 1), _prim_positive_p,
      doc='Return #t if a is strictly greater than zero.',
      category=CATEGORY)
   register_primitive('negative?', (1, 1), _prim_negative_p,
      doc='Return #t if a is strictly less than zero.',
      category=CATEGORY)
   register_primitive('even?', (1, 1), _prim_even_p,
      doc='Return #t if a is an even integer.',
      category=CATEGORY)
   register_primitive('odd?', (1, 1), _prim_odd_p,
      doc='Return #t if a is an odd integer.',
      category=CATEGORY)
   register_primitive('boolean?', (1, 1), _prim_boolean_p,
      doc='Return #t if a is #t or #f.',
      category=CATEGORY)
   register_primitive('procedure?', (1, 1), _prim_procedure_p,
      doc='Return #t if a is callable (a primitive or user-defined closure).',
      category=CATEGORY)
   register_primitive('symbol?', (1, 1), _prim_symbol_p,
      doc='Return #t if a is a symbol.',
      category=CATEGORY)
   register_primitive('string?', (1, 1), _prim_string_p,
      doc='Return #t if a is a string.',
      category=CATEGORY)
   register_primitive('promise?', (1, 1), _prim_promise_p,
      doc='Return #t if a is a promise (created by delay, delay-force, or make-promise).',
      category=CATEGORY)
   register_primitive('parameter?', (1, 1), _prim_parameter_p,
      doc='Return #t if a is a parameter object (created by make-parameter).',
      category=CATEGORY)
   register_primitive('error-object?', (1, 1), _prim_error_object_p,
      doc='Return #t if a is an error object (produced by the error primitive).',
      category=CATEGORY)
