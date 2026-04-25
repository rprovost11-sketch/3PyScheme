"""Arithmetic primitives: +, -, *, /, abs, quotient, remainder, modulo,
min, max, gcd, lcm, expt, sqrt, square, floor, ceiling, truncate,
round, exact?, inexact?, exact, inexact, exact->inexact,
inexact->exact, exact-integer?, numerator, denominator,
number->string, string->number.

First-cut numeric tower: INTEGER and REAL only.  Results follow Python's
own promotion (int + int -> int; anything + float -> float), wrapped back
into the tagged value.  RATIONAL and COMPLEX support can be layered in
without changing the API.
"""

import math

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_integer, is_real, is_string, as_integer, as_real, as_string,
   make_integer, make_real, make_boolean, make_string, make_multi_values,
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


def _prim_gcd(ctx, env, args, app_node):
   if len(args) == 0:
      return make_integer(0)
   result = abs(_check_int(args[0], 'gcd', app_node, 1))
   i = 1
   while i < len(args):
      result = math.gcd(result, abs(_check_int(args[i], 'gcd', app_node, i + 1)))
      i = i + 1
   return make_integer(result)


def _prim_lcm(ctx, env, args, app_node):
   if len(args) == 0:
      return make_integer(1)
   result = abs(_check_int(args[0], 'lcm', app_node, 1))
   i = 1
   while i < len(args):
      v = abs(_check_int(args[i], 'lcm', app_node, i + 1))
      if result == 0 or v == 0:
         result = 0
      else:
         result = result * v // math.gcd(result, v)
      i = i + 1
   return make_integer(result)


def _prim_expt(ctx, env, args, app_node):
   base = _num(args[0], 'expt', app_node, 1)
   exp  = _num(args[1], 'expt', app_node, 2)
   if isinstance(base, int) and isinstance(exp, int) and exp >= 0:
      return make_integer(base ** exp)
   return _wrap(float(base) ** float(exp))


def _prim_sqrt(ctx, env, args, app_node):
   v = _num(args[0], 'sqrt', app_node, 1)
   if isinstance(v, int) and v >= 0:
      r = math.isqrt(v)
      if r * r == v:
         return make_integer(r)
   return make_real(math.sqrt(float(v)))


def _prim_square(ctx, env, args, app_node):
   v = _num(args[0], 'square', app_node, 1)
   return _wrap(v * v)


