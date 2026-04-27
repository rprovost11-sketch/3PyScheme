"""Numeric comparison primitives: =, <, >, <=, >=.

R7RS 6.2.6: each accepts one or more arguments.  The predicate is true
iff the arguments are (respectively) equal, strictly increasing,
strictly decreasing, non-decreasing, or non-increasing.  With a single
argument, all are vacuously #t.
"""

from fractions import Fraction

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_integer, is_real, is_rational, is_complex, is_exact_complex,
   as_integer, as_real, as_rational_num, as_rational_den,
   as_complex_real, as_complex_imag,
   as_exact_complex_real, as_exact_complex_imag,
   make_boolean,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'comparison'


def _num(v, name, app_node, i):
   """Extract a real-valued Python number.  Errors on complex (not ordered)."""
   if is_integer(v):
      return as_integer(v)
   if is_rational(v):
      return Fraction(as_rational_num(v), as_rational_den(v))
   if is_real(v):
      return as_real(v)
   raise SchemeTypeError(
      '%s: argument %d is not a number' % (name, i),
      app_node)


def _num_eq(v, name, app_node, i):
   """Extract any Python number including complex (for = only).
   Exact complex is converted to Python complex for comparison so that
   (= 3+4i 3.0+4.0i) works correctly."""
   if is_integer(v):
      return as_integer(v)
   if is_rational(v):
      return Fraction(as_rational_num(v), as_rational_den(v))
   if is_real(v):
      return as_real(v)
   if is_complex(v):
      return complex(as_complex_real(v), as_complex_imag(v))
   if is_exact_complex(v):
      re = as_exact_complex_real(v)
      im = as_exact_complex_imag(v)
      re_py = as_integer(re) if is_integer(re) else Fraction(as_rational_num(re), as_rational_den(re))
      im_py = as_integer(im) if is_integer(im) else Fraction(as_rational_num(im), as_rational_den(im))
      return complex(float(re_py), float(im_py))
   raise SchemeTypeError(
      '%s: argument %d is not a number' % (name, i),
      app_node)


def _prim_num_eq(ctx, env, args, app_node):
   prev = _num_eq(args[0], '=', app_node, 1)
   i = 1
   while i < len(args):
      cur = _num_eq(args[i], '=', app_node, i + 1)
      if prev != cur:
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_num_lt(ctx, env, args, app_node):
   prev = _num(args[0], '<', app_node, 1)
   i = 1
   while i < len(args):
      cur = _num(args[i], '<', app_node, i + 1)
      if not (prev < cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_num_gt(ctx, env, args, app_node):
   prev = _num(args[0], '>', app_node, 1)
   i = 1
   while i < len(args):
      cur = _num(args[i], '>', app_node, i + 1)
      if not (prev > cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_num_le(ctx, env, args, app_node):
   prev = _num(args[0], '<=', app_node, 1)
   i = 1
   while i < len(args):
      cur = _num(args[i], '<=', app_node, i + 1)
      if not (prev <= cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_num_ge(ctx, env, args, app_node):
   prev = _num(args[0], '>=', app_node, 1)
   i = 1
   while i < len(args):
      cur = _num(args[i], '>=', app_node, i + 1)
      if not (prev >= cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def register():
   register_primitive('=', (1, None), _prim_num_eq,
      doc='Return #t if all arguments are numerically equal.',
      category=CATEGORY)
   register_primitive('<', (1, None), _prim_num_lt,
      doc='Return #t if the arguments are monotonically strictly increasing.',
      category=CATEGORY)
   register_primitive('>', (1, None), _prim_num_gt,
      doc='Return #t if the arguments are monotonically strictly decreasing.',
      category=CATEGORY)
   register_primitive('<=', (1, None), _prim_num_le,
      doc='Return #t if the arguments are monotonically non-decreasing.',
      category=CATEGORY)
   register_primitive('>=', (1, None), _prim_num_ge,
      doc='Return #t if the arguments are monotonically non-increasing.',
      category=CATEGORY)
