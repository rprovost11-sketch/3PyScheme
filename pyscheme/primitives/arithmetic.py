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
import cmath
from fractions import Fraction

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_integer, is_real, is_rational, is_complex,
   is_string,
   as_integer, as_real, as_rational_num, as_rational_den,
   as_complex_real, as_complex_imag, as_string,
   make_integer, make_real, make_rational, make_complex,
   make_boolean, make_string, make_multi_values,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'arithmetic'


def _num(v, name, app_node, i):
   """Extract a Python real-numeric value (int, Fraction, float).
   Raises for non-numeric or complex (complex not valid for ordering ops)."""
   if is_integer(v):
      return as_integer(v)
   if is_rational(v):
      return Fraction(as_rational_num(v), as_rational_den(v))
   if is_real(v):
      return as_real(v)
   raise SchemeTypeError(
      '%s: argument %d is not a number' % (name, i),
      app_node)


def _any_num(v, name, app_node, i):
   """Extract a Python numeric value (int, Fraction, float, or complex)."""
   if is_integer(v):
      return as_integer(v)
   if is_rational(v):
      return Fraction(as_rational_num(v), as_rational_den(v))
   if is_real(v):
      return as_real(v)
   if is_complex(v):
      return complex(as_complex_real(v), as_complex_imag(v))
   raise SchemeTypeError(
      '%s: argument %d is not a number' % (name, i),
      app_node)


def _check_int(v, name, app_node, i):
   if is_integer(v):
      return as_integer(v)
   raise SchemeTypeError(
      '%s: argument %d is not an integer' % (name, i),
      app_node)


def _check_int_x(v, name, app_node, i):
   """Like _check_int but accepts inexact integer-valued reals.
   Returns (int_value, is_inexact)."""
   if is_integer(v):
      return as_integer(v), False
   if is_real(v):
      fv = as_real(v)
      if math.isfinite(fv) and fv == math.trunc(fv):
         return int(fv), True
   raise SchemeTypeError(
      '%s: argument %d is not an integer' % (name, i),
      app_node)


def _wrap(n):
   """Wrap a Python number back into a tagged value."""
   if isinstance(n, bool):
      n = int(n)
   if isinstance(n, int):
      return make_integer(n)
   if isinstance(n, Fraction):
      if n.denominator == 1:
         return make_integer(n.numerator)
      return make_rational(n.numerator, n.denominator)
   if isinstance(n, float):
      return make_real(n)
   if isinstance(n, complex):
      if n.imag == 0.0:
         return make_real(n.real)
      return make_complex(n.real, n.imag)
   raise TypeError('internal: arithmetic result has unexpected type ' + str(type(n)))


def _prim_add(ctx, env, args, app_node):
   total = 0
   i = 0
   while i < len(args):
      total = total + _any_num(args[i], '+', app_node, i + 1)
      i = i + 1
   return _wrap(total)


def _prim_sub(ctx, env, args, app_node):
   if len(args) == 1:
      return _wrap(-_any_num(args[0], '-', app_node, 1))
   result = _any_num(args[0], '-', app_node, 1)
   i = 1
   while i < len(args):
      result = result - _any_num(args[i], '-', app_node, i + 1)
      i = i + 1
   return _wrap(result)


def _prim_mul(ctx, env, args, app_node):
   result = 1
   i = 0
   while i < len(args):
      result = result * _any_num(args[i], '*', app_node, i + 1)
      i = i + 1
   return _wrap(result)


def _exact_div(a, b):
   """Divide two numbers.
   - Complex input: use Python complex division.
   - Both exact (int or Fraction): return exact result (Fraction or int).
   - Either inexact (float): return float; float zero divisor -> +-inf.
   """
   if isinstance(a, complex) or isinstance(b, complex):
      ca = a if isinstance(a, complex) else complex(float(a))
      cb = b if isinstance(b, complex) else complex(float(b))
      return ca / cb
   if isinstance(b, float):
      fb = b
      fa = float(a)
      if fb == 0.0:
         if math.isnan(fa) or fa == 0.0:
            return float('nan')
         return math.copysign(float('inf'), fa)
      return fa / fb
   if isinstance(a, float):
      return a / float(b)
   # Both exact: use Fraction for exact rational arithmetic.
   return Fraction(a) / Fraction(b)


