"""Documentation stubs for macro definition special forms.

All forms here are handled by the Expander, not as primitives.  Stubs make
them visible in (help) and (apropos).
"""

from pyscheme.primitives import register_primitive


CATEGORY = 'macros'
_SPECIAL = 'special'


def _stub(form_name):
    raise RuntimeError(
        repr(form_name) + ' is a special form, not a procedure; it cannot be '
        'applied as a first-class value.  This stub exists only to carry '
        'documentation into the help system.')


def _form_define_syntax(ctx, env, args, app_node):
    _stub('define-syntax')


def _form_let_syntax(ctx, env, args, app_node):
    _stub('let-syntax')


def _form_letrec_syntax(ctx, env, args, app_node):
    _stub('letrec-syntax')


def _form_syntax_rules(ctx, env, args, app_node):
    _stub('syntax-rules')


def register():
    register_primitive('define-syntax', (2, 2), _form_define_syntax,
                       usage='(define-syntax <name> <transformer>)',
                       doc=(
        "Bind <name> to a syntax transformer at expand time.  <transformer>\n"
        "must be a (syntax-rules ...) form.  Subsequent occurrences of\n"
        "<name> in head position will be expanded by the transformer.\n"
        "R7RS 4.3."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('let-syntax', (2, None), _form_let_syntax,
                       usage='(let-syntax ((<name> <transformer>)...) <body>...)',
                       doc=(
        "Locally bind one or more syntax transformers for the duration\n"
        "of <body>.  Each transformer's definition env is the enclosing\n"
        "scope (siblings are NOT visible to each other).  R7RS 4.3."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('letrec-syntax', (2, None), _form_letrec_syntax,
                       usage='(letrec-syntax ((<name> <transformer>)...) <body>...)',
                       doc=(
        "Like let-syntax, but each transformer's definition env includes\n"
        "its sibling transformers, so mutually recursive macros can be\n"
        "defined in one form.  R7RS 4.3."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('syntax-rules', (1, None), _form_syntax_rules,
                       usage='(syntax-rules [<ellipsis>] (<literal>...) (<pattern> <template>)...)',
                       doc=(
        "Create a syntax transformer.  Each <pattern> matches the use-site\n"
        "form structurally; the first matching rule's <template> is\n"
        "substituted with captured pattern variables.  Literals match\n"
        "themselves; '...' repeats the preceding pattern.  R7RS 4.3."),
        category=CATEGORY, kind=_SPECIAL)
