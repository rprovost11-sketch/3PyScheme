"""Logical primitive: not.

`and` and `or` are R7RS special forms (§4.2.1), not primitives, and are
handled in the evaluator.  The only Boolean primitive is `not`.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import make_boolean
from pyscheme.Evaluator import isFalse


CATEGORY = 'logical'


def _prim_not(ctx, env, args, app_node):
   return make_boolean(isFalse(args[0]))


def register():
   register_primitive('not', (1, 1), _prim_not,
      doc=(
         "Return #t if a is #f, and #f otherwise.  In Scheme only #f is\n"
         "considered false; every other value (including 0 and the empty list)\n"
         "is truthy."),
      category=CATEGORY)
