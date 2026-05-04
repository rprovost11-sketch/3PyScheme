"""Character primitives.

R7RS 6.6 - characters.  Covers char?, char=?, char<?, char<=?, char>?,
char>=?, char-alphabetic?, char-numeric?, char-whitespace?, char-upper-
case?, char-lower-case?, char-upcase, char-downcase, char->integer,
integer->char, digit-value.

Our character values store a single Python str of length 1; comparisons
and predicates delegate to Python's str methods.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_character, is_integer,
   as_character, as_integer,
   make_character, make_integer, make_boolean,
   src_of,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'chars'


def _check_char(v, name, app_node, idx=1):
   if not is_character(v):
      raise SchemeTypeError(
         '%s: argument %d must be a character' % (name, idx), src_of(app_node))
   return as_character(v)


def _prim_char_p(ctx, env, args, app_node):
   return make_boolean(is_character(args[0]))


def _char_compare(name, args, app_node, op):
   prev = _check_char(args[0], name, app_node, 1)
   i = 1
   while i < len(args):
      cur = _check_char(args[i], name, app_node, i + 1)
      if not op(prev, cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_char_eq(ctx, env, args, app_node):
   return _char_compare('char=?', args, app_node, lambda a, b: a == b)


def _prim_char_lt(ctx, env, args, app_node):
   return _char_compare('char<?', args, app_node, lambda a, b: a < b)


def _prim_char_le(ctx, env, args, app_node):
   return _char_compare('char<=?', args, app_node, lambda a, b: a <= b)


def _prim_char_gt(ctx, env, args, app_node):
   return _char_compare('char>?', args, app_node, lambda a, b: a > b)


def _prim_char_ge(ctx, env, args, app_node):
   return _char_compare('char>=?', args, app_node, lambda a, b: a >= b)


def _prim_char_alphabetic(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-alphabetic?', app_node)
   return make_boolean(c.isalpha())


def _prim_char_numeric(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-numeric?', app_node)
   return make_boolean(c.isdigit())


def _prim_char_whitespace(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-whitespace?', app_node)
   return make_boolean(c.isspace())


def _prim_char_upper_case(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-upper-case?', app_node)
   return make_boolean(c.isupper())


def _prim_char_lower_case(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-lower-case?', app_node)
   return make_boolean(c.islower())


def _prim_char_upcase(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-upcase', app_node)
   return make_character(c.upper())


def _prim_char_downcase(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-downcase', app_node)
   return make_character(c.lower())


def _prim_char_foldcase(ctx, env, args, app_node):
   c = _check_char(args[0], 'char-foldcase', app_node)
   return make_character(c.casefold())


def _char_compare_ci(name, args, app_node, op):
   prev = _check_char(args[0], name, app_node, 1).casefold()
   i = 1
   while i < len(args):
      cur = _check_char(args[i], name, app_node, i + 1).casefold()
      if not op(prev, cur):
         return make_boolean(False)
      prev = cur
      i = i + 1
   return make_boolean(True)


def _prim_char_ci_eq(ctx, env, args, app_node):
   return _char_compare_ci('char-ci=?', args, app_node, lambda a, b: a == b)


def _prim_char_ci_lt(ctx, env, args, app_node):
   return _char_compare_ci('char-ci<?', args, app_node, lambda a, b: a < b)


def _prim_char_ci_le(ctx, env, args, app_node):
   return _char_compare_ci('char-ci<=?', args, app_node, lambda a, b: a <= b)


def _prim_char_ci_gt(ctx, env, args, app_node):
   return _char_compare_ci('char-ci>?', args, app_node, lambda a, b: a > b)


def _prim_char_ci_ge(ctx, env, args, app_node):
   return _char_compare_ci('char-ci>=?', args, app_node, lambda a, b: a >= b)


def _prim_char_to_integer(ctx, env, args, app_node):
   c = _check_char(args[0], 'char->integer', app_node)
   return make_integer(ord(c))


def _prim_integer_to_char(ctx, env, args, app_node):
   v = args[0]
   if not is_integer(v):
      raise SchemeTypeError(
         'integer->char: argument must be an integer', src_of(app_node))
   n = as_integer(v)
   if n < 0 or n > 0x10FFFF or (0xD800 <= n <= 0xDFFF):
      raise SchemeTypeError(
         'integer->char: code point out of range', src_of(app_node))
   return make_character(chr(n))


def _prim_digit_value(ctx, env, args, app_node):
   c = _check_char(args[0], 'digit-value', app_node)
   if c.isdecimal():
      return make_integer(int(c))
   return make_boolean(False)


def register():
   register_primitive('char?', (1, 1), _prim_char_p,
      doc='Return #t if obj is a character.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char=?', (2, None), _prim_char_eq,
      doc='Return #t if all character arguments compare equal.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char<?', (2, None), _prim_char_lt,
      doc='Return #t if characters are in strictly ascending order.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char<=?', (2, None), _prim_char_le,
      doc='Return #t if characters are in non-descending order.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char>?', (2, None), _prim_char_gt,
      doc='Return #t if characters are in strictly descending order.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char>=?', (2, None), _prim_char_ge,
      doc='Return #t if characters are in non-ascending order.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-alphabetic?', (1, 1), _prim_char_alphabetic,
      doc='Return #t if char is a letter.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-numeric?', (1, 1), _prim_char_numeric,
      doc='Return #t if char is a digit.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-whitespace?', (1, 1), _prim_char_whitespace,
      doc='Return #t if char is whitespace.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-upper-case?', (1, 1), _prim_char_upper_case,
      doc='Return #t if char is an upper-case letter.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-lower-case?', (1, 1), _prim_char_lower_case,
      doc='Return #t if char is a lower-case letter.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-upcase', (1, 1), _prim_char_upcase,
      doc='Return char\'s upper-case equivalent.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-downcase', (1, 1), _prim_char_downcase,
      doc='Return char\'s lower-case equivalent.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-foldcase', (1, 1), _prim_char_foldcase,
      doc='Return char\'s case-folded equivalent (Unicode full case folding).  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-ci=?', (2, None), _prim_char_ci_eq,
      doc='Case-insensitive char=?.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-ci<?', (2, None), _prim_char_ci_lt,
      doc='Case-insensitive char<?.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-ci<=?', (2, None), _prim_char_ci_le,
      doc='Case-insensitive char<=?.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-ci>?', (2, None), _prim_char_ci_gt,
      doc='Case-insensitive char>?.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char-ci>=?', (2, None), _prim_char_ci_ge,
      doc='Case-insensitive char>=?.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('char->integer', (1, 1), _prim_char_to_integer,
      doc='Return char\'s Unicode code point as an integer.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('integer->char', (1, 1), _prim_integer_to_char,
      doc='Return the character with the given Unicode code point.  R7RS 6.6.',
      category=CATEGORY)
   register_primitive('digit-value', (1, 1), _prim_digit_value,
      doc=('Return the numeric value of a digit character, or #f if char '
           'is not a digit.  R7RS 6.6.'),
      category=CATEGORY)
