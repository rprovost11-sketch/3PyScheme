"""Logical operations: not, and, or.

`and` and `or` are R7RS special forms (§4.2.1), not primitives, and are
handled in the evaluator.  Their stubs here exist solely to make them
visible in (help) and (apropos).
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import make_boolean
from pyscheme.Evaluator import isFalse


CATEGORY = 'logical'
_SPECIAL = 'special'


def _prim_not(ctx, env, args, app_node):
    return make_boolean(isFalse(args[0]))


def _form_and(ctx, env, args, app_node):
    raise RuntimeError("'and' is a special form, not a procedure.")


def _form_or(ctx, env, args, app_node):
    raise RuntimeError("'or' is a special form, not a procedure.")


def register():
    register_primitive('not', (1, 1), _prim_not,
                       doc=(
        "Return #t if a is #f, and #f otherwise.  In Scheme only #f is\n"
        "considered false; every other value (including 0 and the empty list)\n"
        "is truthy."),
        category=CATEGORY)

    register_primitive('and', (0, None), _form_and,
                       usage='(and <expr>...)',
                       doc=(
        "Evaluate expressions left to right.  Short-circuit and return #f\n"
        "on the first false expression; otherwise return the last value.  With no\n"
        "arguments, returns #t."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('or', (0, None), _form_or,
                       usage='(or <expr>...)',
                       doc=(
        "Evaluate expressions left to right.  Short-circuit and return\n"
        "the first truthy value; otherwise return #f.  With no arguments, returns #f."),
        category=CATEGORY, kind=_SPECIAL)
