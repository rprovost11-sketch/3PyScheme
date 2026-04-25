"""Bytevector primitives.

R7RS 6.9 - bytevectors are fixed-length sequences of bytes (u8: 0..255).
Backed by a Python bytearray held in a tagged tuple.

Covers: bytevector?, make-bytevector, bytevector, bytevector-length,
bytevector-u8-ref, bytevector-u8-set!, bytevector-copy, bytevector-copy!,
bytevector-append, utf8->string, string->utf8.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   VOID_VALUE,
   is_integer, is_string, is_bytevector,
   as_integer, as_string, as_bytevector_items,
   make_bytevector, make_integer, make_string, make_boolean,
   src_of,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'bytevectors'


def _check_bv(v, name, app_node, idx=1):
   if not is_bytevector(v):
      raise SchemeTypeError(
         '%s: argument %d must be a bytevector' % (name, idx),
         src_of(app_node))
   return as_bytevector_items(v)


def _check_u8(v, name, app_node):
   if not is_integer(v):
      raise SchemeTypeError(
         '%s: byte must be an integer' % name, src_of(app_node))
   n = as_integer(v)
   if n < 0 or n > 255:
      raise SchemeTypeError(
         '%s: byte %d out of u8 range (0..255)' % (name, n),
         src_of(app_node))
   return n


def _check_index(v, name, length, app_node):
   if not is_integer(v):
      raise SchemeTypeError(
         '%s: index must be an integer' % name, src_of(app_node))
   k = as_integer(v)
   if k < 0 or k >= length:
      raise SchemeTypeError(
         '%s: index %d out of range' % (name, k), src_of(app_node))
   return k


def _prim_bytevector_p(ctx, env, args, app_node):
   return make_boolean(is_bytevector(args[0]))


def _prim_make_bytevector(ctx, env, args, app_node):
   if not is_integer(args[0]):
      raise SchemeTypeError(
         'make-bytevector: length must be an integer', src_of(app_node))
   k = as_integer(args[0])
   if k < 0:
      raise SchemeTypeError(
         'make-bytevector: length must be non-negative', src_of(app_node))
   fill = 0
   if len(args) >= 2:
      fill = _check_u8(args[1], 'make-bytevector', app_node)
   return make_bytevector(bytearray([fill] * k))


def _prim_bytevector(ctx, env, args, app_node):
   bs = bytearray(len(args))
   i = 0
   while i < len(args):
      bs[i] = _check_u8(args[i], 'bytevector', app_node)
      i = i + 1
   return make_bytevector(bs)


def _prim_bytevector_length(ctx, env, args, app_node):
   bs = _check_bv(args[0], 'bytevector-length', app_node)
   return make_integer(len(bs))


def _prim_bytevector_u8_ref(ctx, env, args, app_node):
   bs = _check_bv(args[0], 'bytevector-u8-ref', app_node)
   k  = _check_index(args[1], 'bytevector-u8-ref', len(bs), app_node)
   return make_integer(bs[k])


def _prim_bytevector_u8_set(ctx, env, args, app_node):
   bs = _check_bv(args[0], 'bytevector-u8-set!', app_node)
   k  = _check_index(args[1], 'bytevector-u8-set!', len(bs), app_node)
   bs[k] = _check_u8(args[2], 'bytevector-u8-set!', app_node)
   return VOID_VALUE


def _prim_bytevector_copy(ctx, env, args, app_node):
   bs = _check_bv(args[0], 'bytevector-copy', app_node)
   start = 0
   end   = len(bs)
   if len(args) >= 2:
      if not is_integer(args[1]):
         raise SchemeTypeError(
            'bytevector-copy: start must be an integer', src_of(app_node))
      start = as_integer(args[1])
   if len(args) >= 3:
      if not is_integer(args[2]):
         raise SchemeTypeError(
            'bytevector-copy: end must be an integer', src_of(app_node))
      end = as_integer(args[2])
   if start < 0 or end > len(bs) or start > end:
      raise SchemeTypeError(
         'bytevector-copy: range out of bounds', src_of(app_node))
   return make_bytevector(bytearray(bs[start:end]))


def _prim_bytevector_copy_bang(ctx, env, args, app_node):
   dst = _check_bv(args[0], 'bytevector-copy!', app_node, 1)
   if not is_integer(args[1]):
      raise SchemeTypeError(
         'bytevector-copy!: at must be an integer', src_of(app_node))
   at  = as_integer(args[1])
   src = _check_bv(args[2], 'bytevector-copy!', app_node, 3)
   start = 0
   end   = len(src)
   if len(args) >= 4:
      if not is_integer(args[3]):
         raise SchemeTypeError(
            'bytevector-copy!: start must be an integer', src_of(app_node))
      start = as_integer(args[3])
   if len(args) >= 5:
      if not is_integer(args[4]):
         raise SchemeTypeError(
            'bytevector-copy!: end must be an integer', src_of(app_node))
      end = as_integer(args[4])
   if (at < 0 or start < 0 or end > len(src) or start > end
         or at + (end - start) > len(dst)):
      raise SchemeTypeError(
         'bytevector-copy!: range out of bounds', src_of(app_node))
   if dst is src and at > start:
      i = end - start - 1
      while i >= 0:
         dst[at + i] = src[start + i]
         i = i - 1
   else:
      i = 0
      n = end - start
      while i < n:
         dst[at + i] = src[start + i]
         i = i + 1
   return VOID_VALUE


def _prim_bytevector_append(ctx, env, args, app_node):
   parts = bytearray()
   i = 0
   while i < len(args):
      parts.extend(_check_bv(args[i], 'bytevector-append', app_node, i + 1))
      i = i + 1
   return make_bytevector(parts)


def _prim_utf8_to_string(ctx, env, args, app_node):
   bs = _check_bv(args[0], 'utf8->string', app_node)
   start = 0
   end   = len(bs)
   if len(args) >= 2:
      if not is_integer(args[1]):
         raise SchemeTypeError(
            'utf8->string: start must be an integer', src_of(app_node))
      start = as_integer(args[1])
   if len(args) >= 3:
      if not is_integer(args[2]):
         raise SchemeTypeError(
            'utf8->string: end must be an integer', src_of(app_node))
      end = as_integer(args[2])
   if start < 0 or end > len(bs) or start > end:
      raise SchemeTypeError(
         'utf8->string: range out of bounds', src_of(app_node))
   try:
      return make_string(bytes(bs[start:end]).decode('utf-8'))
   except UnicodeDecodeError as e:
      raise SchemeTypeError(
         'utf8->string: invalid UTF-8: ' + str(e), src_of(app_node))


def _prim_string_to_utf8(ctx, env, args, app_node):
   if not is_string(args[0]):
      raise SchemeTypeError(
         'string->utf8: argument must be a string', src_of(app_node))
   s = as_string(args[0])
   start = 0
   end   = len(s)
   if len(args) >= 2:
      if not is_integer(args[1]):
         raise SchemeTypeError(
            'string->utf8: start must be an integer', src_of(app_node))
      start = as_integer(args[1])
   if len(args) >= 3:
      if not is_integer(args[2]):
         raise SchemeTypeError(
            'string->utf8: end must be an integer', src_of(app_node))
      end = as_integer(args[2])
   if start < 0 or end > len(s) or start > end:
      raise SchemeTypeError(
         'string->utf8: range out of bounds', src_of(app_node))
   return make_bytevector(bytearray(s[start:end].encode('utf-8')))


def register():
   register_primitive('bytevector?', (1, 1), _prim_bytevector_p,
      doc='Return #t if obj is a bytevector.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('make-bytevector', (1, 2), _prim_make_bytevector,
      doc=('(make-bytevector k [byte]) returns a bytevector of length k '
           'filled with byte (default 0).  R7RS 6.9.'),
      category=CATEGORY)
   register_primitive('bytevector', (0, None), _prim_bytevector,
      doc='Return a bytevector containing the integer arguments.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('bytevector-length', (1, 1), _prim_bytevector_length,
      doc='Return the number of bytes in the bytevector.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('bytevector-u8-ref', (2, 2), _prim_bytevector_u8_ref,
      doc='(bytevector-u8-ref bv k) returns the kth byte as an integer.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('bytevector-u8-set!', (3, 3), _prim_bytevector_u8_set,
      doc='(bytevector-u8-set! bv k byte) stores byte at index k.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('bytevector-copy', (1, 3), _prim_bytevector_copy,
      doc=('(bytevector-copy bv [start [end]]) returns a copy of (a slice '
           'of) the bytevector.  R7RS 6.9.'),
      category=CATEGORY)
   register_primitive('bytevector-copy!', (3, 5), _prim_bytevector_copy_bang,
      doc=('(bytevector-copy! dst at src [start [end]]) copies bytes from '
           'src[start..end] into dst starting at index at.  R7RS 6.9.'),
      category=CATEGORY)
   register_primitive('bytevector-append', (0, None), _prim_bytevector_append,
      doc='Concatenate bytevector arguments into a new bytevector.  R7RS 6.9.',
      category=CATEGORY)
   register_primitive('utf8->string', (1, 3), _prim_utf8_to_string,
      doc=('(utf8->string bv [start [end]]) decodes UTF-8 bytes to a '
           'string.  R7RS 6.9.'),
      category=CATEGORY)
   register_primitive('string->utf8', (1, 3), _prim_string_to_utf8,
      doc=('(string->utf8 string [start [end]]) encodes a string as a '
           'UTF-8 bytevector.  R7RS 6.9.'),
      category=CATEGORY)
