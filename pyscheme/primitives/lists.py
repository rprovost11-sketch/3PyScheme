"""List and pair primitives: cons, car, cdr, pair?, null?, list."""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   alloc_cons, NIL_VALUE, is_cons, is_nil, src_of, make_boolean,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'lists'


def _prim_cons(ctx, env, args, app_node):
   return alloc_cons(args[0], args[1], None)


def _prim_car(ctx, env, args, app_node):
   v = args[0]
   if not is_cons(v):
      raise SchemeTypeError('car: expected pair, got non-pair value',
                            src_of(app_node))
   return v.car


def _prim_cdr(ctx, env, args, app_node):
   v = args[0]
   if not is_cons(v):
      raise SchemeTypeError('cdr: expected pair, got non-pair value',
                            src_of(app_node))
   return v.cdr


def _prim_pair_p(ctx, env, args, app_node):
   return make_boolean(is_cons(args[0]))


def _prim_null_p(ctx, env, args, app_node):
   return make_boolean(is_nil(args[0]))


def _prim_list(ctx, env, args, app_node):
   result = NIL_VALUE
   i = len(args) - 1
   while i >= 0:
      result = alloc_cons(args[i], result, None)
      i = i - 1
   return result


def _prim_append(ctx, env, args, app_node):
   # (append list1 list2 ... listN) - all but the last must be proper
   # lists; the last may be any value and forms the final cdr.
   if not args:
      return NIL_VALUE
   if len(args) == 1:
      return args[0]
   collected = []
   i = 0
   while i < len(args) - 1:
      cur = args[i]
      while is_cons(cur):
         collected.append(cur.car)
         cur = cur.cdr
      if not is_nil(cur):
         raise SchemeTypeError(
            'append: non-last argument must be a proper list', app_node)
      i = i + 1
   tail = args[len(args) - 1]
   j = len(collected) - 1
   while j >= 0:
      tail = alloc_cons(collected[j], tail, None)
      j = j - 1
   return tail


def register():
   register_primitive('cons', (2, 2), _prim_cons,
      doc=(
         "Return a newly allocated pair whose car is a and whose cdr is b.\n"
         "The pair is guaranteed to be different (in the sense of eq?) from every\n"
         "existing object."),
      category=CATEGORY)
   register_primitive('car', (1, 1), _prim_car,
      doc=(
         "Return the contents of the car field of the pair a.  Signals an\n"
         "error if a is not a pair."),
      category=CATEGORY)
   register_primitive('cdr', (1, 1), _prim_cdr,
      doc=(
         "Return the contents of the cdr field of the pair a.  Signals an\n"
         "error if a is not a pair."),
      category=CATEGORY)
   register_primitive('pair?', (1, 1), _prim_pair_p,
      doc=(
         "Return #t if a is a pair, #f otherwise.  The empty list is not\n"
         "considered a pair."),
      category=CATEGORY)
   register_primitive('null?', (1, 1), _prim_null_p,
      doc='Return #t if a is the empty list (), #f otherwise.',
      category=CATEGORY)
   register_primitive('list', (0, None), _prim_list,
      doc=(
         "Return a newly allocated list containing the arguments in order.\n"
         "With no arguments, returns the empty list."),
      category=CATEGORY)
   register_primitive('append', (0, None), _prim_append,
      doc=(
         "Return a list that is the concatenation of its arguments.  All\n"
         "but the last argument must be proper lists; the last may be any\n"
         "value (forming the final cdr).  R7RS 6.4."),
      category=CATEGORY)
