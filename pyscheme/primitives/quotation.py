"""Documentation stubs for quotation special forms.

All forms here are handled by the evaluator, not as primitives.  Stubs make
them visible in (help) and (apropos).
"""

from pyscheme.primitives import register_primitive


CATEGORY = 'quotation'
_SPECIAL = 'special'


def _stub(form_name):
    raise RuntimeError(
        repr(form_name) + ' is a special form, not a procedure; it cannot be '
        'applied as a first-class value.  This stub exists only to carry '
        'documentation into the help system.')


def _form_quote(ctx, env, args, app_node):
    _stub('quote')


def _form_quasiquote(ctx, env, args, app_node):
    _stub('quasiquote')


def _form_unquote(ctx, env, args, app_node):
    _stub('unquote')


def _form_unquote_splicing(ctx, env, args, app_node):
    _stub('unquote-splicing')


def register():
    register_primitive('quote', (1, 1), _form_quote,
                       usage='(quote <datum>)',
                       doc="Return the datum unevaluated.  'x is shorthand for (quote x).",
                       category=CATEGORY, kind=_SPECIAL)

    register_primitive('quasiquote', (1, 1), _form_quasiquote,
                       usage='(quasiquote <template>)',
                       doc=(
        "Return a value shaped like <template>, but with any (unquote e)\n"
        "holes replaced by the value of e, and any (unquote-splicing e)\n"
        "holes expanded by splicing the elements of e's list into the\n"
        "surrounding list.  Reader syntax: `x == (quasiquote x),\n"
        ",e == (unquote e), ,@e == (unquote-splicing e).  R7RS 4.2.8."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('unquote', (1, 1), _form_unquote,
                       usage='(unquote <expr>)',
                       doc="Unquote marker, valid only inside a quasiquote template.",
                       category=CATEGORY, kind=_SPECIAL)

    register_primitive('unquote-splicing', (1, 1), _form_unquote_splicing,
                       usage='(unquote-splicing <expr>)',
                       doc="Splicing-unquote marker, valid only inside a quasiquote template.",
                       category=CATEGORY, kind=_SPECIAL)
