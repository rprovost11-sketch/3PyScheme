"""Control flow: conditionals, sequencing, iteration, continuations, exceptions.

Special forms handled directly by the evaluator; stubs here make them visible
in (help) and (apropos).  Real control procedures (call/cc, raise, values, ...)
are also registered here because they extend or manipulate evaluation flow.

A special-form stub would only be reached by pathological code like
`(apply if ...)`; raising from the stub is the right behavior.
"""

from pyscheme.primitives import register_primitive, _stub
from pyscheme.AST import make_multi_values, src_of
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'control'
_SPECIAL = 'special'


# --- Special-form stubs -------------------------------------------------------

def _form_if(ctx, env, args, app_node):
    _stub('if')


def _form_when(ctx, env, args, app_node):
    _stub('when')


def _form_unless(ctx, env, args, app_node):
    _stub('unless')


def _form_cond(ctx, env, args, app_node):
    _stub('cond')


def _form_case(ctx, env, args, app_node):
    _stub('case')


def _form_do(ctx, env, args, app_node):
    _stub('do')


def _form_begin(ctx, env, args, app_node):
    _stub('begin')


def _form_guard(ctx, env, args, app_node):
    _stub('guard')


def _form_parameterize(ctx, env, args, app_node):
    _stub('parameterize')


# --- Real control primitives --------------------------------------------------
# The evaluator intercepts these at FRAME_CALL dispatch; bodies fire only when
# the interception is bypassed (e.g., via apply re-entering cek_eval).

def _prim_error_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'error: cannot be called through a re-entering path in this '
        'implementation', app_node)


def _prim_raise_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'raise: cannot be called through a re-entering path in this '
        'implementation', app_node)


def _prim_raise_continuable_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'raise-continuable: cannot be called through a re-entering path '
        'in this implementation', app_node)


def _prim_with_exception_handler_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'with-exception-handler: cannot be called through a re-entering '
        'path in this implementation', app_node)


def _prim_guard_eval_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        '%guard-eval: cannot be called through a re-entering '
        'path in this implementation', app_node)


def _prim_values(ctx, env, args, app_node):
    if len(args) == 1:
        return args[0]
    return make_multi_values(list(args), src_of(app_node))


def _prim_call_with_values_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'call-with-values: cannot be called through a re-entering path '
        'in this implementation', app_node)


def _prim_call_cc_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'call/cc: cannot be applied through a re-entering primitive '
        '(apply / call-with-values / force) in this implementation',
        app_node)


def _prim_dynamic_wind_unreached(ctx, env, args, app_node):
    raise SchemeTypeError(
        'dynamic-wind: cannot be applied through a re-entering primitive '
        '(apply / call-with-values / force) in this implementation',
        app_node)


