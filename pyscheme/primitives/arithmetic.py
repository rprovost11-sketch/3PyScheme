"""Arithmetic primitives: +, -, *, /, abs, quotient, remainder, modulo,
min, max.

First-cut numeric tower: INTEGER and REAL only.  Results follow Python's
own promotion (int + int -> int; anything + float -> float), wrapped back
into the tagged value.  RATIONAL and COMPLEX support can be layered in
without changing the API.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_integer, is_real, as_integer, as_real, make_integer, make_real,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'arithmetic'


def _num(v, name, app_node, i):
   """Extract the Python numeric value from v, or raise."""
   if is_integer(v):
      return as_integer(v)
   if is_real(v):
      return as_real(v)
   raise SchemeTypeError(
      '%s: argument %d is not a number' % (name, i),
      app_node)


def _check_int(v, name, app_node, i):
   if is_integer(v):
      return as_integer(v)
   raise SchemeTypeError(
      '%s: argument %d is not an integer' % (name, i),
      app_node)


def _wrap(n):
   """Wrap a Python number back into a tagged value."""
   if isinstance(n, bool):
      # bool is a subclass of int in Python; coerce explicitly so
      # arithmetic-on-boolean doesn't accidentally yield an INTEGER.
      n = int(n)
   if isinstance(n, int):
      return make_integer(n)
   if isinstance(n, float):
      return make_real(n)
   raise TypeError('internal: arithmetic result has unexpected type ' + str(type(n)))


def _prim_add(ctx, env, args, app_node):
   total = 0
   i = 0
   while i < len(args):
      total = total + _num(args[i], '+', app_node, i + 1)
      i = i + 1
   return _wrap(total)


def _prim_sub(ctx, env, args, app_node):
   if len(args) == 1:
      return _wrap(-_num(args[0], '-', app_node, 1))
   result = _num(args[0], '-', app_node, 1)
   i = 1
   while i < len(args):
      result = result - _num(args[i], '-', app_node, i + 1)
      i = i + 1
   return _wrap(result)


def _prim_mul(ctx, env, args, app_node):
   result = 1
   i = 0
   while i < len(args):
      result = result * _num(args[i], '*', app_node, i + 1)
      i = i + 1
   return _wrap(result)


def _exact_div(a, b):
   """Divide two numbers.  If both are int and the result is exact,
   return an int; otherwise return a float.  Callers ensure b != 0."""
   if isinstance(a, int) and isinstance(b, int) and a % b == 0:
      return a // b
   return a / b


def _prim_div(ctx, env, args, app_node):
   if len(args) == 1:
      n = _num(args[0], '/', app_node, 1)
      if n == 0:
         raise SchemeTypeError('/: division by zero', app_node)
      return _wrap(_exact_div(1, n))
   result = _num(args[0], '/', app_node, 1)
   i = 1
   while i < len(args):
      divisor = _num(args[i], '/', app_node, i + 1)
      if divisor == 0:
         raise SchemeTypeError('/: division by zero', app_node)
      result = _exact_div(result, divisor)
      i = i + 1
   return _wrap(result)


def _prim_abs(ctx, env, args, app_node):
   return _wrap(abs(_num(args[0], 'abs', app_node, 1)))


def _trunc_div(n, d):
   """R7RS quotient truncates toward zero.  Python // rounds toward -inf,
   so adjust for mixed signs with a non-zero remainder."""
   if (n < 0) != (d < 0) and n % d != 0:
      return -(-n // d)
   return n // d


def _prim_quotient(ctx, env, args, app_node):
   n = _check_int(args[0], 'quotient', app_node, 1)
   d = _check_int(args[1], 'quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError('quotient: division by zero', app_node)
   return make_integer(_trunc_div(n, d))


def _prim_remainder(ctx, env, args, app_node):
   n = _check_int(args[0], 'remainder', app_node, 1)
   d = _check_int(args[1], 'remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError('remainder: division by zero', app_node)
   # R7RS remainder has the sign of the dividend n.
   r = n - d * _trunc_div(n, d)
   return make_integer(r)


def _prim_modulo(ctx, env, args, app_node):
   n = _check_int(args[0], 'modulo', app_node, 1)
   d = _check_int(args[1], 'modulo', app_node, 2)
   if d == 0:
      raise SchemeTypeError('modulo: division by zero', app_node)
   # R7RS modulo has the sign of the divisor d.  Python % already does this.
   return make_integer(n % d)


def _prim_min(ctx, env, args, app_node):
   result = _num(args[0], 'min', app_node, 1)
   any_real = is_real(args[0])
   i = 1
   while i < len(args):
      v = _num(args[i], 'min', app_node, i + 1)
      if is_real(args[i]):
         any_real = True
      if v < result:
         result = v
      i = i + 1
   if any_real and not isinstance(result, float):
      result = float(result)
   return _wrap(result)


def _prim_max(ctx, env, args, app_node):
   result = _num(args[0], 'max', app_node, 1)
   any_real = is_real(args[0])
   i = 1
   while i < len(args):
      v = _num(args[i], 'max', app_node, i + 1)
      if is_real(args[i]):
         any_real = True
      if v > result:
         result = v
      i = i + 1
   if any_real and not isinstance(result, float):
      result = float(result)
   return _wrap(result)


def register():
   register_primitive('+', (0, None), _prim_add,
      doc=(
         "Return the sum of the arguments.  With no arguments, returns 0.\n"
         "Integer addition produces INTEGER; any real-valued argument produces REAL."),
      category=CATEGORY)
   register_primitive('-', (1, None), _prim_sub,
      doc=(
         "With one argument, return its additive inverse.\n"
         "With multiple arguments, return the first argument minus the sum of the rest."),
      category=CATEGORY)
   register_primitive('*', (0, None), _prim_mul,
      doc='Return the product of the arguments.  With no arguments, returns 1.',
      category=CATEGORY)
   register_primitive('/', (1, None), _prim_div,
      doc=(
         "With one argument, return its multiplicative inverse.\n"
         "With multiple arguments, return the first argument divided by the product of\n"
         "the rest.  Integer-by-integer division returns an INTEGER when the quotient\n"
         "is exact, otherwise a REAL."),
      category=CATEGORY)
   register_primitive('abs', (1, 1), _prim_abs,
      doc='Return the absolute value of a.',
      category=CATEGORY)
   register_primitive('quotient', (2, 2), _prim_quotient,
      doc=(
         "Integer division of a by b truncated toward zero.  Both args must\n"
         "be integers; result is an integer."),
      category=CATEGORY)
   register_primitive('remainder', (2, 2), _prim_remainder,
      doc=(
         "Integer remainder with the sign of the dividend (a).  Satisfies\n"
         "(= a (+ (* b (quotient a b)) (remainder a b)))."),
      category=CATEGORY)
   register_primitive('modulo', (2, 2), _prim_modulo,
      doc='Integer modulo with the sign of the divisor (b).',
      category=CATEGORY)
   register_primitive('min', (1, None), _prim_min,
      doc=(
         "Return the smallest of its numeric arguments.  If any argument is\n"
         "inexact (a REAL), the result is inexact."),
      category=CATEGORY)
   register_primitive('max', (1, None), _prim_max,
      doc=(
         "Return the largest of its numeric arguments.  If any argument is\n"
         "inexact (a REAL), the result is inexact."),
      category=CATEGORY)