def _prim_floor(ctx, env, args, app_node):
   v = _num(args[0], 'floor', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   return make_real(math.floor(v))


def _prim_ceiling(ctx, env, args, app_node):
   v = _num(args[0], 'ceiling', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   return make_real(math.ceil(v))


def _prim_truncate(ctx, env, args, app_node):
   v = _num(args[0], 'truncate', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   return make_real(math.trunc(v))


def _prim_round(ctx, env, args, app_node):
   v = _num(args[0], 'round', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   # R7RS specifies banker's rounding (round half to even); Python's round() does this.
   return make_real(round(v))


def _prim_exact_p(ctx, env, args, app_node):
   return make_boolean(is_integer(args[0]))


def _prim_inexact_p(ctx, env, args, app_node):
   return make_boolean(is_real(args[0]))


def _prim_exact(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return v
   if is_real(v):
      f = as_real(v)
      if f.is_integer():
         return make_integer(int(f))
      raise SchemeTypeError(
         'exact: cannot convert non-integer real to exact', app_node)
   raise SchemeTypeError(
      'exact: argument must be a number', app_node)


def _prim_inexact(ctx, env, args, app_node):
   v = args[0]
   if is_real(v):
      return v
   if is_integer(v):
      return make_real(float(as_integer(v)))
   raise SchemeTypeError(
      'inexact: argument must be a number', app_node)


def _prim_exact_integer_p(ctx, env, args, app_node):
   return make_boolean(is_integer(args[0]))


def _prim_numerator(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return v
   if is_real(v):
      f = as_real(v)
      if f.is_integer():
         return make_real(f)
   raise SchemeTypeError(
      'numerator: argument must be an integer or integer-valued real',
      app_node)


def _prim_denominator(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return make_integer(1)
   if is_real(v):
      f = as_real(v)
      if f.is_integer():
         return make_real(1.0)
   raise SchemeTypeError(
      'denominator: argument must be an integer or integer-valued real',
      app_node)


def _prim_number_to_string(ctx, env, args, app_node):
   v = args[0]
   radix = 10
   if len(args) >= 2:
      r = args[1]
      if not is_integer(r):
         raise SchemeTypeError(
            'number->string: radix must be an integer', app_node)
      radix = as_integer(r)
   if is_integer(v):
      n = as_integer(v)
      if radix == 10:
         return make_string(str(n))
      if radix == 2:
         return make_string(bin(n)[2:] if n >= 0 else '-' + bin(-n)[2:])
      if radix == 8:
         return make_string(oct(n)[2:] if n >= 0 else '-' + oct(-n)[2:])
      if radix == 16:
         return make_string(hex(n)[2:] if n >= 0 else '-' + hex(-n)[2:])
      raise SchemeTypeError(
         'number->string: radix must be 2, 8, 10, or 16', app_node)
   if is_real(v):
      if radix != 10:
         raise SchemeTypeError(
            'number->string: only radix 10 is supported for inexact numbers',
            app_node)
      f = as_real(v)
      if f != f:
         return make_string('+nan.0')
      if f == float('inf'):
         return make_string('+inf.0')
      if f == float('-inf'):
         return make_string('-inf.0')
      s = repr(f)
      if '.' not in s and 'e' not in s:
         s = s + '.0'
      return make_string(s)
   raise SchemeTypeError(
      'number->string: argument must be a number', app_node)


def _floor_div(n, d):
   """Floor division with R7RS sign convention: result has the sign of d.
   Python's // is floor division for ints, matching Scheme floor/."""
   if d == 0:
      return None
   return n // d


def _floor_mod(n, d):
   if d == 0:
      return None
   return n % d


def _prim_floor_quotient(ctx, env, args, app_node):
   n = _check_int(args[0], 'floor-quotient', app_node, 1)
   d = _check_int(args[1], 'floor-quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'floor-quotient: divide by zero', app_node)
   return make_integer(n // d)


def _prim_floor_remainder(ctx, env, args, app_node):
   n = _check_int(args[0], 'floor-remainder', app_node, 1)
   d = _check_int(args[1], 'floor-remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'floor-remainder: divide by zero', app_node)
   return make_integer(n % d)


def _prim_floor_div(ctx, env, args, app_node):
   n = _check_int(args[0], 'floor/', app_node, 1)
   d = _check_int(args[1], 'floor/', app_node, 2)
   if d == 0:
      raise SchemeTypeError('floor/: divide by zero', app_node)
   return make_multi_values([make_integer(n // d), make_integer(n % d)])


def _prim_truncate_quotient(ctx, env, args, app_node):
   n = _check_int(args[0], 'truncate-quotient', app_node, 1)
   d = _check_int(args[1], 'truncate-quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'truncate-quotient: divide by zero', app_node)
   return make_integer(_trunc_div(n, d))


def _prim_truncate_remainder(ctx, env, args, app_node):
   n = _check_int(args[0], 'truncate-remainder', app_node, 1)
   d = _check_int(args[1], 'truncate-remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'truncate-remainder: divide by zero', app_node)
   return make_integer(n - _trunc_div(n, d) * d)


def _prim_truncate_div(ctx, env, args, app_node):
   n = _check_int(args[0], 'truncate/', app_node, 1)
   d = _check_int(args[1], 'truncate/', app_node, 2)
   if d == 0:
      raise SchemeTypeError('truncate/: divide by zero', app_node)
   q = _trunc_div(n, d)
   r = n - q * d
   return make_multi_values([make_integer(q), make_integer(r)])


def _prim_exact_integer_sqrt(ctx, env, args, app_node):
   n = _check_int(args[0], 'exact-integer-sqrt', app_node, 1)
   if n < 0:
      raise SchemeTypeError(
         'exact-integer-sqrt: argument must be non-negative', app_node)
   r = math.isqrt(n)
   return make_multi_values([make_integer(r), make_integer(n - r * r)])


def _prim_features(ctx, env, args, app_node):
   from pyscheme.AST import alloc_cons, NIL_VALUE, make_symbol
   from pyscheme.Expander import _FEATURES
   names = list(_FEATURES.keys())
   result = NIL_VALUE
   i = len(names) - 1
   while i >= 0:
      result = alloc_cons(make_symbol(names[i]), result, None)
      i = i - 1
   return result


def _prim_string_to_number(ctx, env, args, app_node):
   v = args[0]
   if not is_string(v):
      raise SchemeTypeError(
         'string->number: first argument must be a string', app_node)
   s = as_string(v).strip()
   radix = 10
   if len(args) >= 2:
      r = args[1]
      if not is_integer(r):
         raise SchemeTypeError(
            'string->number: radix must be an integer', app_node)
      radix = as_integer(r)
   try:
      return make_integer(int(s, radix))
   except (ValueError, TypeError):
      pass
   if radix == 10:
      try:
         return make_real(float(s))
      except (ValueError, TypeError):
         pass
   return make_boolean(False)


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
   register_primitive('gcd', (0, None), _prim_gcd,
      doc=('Return the greatest common divisor of the integer arguments.  '
           'With zero arguments, returns 0.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('lcm', (0, None), _prim_lcm,
      doc=('Return the least common multiple of the integer arguments.  '
           'With zero arguments, returns 1.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('expt', (2, 2), _prim_expt,
      doc=('Return base raised to the exponent.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('sqrt', (1, 1), _prim_sqrt,
      doc=('Return the principal square root of the argument.  Returns an '
           'exact integer when the argument is a perfect square; otherwise '
           'a real.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('square', (1, 1), _prim_square,
      doc='Return (* x x).  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('floor', (1, 1), _prim_floor,
      doc='Return the largest integer not greater than x.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('ceiling', (1, 1), _prim_ceiling,
      doc='Return the smallest integer not less than x.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('truncate', (1, 1), _prim_truncate,
      doc='Return the integer part of x (toward zero).  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('round', (1, 1), _prim_round,
      doc=('Return the integer closest to x; ties go to the nearest even '
           'integer (banker\'s rounding).  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('exact?', (1, 1), _prim_exact_p,
      doc='Return #t if obj is an exact number (an integer in our impl).',
      category=CATEGORY)
   register_primitive('inexact?', (1, 1), _prim_inexact_p,
      doc='Return #t if obj is an inexact number (a real in our impl).',
      category=CATEGORY)
   register_primitive('exact', (1, 1), _prim_exact,
      doc=('Convert a number to its exact form.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('inexact', (1, 1), _prim_inexact,
      doc=('Convert a number to its inexact form.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('exact-integer?', (1, 1), _prim_exact_integer_p,
      doc='Return #t if obj is an exact integer.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('numerator', (1, 1), _prim_numerator,
      doc='Return the numerator of a rational number.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('denominator', (1, 1), _prim_denominator,
      doc='Return the denominator of a rational number.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('number->string', (1, 2), _prim_number_to_string,
      doc=('(number->string number [radix]) returns a string representation. '
           'Radix may be 2, 8, 10, or 16; default 10.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('string->number', (1, 2), _prim_string_to_number,
      doc=('(string->number string [radix]) parses a string; returns the '
           'number on success, #f on failure.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('floor-quotient', (2, 2), _prim_floor_quotient,
      doc=('Integer floor division.  Result has the sign of the divisor.  '
           'R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('floor-remainder', (2, 2), _prim_floor_remainder,
      doc=('Integer floor remainder.  Result has the sign of the divisor.  '
           'R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('floor/', (2, 2), _prim_floor_div,
      doc=('(floor/ n d) returns two values: the floor quotient and '
           'remainder.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('truncate-quotient', (2, 2), _prim_truncate_quotient,
      doc=('Integer truncate division (toward zero).  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('truncate-remainder', (2, 2), _prim_truncate_remainder,
      doc=('Integer truncate remainder.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('truncate/', (2, 2), _prim_truncate_div,
      doc=('(truncate/ n d) returns two values: the truncate quotient '
           'and remainder.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('exact-integer-sqrt', (1, 1), _prim_exact_integer_sqrt,
      doc=('(exact-integer-sqrt n) returns two values: the integer floor '
           'of sqrt(n) and the difference n - r*r.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('features', (0, 0), _prim_features,
      doc=('Return a list of feature identifiers supported by this '
           'implementation.  R7RS 5.6.2.'),
      category=CATEGORY)