def register():
    # Conditionals
    register_primitive('if', (2, 3), _form_if,
                       usage='(if <test> <then> [<else>])',
                       doc=(
        "Evaluate <test>.  If truthy, evaluate and return <then>; otherwise\n"
        "evaluate and return <else> if present, or an unspecified value if not.\n"
        "In Scheme only #f is false; every other value (including 0 and '()) is\n"
        "truthy."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('when', (2, None), _form_when,
                       usage='(when <test> <body>...)',
                       doc=(
        "If <test> is truthy, evaluate <body> in sequence and return the\n"
        "last value.  Otherwise, return an unspecified value."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('unless', (2, None), _form_unless,
                       usage='(unless <test> <body>...)',
                       doc=(
        "If <test> is false, evaluate <body> in sequence and return the\n"
        "last value.  Otherwise, return an unspecified value."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('cond', (1, None), _form_cond,
                       usage='(cond <clause>...)',
                       doc=(
        "Evaluate clauses in order.  Each clause has one of the forms:\n"
        "(<test>)                -> returns the test value if truthy\n"
        "(<test> <expr>...)      -> evaluates the body if the test is truthy\n"
        "(<test> => <proc>)      -> if truthy, applies <proc> to the test value\n"
        "(else <expr>...)        -> unconditional; must be the last clause\n"
        "If no clause matches, returns an unspecified value."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('case', (2, None), _form_case,
                       usage='(case <key> <clause>...)',
                       doc=(
        "Evaluate <key>, then match its value against each clause's datum\n"
        "list using eqv?.  Each clause has one of the forms:\n"
        "((<datum>...) <expr>...)  -> run body if key matches any datum\n"
        "(else <expr>...)          -> unconditional; must be the last clause\n"
        "Datums are implicitly quoted and are not evaluated.  If no clause\n"
        "matches, returns an unspecified value."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('do', (2, None), _form_do,
                       usage='(do ((<var> <init> [<step>])...) (<test> <result>...) <command>...)',
                       doc=(
        "Iterative construct.  Bind each <var> to its <init>, then repeatedly:\n"
        "  evaluate <test>; if true, evaluate <result> expressions in order and\n"
        "  return the last value (or an unspecified value if <result> is empty);\n"
        "  otherwise evaluate <command>s for effect, evaluate each <step> in\n"
        "  the current env, rebind each <var> to its step's value, then loop.\n"
        "A binding of the form (<var> <init>) keeps <var> at its current value\n"
        "each iteration (no step).  Implemented as a desugar to named let."),
        category=CATEGORY, kind=_SPECIAL)

    # Sequencing
    register_primitive('begin', (1, None), _form_begin,
                       usage='(begin <expr>...)',
                       doc='Evaluate expressions in sequence; return the value of the last one.',
                       category=CATEGORY, kind=_SPECIAL)

    # Exception handling
    register_primitive('guard', (2, None), _form_guard,
                       usage='(guard (<var> <clause>...) <body>...)',
                       doc=(
        "Install an exception handler for <body>.  If raise or error fires\n"
        "during <body>, the handler binds <var> to the raised value and\n"
        "evaluates the <clause>s in cond-like fashion.  Clauses follow the\n"
        "same grammar as cond.  If no clause matches and no else is given,\n"
        "the raised value is re-raised in the outer scope.  R7RS 4.2.7."),
        category=CATEGORY, kind=_SPECIAL)

    register_primitive('error', (1, None), _prim_error_unreached,
                       doc=(
        "Raise a user error.  The first argument is a string message;\n"
        "any trailing arguments are appended to the message as irritants\n"
        "separated by spaces.  Does not return."),
        category=CATEGORY)

    register_primitive('raise', (1, 1), _prim_raise_unreached,
                       doc=(
        "Raise a non-continuable exception carrying the given value.\n"
        "If a with-exception-handler (or guard) handler catches it, the\n"
        "handler's return value is discarded and a secondary error fires\n"
        "because there is no valid continuation to return to.  R7RS 6.11."),
        category=CATEGORY)

    register_primitive('raise-continuable', (1, 1), _prim_raise_continuable_unreached,
                       doc=(
        "Raise a continuable exception carrying the given value.  When\n"
        "caught by with-exception-handler, the handler's return value\n"
        "becomes the return value of (raise-continuable ...).  R7RS 6.11."),
        category=CATEGORY)

    register_primitive('with-exception-handler', (2, 2),
                       _prim_with_exception_handler_unreached,
                       usage='(with-exception-handler <handler> <thunk>)',
                       doc=(
        "Install <handler> for the dynamic extent of (<thunk>).  When a\n"
        "raise or raise-continuable fires inside <thunk>, call <handler>\n"
        "with the raised value.  <handler> is a 1-arg procedure; <thunk>\n"
        "is a 0-arg procedure.  R7RS 6.11."),
        category=CATEGORY)

    register_primitive('%guard-eval', (2, 2),
                       _prim_guard_eval_unreached,
                       doc='Internal: guard body evaluator (FRAME_GUARD, no call/cc chain).')

    # Parameters
    register_primitive('parameterize', (2, None), _form_parameterize,
                       usage='(parameterize ((<param> <val>)...) <body>...)',
                       doc=(
        "Dynamically bind parameter objects for the extent of <body>.\n"
        "Each <param> must evaluate to a parameter (made by make-parameter).\n"
        "Each <val> is bound as the parameter's dynamic value (converted\n"
        "if the parameter has a converter).  When <body> returns, the\n"
        "original values are restored - even if <body> raises.  R7RS 4.2.6."),
        category=CATEGORY, kind=_SPECIAL)

    # Multiple values
    register_primitive('values', (0, None), _prim_values,
                       usage='(values <obj>...)',
                       doc=(
        "Return the arguments as multiple values.  With zero arguments,\n"
        "returns an empty multi-values container.  With one argument,\n"
        "returns that value unchanged (no wrapper).  With two or more,\n"
        "returns a multi-values container that only call-with-values\n"
        "(and a few related forms) can consume; delivering multi-values\n"
        "to a single-value context is an error."),
        category=CATEGORY)

    register_primitive('call-with-values', (2, 2), _prim_call_with_values_unreached,
                       usage='(call-with-values <producer> <consumer>)',
                       doc=(
        "Call <producer> with no arguments.  Pass its return value(s)\n"
        "to <consumer>: if <producer> returned a multi-values container,\n"
        "each value becomes a separate argument to <consumer>; otherwise\n"
        "<consumer> is called with the single value.  Returns <consumer>'s\n"
        "result."),
        category=CATEGORY)

    # First-class continuations
    register_primitive('call-with-current-continuation', (1, 1),
                       _prim_call_cc_unreached,
                       usage='(call-with-current-continuation <proc>)',
                       doc=(
        "Capture the current continuation as a first-class procedure and\n"
        "apply <proc> to it.  Invoking the continuation with zero or more\n"
        "values abandons the current context and returns to call/cc's\n"
        "caller with those values.  R7RS 6.10."),
        category=CATEGORY)

    register_primitive('call/cc', (1, 1), _prim_call_cc_unreached,
                       usage='(call/cc <proc>)',
                       doc='Alias for call-with-current-continuation.',
                       category=CATEGORY)

    register_primitive('dynamic-wind', (3, 3), _prim_dynamic_wind_unreached,
                       usage='(dynamic-wind <before> <thunk> <after>)',
                       doc=(
        "Call <before> for effect, then <thunk> for value, then <after>\n"
        "for effect.  The after thunk runs whether <thunk> returns normally\n"
        "or control leaves via a continuation invocation or an exception.\n"
        "If the dynamic extent is later re-entered via a continuation,\n"
        "<before> runs again.  R7RS 6.10."),
        category=CATEGORY)