def _prim_div(ctx, env, args, app_node):
   if len(args) == 1:
      n = _any_num(args[0], '/', app_node, 1)
      if isinstance(n, int) and n == 0:
         raise SchemeTypeError('/: division by zero', app_node)
      try:
         return _wrap(_exact_div(1, n))
      except ZeroDivisionError:
         raise SchemeTypeError('/: division by zero', app_node)
   result = _any_num(args[0], '/', app_node, 1)
   i = 1
   while i < len(args):
      divisor = _any_num(args[i], '/', app_node, i + 1)
      if isinstance(divisor, int) and divisor == 0:
         raise SchemeTypeError('/: division by zero', app_node)
      try:
         result = _exact_div(result, divisor)
      except ZeroDivisionError:
         raise SchemeTypeError('/: division by zero', app_node)
      i = i + 1
   return _wrap(result)


def _prim_abs(ctx, env, args, app_node):
   return _wrap(abs(_any_num(args[0], 'abs', app_node, 1)))


def _trunc_div(n, d):
   """R7RS quotient truncates toward zero.  Python // rounds toward -inf,
   so adjust for mixed signs with a non-zero remainder."""
   if (n < 0) != (d < 0) and n % d != 0:
      return -(-n // d)
   return n // d


def _prim_quotient(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'quotient', app_node, 1)
   d, di = _check_int_x(args[1], 'quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError('quotient: division by zero', app_node)
   r = _trunc_div(n, d)
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_remainder(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'remainder', app_node, 1)
   d, di = _check_int_x(args[1], 'remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError('remainder: division by zero', app_node)
   r = n - d * _trunc_div(n, d)
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_modulo(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'modulo', app_node, 1)
   d, di = _check_int_x(args[1], 'modulo', app_node, 2)
   if d == 0:
      raise SchemeTypeError('modulo: division by zero', app_node)
   r = n % d
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_min(ctx, env, args, app_node):
   result = _num(args[0], 'min', app_node, 1)
   any_real = is_real(args[0])
   i = 1
   while i < len(args):
      v = _num(args[i], 'min', app_node, i + 1)
      if is_real(args[i]):
         any_real = True
      if isinstance(v, float) and math.isnan(v):
         result = v
      elif not (isinstance(result, float) and math.isnan(result)):
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
      if isinstance(v, float) and math.isnan(v):
         result = v
      elif not (isinstance(result, float) and math.isnan(result)):
         if v > result:
            result = v
      i = i + 1
   if any_real and not isinstance(result, float):
      result = float(result)
   return _wrap(result)


def _prim_gcd(ctx, env, args, app_node):
   if len(args) == 0:
      return make_integer(0)
   first, inexact = _check_int_x(args[0], 'gcd', app_node, 1)
   result = abs(first)
   i = 1
   while i < len(args):
      v, vi = _check_int_x(args[i], 'gcd', app_node, i + 1)
      inexact = inexact or vi
      result = math.gcd(result, abs(v))
      i = i + 1
   return make_real(float(result)) if inexact else make_integer(result)


def _prim_lcm(ctx, env, args, app_node):
   if len(args) == 0:
      return make_integer(1)
   first, inexact = _check_int_x(args[0], 'lcm', app_node, 1)
   result = abs(first)
   i = 1
   while i < len(args):
      v, vi = _check_int_x(args[i], 'lcm', app_node, i + 1)
      inexact = inexact or vi
      if result == 0 or v == 0:
         result = 0
      else:
         result = result * abs(v) // math.gcd(result, abs(v))
      i = i + 1
   return make_real(float(result)) if inexact else make_integer(result)


_MACH_EPS = 2.2204460492503131e-16


def _clean_complex(c):
   """Snap near-zero real part when imaginary part is exactly ±1.0."""
   if abs(c.imag) == 1.0 and abs(c.real) <= _MACH_EPS:
      return complex(0.0, c.imag)
   return c


def _prim_expt(ctx, env, args, app_node):
   base = _any_num(args[0], 'expt', app_node, 1)
   exp  = _any_num(args[1], 'expt', app_node, 2)
   if isinstance(base, complex) or isinstance(exp, complex):
      cb = base if isinstance(base, complex) else complex(float(base))
      if isinstance(exp, int):
         return _wrap(cb ** exp)
      ce = exp if isinstance(exp, complex) else complex(float(exp))
      return _wrap(_clean_complex(cb ** ce))
   if isinstance(exp, int) and isinstance(base, (int, Fraction)):
      if exp >= 0:
         return _wrap(Fraction(base) ** exp)
      # Negative exact exponent: 1 / base^(-exp)
      pos = Fraction(base) ** (-exp)
      if pos == 0:
         raise SchemeTypeError('expt: division by zero', args[1])
      return _wrap(Fraction(1, 1) / pos)
   fb = float(base)
   fe = float(exp)
   try:
      result = fb ** fe
      if isinstance(result, complex):
         result = _clean_complex(result)
      return _wrap(result)
   except ValueError:
      return _wrap(_clean_complex(complex(fb, 0.0) ** complex(fe, 0.0)))


def _prim_sqrt(ctx, env, args, app_node):
   v = _any_num(args[0], 'sqrt', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.sqrt(v))
   if isinstance(v, int) and v >= 0:
      r = math.isqrt(v)
      if r * r == v:
         return make_integer(r)
   if isinstance(v, Fraction) and v >= 0:
      rn = math.isqrt(v.numerator)
      rd = math.isqrt(v.denominator)
      if rn * rn == v.numerator and rd * rd == v.denominator:
         return _wrap(Fraction(rn, rd))
   fv = float(v)
   if fv == 0.0:
      return make_real(0.0)
   if fv < 0.0:
      return _wrap(cmath.sqrt(complex(fv, 0.0)))
   return make_real(math.sqrt(fv))


def _prim_square(ctx, env, args, app_node):
   v = _any_num(args[0], 'square', app_node, 1)
   return _wrap(v * v)


def _prim_floor(ctx, env, args, app_node):
   v = _num(args[0], 'floor', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   if isinstance(v, Fraction):
      return make_integer(math.floor(v))
   if not math.isfinite(v):
      return make_real(v)
   return make_real(float(math.floor(v)))


def _prim_ceiling(ctx, env, args, app_node):
   v = _num(args[0], 'ceiling', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   if isinstance(v, Fraction):
      return make_integer(math.ceil(v))
   if not math.isfinite(v):
      return make_real(v)
   return make_real(float(math.ceil(v)))


def _prim_truncate(ctx, env, args, app_node):
   v = _num(args[0], 'truncate', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   if isinstance(v, Fraction):
      return make_integer(math.trunc(v))
   if not math.isfinite(v):
      return make_real(v)
   return make_real(float(math.trunc(v)))


def _prim_round(ctx, env, args, app_node):
   v = _num(args[0], 'round', app_node, 1)
   if isinstance(v, int):
      return make_integer(v)
   if isinstance(v, Fraction):
      return make_integer(round(v))
   if not math.isfinite(v):
      return make_real(v)
   # R7RS specifies banker's rounding (round half to even); Python's round() does this.
   return make_real(float(round(v)))


def _prim_exact_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_integer(v) or is_rational(v))


def _prim_inexact_p(ctx, env, args, app_node):
   v = args[0]
   return make_boolean(is_real(v) or is_complex(v))


def _prim_exact(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v) or is_rational(v):
      return v
   if is_real(v):
      f = as_real(v)
      if not math.isfinite(f):
         raise SchemeTypeError(
            'exact: no exact representation for non-finite real', app_node)
      if f.is_integer():
         return make_integer(int(f))
      frac = Fraction(f)
      return make_rational(frac.numerator, frac.denominator)
   if is_complex(v):
      raise SchemeTypeError(
         'exact: no exact representation for complex number', app_node)
   raise SchemeTypeError(
      'exact: argument must be a number', app_node)


def _prim_inexact(ctx, env, args, app_node):
   v = args[0]
   if is_real(v) or is_complex(v):
      return v
   if is_integer(v):
      return make_real(float(as_integer(v)))
   if is_rational(v):
      return make_real(float(Fraction(as_rational_num(v), as_rational_den(v))))
   raise SchemeTypeError(
      'inexact: argument must be a number', app_node)


def _prim_exact_integer_p(ctx, env, args, app_node):
   return make_boolean(is_integer(args[0]))


def _prim_numerator(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return v
   if is_rational(v):
      return make_integer(as_rational_num(v))
   if is_real(v):
      f = as_real(v)
      if not math.isfinite(f):
         raise SchemeTypeError('numerator: argument must be finite', app_node)
      if f.is_integer():
         return make_real(f)
      return make_real(float(Fraction(f).numerator))
   raise SchemeTypeError('numerator: argument must be a real number', app_node)


def _prim_denominator(ctx, env, args, app_node):
   v = args[0]
   if is_integer(v):
      return make_integer(1)
   if is_rational(v):
      return make_integer(as_rational_den(v))
   if is_real(v):
      f = as_real(v)
      if not math.isfinite(f):
         raise SchemeTypeError('denominator: argument must be finite', app_node)
      if f.is_integer():
         return make_real(1.0)
      return make_real(float(Fraction(f).denominator))
   raise SchemeTypeError('denominator: argument must be a real number', app_node)


def _format_num_str(f):
   """Format a float as R7RS surface syntax (used by number->string for complex)."""
   if math.isnan(f):
      return '+nan.0'
   if f == float('inf'):
      return '+inf.0'
   if f == float('-inf'):
      return '-inf.0'
   s = repr(f)
   if '.' not in s and 'e' not in s:
      s = s + '.0'
   return s


def _parse_number_for_stn(s):
   """Parse a real-number string for string->number complex parsing."""
   if s == '+inf.0':
      return float('inf')
   if s == '-inf.0':
      return float('-inf')
   if s == '+nan.0':
      return float('nan')
   try:
      return float(s)
   except ValueError:
      pass
   return None


def _parse_complex_for_stn(s):
   """Parse a+bi style string into (re_float, im_float) or None."""
   body = s[:-1]   # strip 'i'
   if not body:
      return None
   split = -1
   j = len(body) - 1
   while j >= 0:
      c = body[j]
      if c == '+' or c == '-':
         if j > 0 and body[j - 1].lower() == 'e':
            j = j - 1
            continue
         split = j
         break
      j = j - 1
   if split == -1:
      return None
   real_str  = body[:split]
   sign_imag = body[split:]
   if real_str == '':
      re_val = 0.0
   else:
      re_val = _parse_number_for_stn(real_str)
      if re_val is None:
         return None
   if sign_imag == '+':
      im_val = 1.0
   elif sign_imag == '-':
      im_val = -1.0
   else:
      im_val = _parse_number_for_stn(sign_imag)
      if im_val is None:
         return None
   return (float(re_val), float(im_val))


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
   if is_rational(v):
      n = as_rational_num(v)
      d = as_rational_den(v)
      if radix == 10:
         return make_string(str(n) + '/' + str(d))
      raise SchemeTypeError(
         'number->string: only radix 10 supported for rational numbers',
         app_node)
   if is_complex(v):
      if radix != 10:
         raise SchemeTypeError(
            'number->string: only radix 10 supported for complex numbers',
            app_node)
      re = as_complex_real(v)
      im = as_complex_imag(v)
      re_s = _format_num_str(re)
      im_s = _format_num_str(im)
      if math.isnan(im) or im >= 0:
         return make_string(re_s + '+' + im_s + 'i')
      return make_string(re_s + im_s + 'i')
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
   n, ni = _check_int_x(args[0], 'floor-quotient', app_node, 1)
   d, di = _check_int_x(args[1], 'floor-quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'floor-quotient: divide by zero', app_node)
   r = n // d
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_floor_remainder(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'floor-remainder', app_node, 1)
   d, di = _check_int_x(args[1], 'floor-remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'floor-remainder: divide by zero', app_node)
   r = n % d
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_floor_div(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'floor/', app_node, 1)
   d, di = _check_int_x(args[1], 'floor/', app_node, 2)
   if d == 0:
      raise SchemeTypeError('floor/: divide by zero', app_node)
   q = n // d
   r = n % d
   if ni or di:
      return make_multi_values([make_real(float(q)), make_real(float(r))])
   return make_multi_values([make_integer(q), make_integer(r)])


def _prim_truncate_quotient(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'truncate-quotient', app_node, 1)
   d, di = _check_int_x(args[1], 'truncate-quotient', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'truncate-quotient: divide by zero', app_node)
   r = _trunc_div(n, d)
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_truncate_remainder(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'truncate-remainder', app_node, 1)
   d, di = _check_int_x(args[1], 'truncate-remainder', app_node, 2)
   if d == 0:
      raise SchemeTypeError(
         'truncate-remainder: divide by zero', app_node)
   r = n - _trunc_div(n, d) * d
   return make_real(float(r)) if ni or di else make_integer(r)


def _prim_truncate_div(ctx, env, args, app_node):
   n, ni = _check_int_x(args[0], 'truncate/', app_node, 1)
   d, di = _check_int_x(args[1], 'truncate/', app_node, 2)
   if d == 0:
      raise SchemeTypeError('truncate/: divide by zero', app_node)
   q = _trunc_div(n, d)
   r = n - q * d
   if ni or di:
      return make_multi_values([make_real(float(q)), make_real(float(r))])
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
   # Strip #b/#o/#d/#x/#e/#i prefix(es) from the string.
   explicit_radix = False
   exact = -1   # -1 = unspecified; 1 = exact; 0 = inexact
   while len(s) >= 2 and s[0] == '#':
      ch = s[1].lower()
      if ch == 'b':
         if explicit_radix:
            return make_boolean(False)
         radix = 2
         explicit_radix = True
         s = s[2:]
      elif ch == 'o':
         if explicit_radix:
            return make_boolean(False)
         radix = 8
         explicit_radix = True
         s = s[2:]
      elif ch == 'd':
         if explicit_radix:
            return make_boolean(False)
         radix = 10
         explicit_radix = True
         s = s[2:]
      elif ch == 'x':
         if explicit_radix:
            return make_boolean(False)
         radix = 16
         explicit_radix = True
         s = s[2:]
      elif ch == 'e':
         if exact != -1:
            return make_boolean(False)
         exact = 1
         s = s[2:]
      elif ch == 'i':
         if exact != -1:
            return make_boolean(False)
         exact = 0
         s = s[2:]
      else:
         return make_boolean(False)
   if not s:
      return make_boolean(False)
   # Try polar literal (radix 10 only, contains '@').
   if radix == 10 and '@' in s:
      at = s.index('@')
      r_str     = s[:at]
      theta_str = s[at + 1:]
      if r_str and theta_str:
         r_val     = _parse_number_for_stn(r_str)
         theta_val = _parse_number_for_stn(theta_str)
         if r_val is not None and theta_val is not None:
            if exact == 1:
               return make_boolean(False)
            re_val = r_val * math.cos(theta_val)
            im_val = r_val * math.sin(theta_val)
            return make_complex(float(re_val), float(im_val))
   # Try complex literal (radix 10 only, ends in 'i').
   if radix == 10 and s.endswith('i') and len(s) >= 2:
      tup = _parse_complex_for_stn(s)
      if tup is not None:
         if exact == 1:
            return make_boolean(False)   # exact complex not yet supported
         return make_complex(tup[0], tup[1])
   # Try integer.
   try:
      n = int(s, radix)
      if exact == 0:
         return make_real(float(n))
      return make_integer(n)
   except (ValueError, TypeError):
      pass
   # Try rational n/d.
   if '/' in s:
      slash = s.index('/')
      num_str = s[:slash]
      den_str = s[slash + 1:]
      try:
         num = int(num_str, radix)
         den = int(den_str, radix)
         if den != 0:
            frac = Fraction(num, den)
            if exact == 0:
               return make_real(float(frac))
            if frac.denominator == 1:
               return make_integer(frac.numerator)
            return make_rational(frac.numerator, frac.denominator)
      except (ValueError, TypeError):
         pass
   # Try real (radix 10 only).
   if radix == 10:
      if s == '+inf.0':
         if exact == 1:
            return make_boolean(False)
         return make_real(float('inf'))
      if s == '-inf.0':
         if exact == 1:
            return make_boolean(False)
         return make_real(float('-inf'))
      if s == '+nan.0':
         if exact == 1:
            return make_boolean(False)
         return make_real(float('nan'))
      try:
         f = float(s)
         if exact == 1:
            if not math.isfinite(f):
               return make_boolean(False)
            if f.is_integer():
               return make_integer(int(f))
            frac = Fraction(f)
            return make_rational(frac.numerator, frac.denominator)
         return make_real(f)
      except (ValueError, TypeError):
         pass
   return make_boolean(False)


def _prim_exp(ctx, env, args, app_node):
   v = _any_num(args[0], 'exp', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.exp(v))
   try:
      return make_real(math.exp(float(v)))
   except OverflowError:
      return make_real(float('inf'))


def _prim_log(ctx, env, args, app_node):
   v = _any_num(args[0], 'log', app_node, 1)
   if isinstance(v, complex):
      result = cmath.log(v)
      if len(args) >= 2:
         base = _any_num(args[1], 'log', app_node, 2)
         b = base if isinstance(base, complex) else complex(float(base))
         result = result / cmath.log(b)
      return _wrap(result)
   f = float(v)
   if f == 0.0:
      r = float('-inf')
   elif f < 0.0:
      result = cmath.log(complex(f, 0.0))
      if len(args) >= 2:
         base = _any_num(args[1], 'log', app_node, 2)
         b = base if isinstance(base, complex) else complex(float(base))
         result = result / cmath.log(b)
      return _wrap(result)
   else:
      r = math.log(f)
   if len(args) >= 2:
      base = _any_num(args[1], 'log', app_node, 2)
      b = float(base)
      if b == 0.0 or b < 0.0:
         r = float('nan')
      else:
         r = r / math.log(b)
   return make_real(r)


def _prim_sin(ctx, env, args, app_node):
   v = _any_num(args[0], 'sin', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.sin(v))
   return make_real(math.sin(float(v)))


def _prim_cos(ctx, env, args, app_node):
   v = _any_num(args[0], 'cos', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.cos(v))
   return make_real(math.cos(float(v)))


def _prim_tan(ctx, env, args, app_node):
   v = _any_num(args[0], 'tan', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.tan(v))
   return make_real(math.tan(float(v)))


def _prim_asin(ctx, env, args, app_node):
   v = _any_num(args[0], 'asin', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.asin(v))
   f = float(v)
   if f < -1.0 or f > 1.0:
      return _wrap(cmath.asin(complex(f, 0.0)))
   return make_real(math.asin(f))


def _prim_acos(ctx, env, args, app_node):
   v = _any_num(args[0], 'acos', app_node, 1)
   if isinstance(v, complex):
      return _wrap(cmath.acos(v))
   f = float(v)
   if f < -1.0 or f > 1.0:
      return _wrap(cmath.acos(complex(f, 0.0)))
   return make_real(math.acos(f))


def _prim_atan(ctx, env, args, app_node):
   v = _any_num(args[0], 'atan', app_node, 1)
   if isinstance(v, complex):
      if len(args) >= 2:
         raise SchemeTypeError(
            'atan: two-argument form requires real numbers', app_node)
      return _wrap(cmath.atan(v))
   if len(args) >= 2:
      v2 = _any_num(args[1], 'atan', app_node, 2)
      return make_real(math.atan2(float(v), float(v2)))
   return make_real(math.atan(float(v)))


def _as_real_val(v, name, app_node, idx):
   """Extract v as a Python float for complex construction."""
   if is_integer(v):
      return float(as_integer(v))
   if is_rational(v):
      return float(Fraction(as_rational_num(v), as_rational_den(v)))
   if is_real(v):
      return as_real(v)
   raise SchemeTypeError(
      '%s: argument %d is not a real number' % (name, idx), app_node)


def _prim_make_rectangular(ctx, env, args, app_node):
   re = _as_real_val(args[0], 'make-rectangular', app_node, 1)
   im = _as_real_val(args[1], 'make-rectangular', app_node, 2)
   return make_complex(re, im)


def _prim_make_polar(ctx, env, args, app_node):
   r     = _as_real_val(args[0], 'make-polar', app_node, 1)
   theta = _as_real_val(args[1], 'make-polar', app_node, 2)
   return make_complex(r * math.cos(theta), r * math.sin(theta))


def _prim_real_part(ctx, env, args, app_node):
   v = args[0]
   if is_complex(v):
      return make_real(as_complex_real(v))
   if is_integer(v) or is_rational(v):
      return v
   if is_real(v):
      return v
   raise SchemeTypeError('real-part: argument must be a number', app_node)


def _prim_imag_part(ctx, env, args, app_node):
   v = args[0]
   if is_complex(v):
      return make_real(as_complex_imag(v))
   if is_integer(v) or is_rational(v):
      return make_integer(0)
   if is_real(v):
      return make_real(0.0)
   raise SchemeTypeError('imag-part: argument must be a number', app_node)


def _prim_magnitude(ctx, env, args, app_node):
   v = args[0]
   if is_complex(v):
      re = as_complex_real(v)
      im = as_complex_imag(v)
      return make_real(math.hypot(re, im))
   return _prim_abs(ctx, env, args, app_node)


def _prim_angle(ctx, env, args, app_node):
   v = args[0]
   if is_complex(v):
      return make_real(math.atan2(as_complex_imag(v), as_complex_real(v)))
   n = _num(v, 'angle', app_node, 1)
   if isinstance(n, float) and math.isnan(n):
      return make_real(float('nan'))
   if n >= 0:
      return make_real(0.0)
   return make_real(math.pi)


def _simplest_rational(lo, hi):
   """Return simplest rational in [lo, hi] (lo, hi Fractions, 0 < lo <= hi)."""
   if lo == hi:
      return lo
   n = math.ceil(lo)
   if Fraction(n) <= hi:
      return Fraction(n)
   return Fraction(1) / _simplest_rational(Fraction(1) / hi, Fraction(1) / lo)


def _rationalize_exact(x, delta):
   """Return simplest rational y s.t. |x - y| <= delta (Fractions)."""
   lo = x - delta
   hi = x + delta
   if lo <= 0 and hi >= 0:
      return Fraction(0)
   if lo > 0:
      return _simplest_rational(lo, hi)
   return -_simplest_rational(-hi, -lo)


def _prim_rationalize(ctx, env, args, app_node):
   x     = _num(args[0], 'rationalize', app_node, 1)
   delta = _num(args[1], 'rationalize', app_node, 2)
   inexact = isinstance(x, float) or isinstance(delta, float)
   if isinstance(delta, float):
      if math.isinf(delta):
         return make_real(0.0) if inexact else make_integer(0)
      if math.isnan(delta):
         return make_real(float('nan'))
   if isinstance(x, float) and not math.isfinite(x):
      return make_real(x)
   fx = Fraction(x)
   fd = abs(Fraction(delta))
   r = _rationalize_exact(fx, fd)
   if inexact:
      return _wrap(float(r))
   return _wrap(r)


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
           'a real.  Negative real input returns a complex result.  R7RS 6.2.6.'),
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
      doc='Return #t if obj is an inexact number (real or complex).',
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
   register_primitive('exp', (1, 1), _prim_exp,
      doc='Return e raised to the power z.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('log', (1, 2), _prim_log,
      doc=('(log z) returns the natural log.  (log z w) returns log base w.\n'
           '(log 0) returns -inf.0.  Negative real inputs return +nan.0 in '
           'the real-only tower.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('sin', (1, 1), _prim_sin,
      doc='Return the sine of z (radians).  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('cos', (1, 1), _prim_cos,
      doc='Return the cosine of z (radians).  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('tan', (1, 1), _prim_tan,
      doc='Return the tangent of z (radians).  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('asin', (1, 1), _prim_asin,
      doc='Return the arcsine of z in radians.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('acos', (1, 1), _prim_acos,
      doc='Return the arccosine of z in radians.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('atan', (1, 2), _prim_atan,
      doc=('(atan z) returns the arctangent.  (atan y x) returns atan2(y,x).\n'
           'R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('make-rectangular', (2, 2), _prim_make_rectangular,
      doc='(make-rectangular x y) constructs a complex number x+yi.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('make-polar', (2, 2), _prim_make_polar,
      doc='(make-polar r theta) constructs a complex from polar form r*e^(i*theta).',
      category=CATEGORY)
   register_primitive('real-part', (1, 1), _prim_real_part,
      doc='Return the real part of z.  For real z, returns z itself.',
      category=CATEGORY)
   register_primitive('imag-part', (1, 1), _prim_imag_part,
      doc='Return the imaginary part of z.  For real z, returns 0 (exact) or 0.0.',
      category=CATEGORY)
   register_primitive('magnitude', (1, 1), _prim_magnitude,
      doc='Return the magnitude (absolute value) of z.  R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('angle', (1, 1), _prim_angle,
      doc='Return the angle of z in radians.  For positive real: 0.0; negative: pi.',
      category=CATEGORY)
   register_primitive('rationalize', (2, 2), _prim_rationalize,
      doc=('(rationalize x delta) returns the simplest rational y such that '
           '|x - y| <= delta.  R7RS 6.2.6.'),
      category=CATEGORY)
   register_primitive('exact->inexact', (1, 1), _prim_inexact,
      doc='Alias for inexact.  R6RS / R7RS 6.2.6.',
      category=CATEGORY)
   register_primitive('inexact->exact', (1, 1), _prim_exact,
      doc='Alias for exact.  R6RS / R7RS 6.2.6.',
      category=CATEGORY)
