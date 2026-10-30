"""Semantic analyzer: validates expanded S-expressions and resolves
their variable references to lexical addresses.

Pipeline position:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> Evaluator.cek_eval

The Analyzer takes fully-expanded S-expressions (cons cells + atoms)
from the Expander, walks the tree, and returns a rewritten copy (see
"Lexical addressing" below); the input tree is never edited.  Its job
is threefold:

    1. Shape validation - reject malformed special forms so the
       Evaluator can trust the structures it dispatches on.
    2. Static arity check - reject calls whose argument count
       cannot match the operator's arity.
    3. Lexical addressing - resolve each variable reference to the
       LA key (depth, index, name) the Evaluator looks it up by.

Shape rules enforced (matches R7RS where applicable):

    - lambda's parameter list must be a list/identifier of unique names
    - define needs exactly (define name value)
    - set! needs exactly (set! name value)
    - if needs (if test then) or (if test then else)
    - let bindings must be ((var val) ...)
    - cond clauses must be (test body...) or (test) or (test => proc)
      or (else body...) (last)
    - body forms (lambda, when, unless, begin, let, ...) must be non-empty

Anything that doesn't match a special form is treated as an
application; arg count is checked when the operator's arity is known.

Static arity checking:

    When the operator of an application is an immediate lambda
    literal, a known primitive, or a variable whose arity is tracked
    in the static environment (top-level lambda defines), the
    Analyzer checks the argument count at analyze time and raises
    SchemeArityError on mismatch - same error class as the runtime
    check in Evaluator, so callers see the same message regardless
    of when it fires.

    The static environment is a dict { name: (min, max) } threaded
    through analyze.  max=None means variadic.  A param shadowing an
    outer name maps to None in the extended env, which suppresses
    checks through that name (its runtime arity is unknown).
    Primitives seed the env via primitives.PRIMITIVE_ARITIES.

Lexical addressing:

    The Analyzer threads a compile-time scope chain that mirrors the
    chain of local scopes the Evaluator builds at run time (see the
    table above _resolve).  The rewritten tree differs from the input
    in two ways only:

    - A variable reference -- including a set! target -- becomes a
      resolved symbol (AST.resolve_symbol): still a symbol to every
      reader, carrying its LA key.  Quoted data, parameter and binding
      names, case datums, else / => and special-form heads stay plain.
    - A let, let*, letrec, letrec* or named let gets a scope-names node
      (AST.make_scope_names) right after its head, holding the names of
      the scopes it creates, so the Evaluator does not rebuild them on
      every run.  Printers skip it.

    The Evaluator still resolves a plain (unanalyzed) symbol by name,
    and runs a let-family form without the node, so code that never
    passed through here keeps working.

Public API:
    analyze(sexpr, static_env=None, scope=None)
        Returns the rewritten tree on success, or raises
        SchemeAnalysisError / SchemeArityError.
        static_env defaults to a fresh dict seeded with PRIMITIVE_ARITIES.
        scope is the compile-time scope chain the code will run in:
        None for a dict scope (top level, library, eval environment).

    scope_of_env(env)
        The scope chain for code that will run in the run-time env env.

    extend_static_env_with_define(static_env, sexpr)
        Mutates static_env: if sexpr is a top-level (define name value)
        whose value has a known arity (lambda literal, or a variable
        alias of something in static_env), records name's arity.

    SchemeAnalysisError   -> shape errors
    SchemeArityError      -> statically detected arity mismatch
                              (re-exported from Evaluator)
"""
from pyscheme.Environment import (
    _PositionedSchemeError, SchemeArityError, arity_mismatch_msg,
    LocalEnvironment, global_la_key,
)
from pyscheme.Parser import SchemeSyntaxError
from pyscheme.primitives import PRIMITIVE_ARITIES
from pyscheme.AST import (
    is_cons, is_nil, is_symbol, is_integer, is_real, is_rational, is_complex,
    is_string, is_character, is_boolean,
    as_symbol, as_integer, as_real, as_string, as_character, as_boolean,
    as_rational_num, as_rational_den, as_complex_real, as_complex_imag,
    src_of, gensym_display_name,
    alloc_cons, intern_symbol, as_symbol_id, resolve_symbol,
    make_scope_names, is_scope_names, closure_scope_names,
)


# R7RS §3.1: syntactic keywords that may not be used as plain expressions.
# The analyzer rejects a bare reference to one of these names (when no local
# binding shadows it) instead of letting it reach the evaluator as a stub.
_SYNTACTIC_KEYWORDS = frozenset([
    'and', 'begin', 'case', 'case-lambda', 'cond', 'cond-expand',
    'define', 'define-library', 'define-record-type', 'define-syntax',
    'define-values', 'delay', 'delay-force', 'do', 'guard', 'if',
    'import', 'include', 'include-ci', 'lambda', 'let', 'let*',
    'let*-values', 'let-syntax', 'let-values', 'letrec', 'letrec*',
    'letrec-syntax', 'or', 'parameterize', 'quasiquote', 'quote',
    'set!', 'syntax-rules', 'unless', 'when',
])


# ---- error type --------------------------------------------------------

class SchemeAnalysisError(_PositionedSchemeError):
    """Raised on a shape error.  Carries a SourceInfo (or None) so the
    listener can render a 3-line diagnostic with caret."""
    pass


# ---- helpers -----------------------------------------------------------

def _is_symbol_named(val, name):
    return is_symbol(val) and as_symbol(val) == name


def _proper_list_length(cell):
    """Length of a proper cons-cell list.  Returns -1 if improper.
    For NIL_VALUE returns 0."""
    if is_nil(cell):
        return 0
    n = 0
    cur = cell
    while is_cons(cur):
        n = n + 1
        cur = cur.cdr
    if is_nil(cur):
        return n
    return -1


def _cons_to_list(cell):
    """Walk a proper cons-cell list into a Python list of its elements.
    Caller must guarantee the list is proper."""
    items = []
    cur = cell
    while is_cons(cur):
        items.append(cur.car)
        cur = cur.cdr
    return items


def _display_name(name):
    """Gensym-stripped name for error messages (see AST.gensym_display_name)."""
    return gensym_display_name(name)


def _render(sexpr):
    """Brief text rendering of an S-expression for error messages."""
    if is_symbol(sexpr):
        return _display_name(as_symbol(sexpr))
    if is_nil(sexpr):
        return '()'
    if is_cons(sexpr):
        parts = []
        cur = sexpr
        while is_cons(cur):
            if not is_scope_names(cur.car):
                parts.append(_render(cur.car))
            cur = cur.cdr
        if is_nil(cur):
            return '(' + ' '.join(parts) + ')'
        return '(' + ' '.join(parts) + ' . ' + _render(cur) + ')'
    if is_integer(sexpr):
        return repr(as_integer(sexpr))
    if is_real(sexpr):
        return repr(as_real(sexpr))
    if is_string(sexpr):
        return repr(as_string(sexpr))
    if is_character(sexpr):
        return repr(as_character(sexpr))
    if is_boolean(sexpr):
        return repr(as_boolean(sexpr))
    if is_rational(sexpr):
        return str(as_rational_num(sexpr)) + '/' + str(as_rational_den(sexpr))
    if is_complex(sexpr):
        return str(as_complex_real(sexpr)) + '+' + str(as_complex_imag(sexpr)) + 'i'
    return repr(sexpr)


def _shadow(static_env, names):
    """Return a copy of static_env with each name bound to None
    (arity unknown).  Used when entering a lambda/let body where the
    bound names mask any outer static knowledge."""
    new_env = dict(static_env)
    i = 0
    while i < len(names):
        new_env[names[i]] = None
        i = i + 1
    return new_env


def _require_symbol(sexpr, context):
    if not is_symbol(sexpr):
        raise SchemeAnalysisError(
            "expected an identifier in " + context + ", got " + _render(sexpr),
            src_of(sexpr))
    return as_symbol(sexpr)


# ---- static arity ------------------------------------------------------

def _lambda_arity_from_cons(lam_cons):
    """Compute (min, max) arity from a (lambda params body...) cons cell.
    Caller must guarantee lam_cons is a cons whose car is the symbol
    'lambda' and whose cdr is a cons.  max=None means variadic."""
    params_sexpr = lam_cons.cdr.car
    if is_symbol(params_sexpr):
        return (0, None)
    if is_nil(params_sexpr):
        return (0, 0)
    n = 0
    cur = params_sexpr
    while is_cons(cur):
        n = n + 1
        cur = cur.cdr
    if is_nil(cur):
        return (n, n)
    return (n, None)


def _peek_lambda_arity(sexpr):
    """If sexpr is syntactically (lambda params body...), return its
    (min, max) arity without analyzing the body.  Otherwise None.
    Used by _analyze_define to make a recursive self-reference's arity
    visible while analyzing the body.  Shape errors in the lambda are
    still raised by _analyze_lambda."""
    if not is_cons(sexpr):
        return None
    if not _is_symbol_named(sexpr.car, 'lambda'):
        return None
    if not is_cons(sexpr.cdr):
        return None
    if _proper_list_length(sexpr) < 3:
        return None
    return _lambda_arity_from_cons(sexpr)


def _app_operator_arity(fn_sexpr, static_env):
    """Return (name, (min, max)) if fn_sexpr's arity is known statically,
    else None.  name is '' for an immediate lambda application."""
    if is_cons(fn_sexpr) and _is_symbol_named(fn_sexpr.car, 'lambda'):
        arity = _peek_lambda_arity(fn_sexpr)
        if arity is None:
            return None
        return ('', arity)
    if is_symbol(fn_sexpr):
        name = as_symbol(fn_sexpr)
        arity = static_env.get(name)
        if arity is None:
            return None
        return (name, arity)
    return None


def _check_app_arity(fn_sexpr, args, static_env, app_sexpr):
    """Statically enforce arity ONLY for syntactic keywords -- special forms /
    macros (e.g. define-record-type) that reach application analysis without a
    dedicated handler.  Those have no runtime apply step, so a wrong argument
    count is a syntax error and must be caught here (it is how, for instance, the
    R6RS `(define-record-type name (fields ...))` shape is rejected).

    For ordinary PROCEDURE calls (primitives, intrinsics, closures) arity is left
    to the evaluator's apply path, which raises an identical but *catchable*
    Scheme condition (same message text).  R7RS treats a procedure-call arity
    mismatch as a runtime error, so (test-error (apply +)) and
    (guard (e (#t ...)) (car)) must be able to catch it -- they could not when
    this raised during analysis, before any runtime handler is installed."""
    if not (is_symbol(fn_sexpr) and as_symbol(fn_sexpr) in _SYNTACTIC_KEYWORDS):
        return
    info = _app_operator_arity(fn_sexpr, static_env)
    if info is None:
        return
    name = info[0]
    lo = info[1][0]
    hi = info[1][1]
    n = len(args)
    if n < lo or (hi is not None and n > hi):
        raise SchemeArityError(
            arity_mismatch_msg(name, lo, hi, n), src_of(app_sexpr))


def extend_static_env_with_define(static_env, sexpr):
    """Mutate static_env from a top-level (define name value) cons cell.
    Records arity when value is either:
      - a (lambda params body...) literal -> its (min, max).
      - a symbol aliasing a name whose arity is already known.
    Other RHS shapes leave the binding unset (or remove it on
    re-definition with an unknown-arity RHS)."""
    if not is_cons(sexpr):
        return
    if not _is_symbol_named(sexpr.car, 'define'):
        return
    if not is_cons(sexpr.cdr) or not is_cons(sexpr.cdr.cdr):
        return
    name_sexpr = sexpr.cdr.car
    if not is_symbol(name_sexpr):
        return
    name = as_symbol(name_sexpr)
    value = sexpr.cdr.cdr.car
    if is_cons(value) and _is_symbol_named(value.car, 'lambda'):
        arity = _peek_lambda_arity(value)
        if arity is not None:
            static_env[name] = arity
            return
        static_env.pop(name, None)
        return
    if is_cons(value) and _is_symbol_named(value.car, 'case-lambda'):
        # Arity is the union of per-clause arities.  For the static
        # check we set the widest possible (0, None) - runtime handles
        # the clause match and any SchemeArityError.
        static_env[name] = (0, None)
        return
    if is_symbol(value):
        aliased = static_env.get(as_symbol(value))
        if aliased is not None:
            static_env[name] = aliased
        else:
            static_env.pop(name, None)
        return
    static_env.pop(name, None)


# ---- lexical addressing ------------------------------------------------
#
# Each variable reference is resolved to an LA key against a compile-time
# scope chain that mirrors, scope for scope, the chain the Evaluator builds
# at run time.  A scope chain is None when the code runs in a dict scope
# (global, library, eval / make-environment), or a pair (names, outer):
# names is the tuple of symbol ids of one run-time local scope, in slot
# order.  The scopes the Evaluator builds, and so the ones pushed here:
#
#   lambda, case-lambda clause  one scope: fixed parameters, then rest
#                               (closure_scope_names, as the closure gets)
#   delay, delay-force          one empty scope (the promise's thunk call)
#   let                         inits outside; body in one scope of all names
#   let*                        one scope per binding; each init sees the
#                               scopes of the bindings before it
#   letrec, letrec*             one scope; the inits are inside it
#   named let                   a scope holding the loop name, which the
#                               inits also run in, then the loop procedure's
#                               parameter scope under it for the body
#
# An empty let / let* / letrec builds no scope.  A name no scope binds gets
# a global LA key.  _resolve answers exactly as LocalEnvironment.resolve
# does at run time (the first matching slot, innermost scope first).

def _resolve(scope, sid):
    depth = 0
    while scope is not None:
        names = scope[0]
        i = 0
        while i < len(names):
            if names[i] == sid:
                return (depth, i, sid)
            i = i + 1
        scope = scope[1]
        depth = depth + 1
    return global_la_key(sid)


def _ref(sym, scope):
    """sym resolved against scope: the same symbol carrying its LA key."""
    return resolve_symbol(sym, _resolve(scope, as_symbol_id(sym)))


def scope_of_env(env):
    """The compile-time scope chain for code that will run in env: one entry
    per local scope from env up to the nearest dict scope, read off the live
    chain.  None when env is itself a dict scope, the usual case.  Used for
    forms analyzed at run time -- a debugger prompt paused in a procedure,
    or load called inside one -- so their locals get the keys real code
    gets."""
    chain = []
    e = env
    while isinstance(e, LocalEnvironment):
        names = []
        e.for_each_binding(lambda sid, val: names.append(sid))
        chain.append(tuple(names))
        e = e.getParent()
    scope = None
    i = len(chain) - 1
    while i >= 0:
        scope = (chain[i], scope)
        i = i - 1
    return scope


# ---- rebuilding --------------------------------------------------------
#
# The Analyzer returns a rewritten copy of the tree, never edits it: macro
# expansion can share one subtree between two expansions in different
# scopes, and an in-place rewrite would give the shared node one scope's
# keys in both.  A subtree nothing changed in is reused as it is.

def _rebuild(cell, car, cdr):
    """cell itself if neither field changed, else a fresh cell with its src."""
    if car is cell.car and cdr is cell.cdr:
        return cell
    return alloc_cons(car, cdr, cell.src)


def _analyze_seq(cell, static_env, scope):
    """Analyze each element of a list as an expression, in order; return the
    rebuilt list.  An improper tail is kept as it is."""
    cells = []
    items = []
    cur = cell
    while is_cons(cur):
        cells.append(cur)
        items.append(analyze(cur.car, static_env, scope))
        cur = cur.cdr
    result = cur
    i = len(cells) - 1
    while i >= 0:
        result = _rebuild(cells[i], items[i], result)
        i = i - 1
    return result


def _strip_scope_names(sexpr):
    """An already analyzed let-family form, with its scope-names node
    dropped, so analyzing it again builds a fresh node."""
    if is_cons(sexpr.cdr) and is_scope_names(sexpr.cdr.car):
        return alloc_cons(sexpr.car, sexpr.cdr.cdr, sexpr.src)
    return sexpr


def _with_scope_names(sexpr, scopes, rest):
    """(head <scope-names> . rest) for the let-family form sexpr."""
    return alloc_cons(
        sexpr.car,
        alloc_cons(make_scope_names(scopes), rest, None),
        sexpr.src)


def _rebuild_bindings(bindings_sexpr, inits):
    """The binding list with each (name init) rebuilt around its analyzed
    init, inits[i] for the i-th binding."""
    cells = []
    cur = bindings_sexpr
    while is_cons(cur):
        cells.append(cur)
        cur = cur.cdr
    result = cur
    i = len(cells) - 1
    while i >= 0:
        b = cells[i].car
        new_b = _rebuild(b, b.car, _rebuild(b.cdr, inits[i], b.cdr.cdr))
        result = _rebuild(cells[i], new_b, result)
        i = i - 1
    return result


def _name_sids(pairs):
    sids = []
    i = 0
    while i < len(pairs):
        sids.append(intern_symbol(pairs[i][0]))
        i = i + 1
    return tuple(sids)


# ---- main entry --------------------------------------------------------

# Definition context (R7RS 5.1 / 5.3).  A define may appear only at the top
# level -- including inside a top-level begin -- or at the start of a body.
# Body-leading defines never reach the Analyzer (the Expander rewrites them
# into letrec*), so any define the Analyzer meets below the top level sits
# in an expression context, e.g. (if c (define y 1)): conditionally creating
# a variable.  That is rejected.  _def_ctx[0] is True while the form being
# analyzed is in a definition context; each analyze() call clears it for its
# subforms (a top-level begin passes it through) and restores it on exit, so
# every external caller, which analyzes a top-level form, starts with True.
_def_ctx = [True]


def analyze(sexpr, static_env=None, scope=None):
    """Validate sexpr and resolve its variable references against scope (a
    compile-time scope chain; None for code that runs in a dict scope).
    Returns the rewritten tree, raises SchemeAnalysisError /
    SchemeArityError otherwise."""
    def_ok = _def_ctx[0]
    _def_ctx[0] = def_ok and is_cons(sexpr) and _is_symbol_named(sexpr.car, 'begin')
    try:
        return _analyze_form(sexpr, static_env, def_ok, scope)
    finally:
        _def_ctx[0] = def_ok


def _analyze_form(sexpr, static_env, def_ok, scope):
    if static_env is None:
        static_env = dict(PRIMITIVE_ARITIES)

    if is_nil(sexpr):
        raise SchemeAnalysisError(
            "empty list () is not a valid expression; use (quote ()) for the empty list",
            src_of(sexpr))

    if not is_cons(sexpr):
        # Atom (literal) or symbol (variable reference) - both are leaves.
        # R7RS §3.1: using a syntactic keyword as a variable is an error,
        # unless it is locally rebound (static_env stores None for local names).
        if is_symbol(sexpr):
            name = as_symbol(sexpr)
            if name in _SYNTACTIC_KEYWORDS and static_env.get(name) is not None:
                raise SchemeSyntaxError(
                    'keyword used as expression: ' + name, src_of(sexpr))
            # A user-defined macro keyword (define-syntax / let-syntax) used as an
            # expression is likewise an error (R7RS 4.3.1).  A FREE reference is
            # absent from static_env; a locally rebound name is present-as-None
            # (recorded by _shadow), so `name not in static_env` keeps lexical
            # shadowing working.  Quoted data never reaches here (quote does not
            # analyze its datum), so 'kw-only is unaffected.
            if name not in static_env:
                from pyscheme.Expander import _lookup_macro
                if _lookup_macro(sexpr) is not None:
                    raise SchemeSyntaxError(
                        'keyword used as expression: ' + name, src_of(sexpr))
            return _ref(sexpr, scope)
        return sexpr

    head = sexpr.car
    if is_symbol(head):
        handler = _SPECIAL_FORMS.get(as_symbol(head))
        if handler is not None:
            if handler is _analyze_define and not def_ok:
                raise SchemeSyntaxError(
                    'definition not allowed in an expression context',
                    src_of(sexpr))
            return handler(sexpr, static_env, scope)

    # Application: walk fn and arg sub-expressions.
    if _proper_list_length(sexpr) < 0:
        raise SchemeAnalysisError(
            "application must be a proper list", src_of(sexpr))
    fn_sexpr = sexpr.car
    args = []
    cur = sexpr.cdr
    while is_cons(cur):
        args.append(cur.car)
        cur = cur.cdr
    # Don't fire keyword check for the fn/head position: a keyword in head
    # position is a malformed special form, and the arity check below gives a
    # better error.  Keywords in arg positions are still checked.
    new_fn = fn_sexpr
    if not (is_symbol(fn_sexpr)
            and as_symbol(fn_sexpr) in _SYNTACTIC_KEYWORDS
            and static_env.get(as_symbol(fn_sexpr)) is not None):
        new_fn = analyze(fn_sexpr, static_env, scope)
    new_args = _analyze_seq(sexpr.cdr, static_env, scope)
    _check_app_arity(fn_sexpr, args, static_env, sexpr)
    return _rebuild(sexpr, new_fn, new_args)


# ---- special-form handlers ---------------------------------------------
#
# Each handler validates shape, recurses into sub-expressions with the
# scope chain they will run in, and returns the rewritten form.

def _analyze_lambda_shape(params_sexpr, body_cons, form_name, outer_src,
                          static_env, scope):
    """Validate the formals + body of a lambda-shaped form and return the
    analyzed body.  Shared by _analyze_lambda and _analyze_case_lambda;
    `form_name` ('lambda' or 'case-lambda') is used verbatim in error
    messages."""
    if is_symbol(params_sexpr):
        fixed_names = []
        fixed_srcs = []
        rest_name = as_symbol(params_sexpr)
        rest_src = src_of(params_sexpr)
    elif is_nil(params_sexpr):
        fixed_names = []
        fixed_srcs = []
        rest_name = None
        rest_src = None
    elif is_cons(params_sexpr):
        fixed_names = []
        fixed_srcs = []
        cur = params_sexpr
        while is_cons(cur):
            p = cur.car
            if not is_symbol(p):
                raise SchemeAnalysisError(
                    "expected an identifier in " + form_name +
                    " parameter list, got " + _render(p),
                    src_of(p))
            fixed_names.append(as_symbol(p))
            fixed_srcs.append(src_of(p))
            cur = cur.cdr
        if is_nil(cur):
            rest_name = None
            rest_src = None
        elif is_symbol(cur):
            rest_name = as_symbol(cur)
            rest_src = src_of(cur)
        else:
            raise SchemeAnalysisError(
                "expected an identifier in " + form_name +
                " rest parameter, got " + _render(cur),
                src_of(cur))
    else:
        raise SchemeAnalysisError(
            form_name + " parameter list must be a list or identifier, got " +
            _render(params_sexpr),
            src_of(params_sexpr))

    seen = set()
    i = 0
    while i < len(fixed_names):
        p = fixed_names[i]
        if p in seen:
            raise SchemeAnalysisError(
                "duplicate parameter name in " + form_name + ": " + p,
                fixed_srcs[i])
        seen.add(p)
        i = i + 1
    if rest_name is not None and rest_name in seen:
        raise SchemeAnalysisError(
            "rest parameter name conflicts with fixed parameter: " + rest_name,
            rest_src)

    shadowed = list(fixed_names)
    if rest_name is not None:
        shadowed.append(rest_name)
    body_env = _shadow(static_env, shadowed)

    body_count = _proper_list_length(body_cons)
    if body_count <= 0:
        raise SchemeAnalysisError(
            form_name + " body cannot be empty", outer_src)

    body_scope = (closure_scope_names(fixed_names, rest_name), scope)
    return _analyze_seq(body_cons, body_env, body_scope)


def _analyze_lambda(sexpr, static_env, scope):
    # Three forms:
    #   (lambda (a b c) body...)       fixed arity
    #   (lambda args body...)          pure variadic (args is bound to all args)
    #   (lambda (a b . rest) body...)  fixed prefix + rest bound to remainder
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            "lambda requires a parameter list and at least one body expression",
            src_of(sexpr))
    body = _analyze_lambda_shape(
        sexpr.cdr.car, sexpr.cdr.cdr, 'lambda', src_of(sexpr), static_env, scope)
    return _rebuild(sexpr, sexpr.car,
                    _rebuild(sexpr.cdr, sexpr.cdr.car, body))


def _analyze_define(sexpr, static_env, scope):
    n = _proper_list_length(sexpr) - 1
    if n != 2:
        raise SchemeAnalysisError(
            "define requires a name and a value (got " +
            str(n) + " arguments)",
            src_of(sexpr))
    name_sexpr = sexpr.cdr.car
    value_sexpr = sexpr.cdr.cdr.car
    name = _require_symbol(name_sexpr, 'define')
    # When the RHS is a lambda literal, pre-bind name to its arity so a
    # recursive self-reference inside the body can be arity-checked.
    value_env = static_env
    arity = _peek_lambda_arity(value_sexpr)
    if arity is not None:
        value_env = dict(static_env)
        value_env[name] = arity
    value = analyze(value_sexpr, value_env, scope)
    # The name is not resolved: define always binds into the dict scope.
    return _rebuild(sexpr, sexpr.car,
                    _rebuild(sexpr.cdr, name_sexpr,
                             _rebuild(sexpr.cdr.cdr, value, sexpr.cdr.cdr.cdr)))


def _analyze_set(sexpr, static_env, scope):
    n = _proper_list_length(sexpr) - 1
    if n != 2:
        raise SchemeAnalysisError(
            "set! requires a name and a value (got " + str(n) + " arguments)",
            src_of(sexpr))
    name_sexpr = sexpr.cdr.car
    value_sexpr = sexpr.cdr.cdr.car
    _require_symbol(name_sexpr, 'set!')
    value = analyze(value_sexpr, static_env, scope)
    return _rebuild(sexpr, sexpr.car,
                    _rebuild(sexpr.cdr, _ref(name_sexpr, scope),
                             _rebuild(sexpr.cdr.cdr, value, sexpr.cdr.cdr.cdr)))


def _analyze_if(sexpr, static_env, scope):
    n = _proper_list_length(sexpr) - 1
    if n != 2 and n != 3:
        raise SchemeAnalysisError(
            "if requires 2 or 3 arguments (test, then, optional else), got " + str(n),
            src_of(sexpr))
    return _rebuild(sexpr, sexpr.car, _analyze_seq(sexpr.cdr, static_env, scope))


def _analyze_begin(sexpr, static_env, scope):
    return _rebuild(sexpr, sexpr.car, _analyze_seq(sexpr.cdr, static_env, scope))


def _check_unique_let_names(pairs, form_name, sexpr):
    """Raise SchemeAnalysisError if any binding identifier appears twice.
    With alpha-renaming, two macro-introduced bindings with the same source
    name produce distinct gensym names, so plain name equality is correct."""
    seen = set()
    i = 0
    while i < len(pairs):
        n = pairs[i][0]
        if n in seen:
            raise SchemeAnalysisError(
                "duplicate variable name in " + form_name + " bindings: " + n,
                src_of(sexpr))
        seen.add(n)
        i = i + 1


def _parse_let_bindings(bindings_sexpr, form_name):
    """Validate a let-family binding list and return a list of
    (var_name, value_sexpr) pairs.  Does NOT analyze value_sexprs --
    the caller picks the right static_env."""
    if not is_cons(bindings_sexpr) and not is_nil(bindings_sexpr):
        raise SchemeAnalysisError(
            form_name + " bindings must be a list, got " +
            _render(bindings_sexpr),
            src_of(bindings_sexpr))
    if _proper_list_length(bindings_sexpr) < 0:
        raise SchemeAnalysisError(
            form_name + " bindings must be a proper list",
            src_of(bindings_sexpr))
    pairs = []
    cur = bindings_sexpr
    while is_cons(cur):
        b = cur.car
        if _proper_list_length(b) != 2:
            raise SchemeAnalysisError(
                form_name + " binding must be (name value), got " + _render(b),
                src_of(b))
        var = _require_symbol(b.car, form_name + " binding")
        pairs.append((var, b.cdr.car))
        cur = cur.cdr
    return pairs


def _analyze_let(sexpr, static_env, scope):
    # R7RS 4.2.2: (let <bindings> <body>)  OR  (let <name> <bindings> <body>)
    sexpr = _strip_scope_names(sexpr)
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            "let requires a binding list and at least one body expression",
            src_of(sexpr))
    if is_symbol(sexpr.cdr.car):
        return _analyze_named_let(sexpr, static_env, scope)
    bindings_sexpr = sexpr.cdr.car
    pairs = _parse_let_bindings(bindings_sexpr, 'let')
    # R7RS 4.2.2: a binding name must not appear twice.
    _check_unique_let_names(pairs, 'let', sexpr)
    # All val_exprs evaluated in the enclosing env.
    inits = []
    i = 0
    while i < len(pairs):
        inits.append(analyze(pairs[i][1], static_env, scope))
        i = i + 1
    names = []
    i = 0
    while i < len(pairs):
        names.append(pairs[i][0])
        i = i + 1
    body_env = _shadow(static_env, names)
    body_cons = sexpr.cdr.cdr
    if _proper_list_length(body_cons) <= 0:
        raise SchemeAnalysisError("let body cannot be empty", src_of(sexpr))
    if pairs:
        sids = _name_sids(pairs)
        scopes = (sids,)
        body_scope = (sids, scope)
    else:
        scopes = ()
        body_scope = scope
    body = _analyze_seq(body_cons, body_env, body_scope)
    return _with_scope_names(
        sexpr, scopes,
        alloc_cons(_rebuild_bindings(bindings_sexpr, inits), body, sexpr.cdr.src))


def _analyze_let_star(sexpr, static_env, scope):
    # R7RS 4.2.2: each val_expr sees the preceding bindings.
    sexpr = _strip_scope_names(sexpr)
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            "let* requires a binding list and at least one body expression",
            src_of(sexpr))
    bindings_sexpr = sexpr.cdr.car
    pairs = _parse_let_bindings(bindings_sexpr, 'let*')
    current_env = static_env
    current_scope = scope
    inits = []
    scopes = []
    i = 0
    while i < len(pairs):
        inits.append(analyze(pairs[i][1], current_env, current_scope))
        current_env = _shadow(current_env, [pairs[i][0]])
        sids = (intern_symbol(pairs[i][0]),)
        scopes.append(sids)
        current_scope = (sids, current_scope)
        i = i + 1
    body_cons = sexpr.cdr.cdr
    if _proper_list_length(body_cons) <= 0:
        raise SchemeAnalysisError("let* body cannot be empty", src_of(sexpr))
    body = _analyze_seq(body_cons, current_env, current_scope)
    return _with_scope_names(
        sexpr, tuple(scopes),
        alloc_cons(_rebuild_bindings(bindings_sexpr, inits), body, sexpr.cdr.src))


def _analyze_letrec(sexpr, static_env, scope):
    return _analyze_letrec_family(sexpr, static_env, 'letrec', scope)


def _analyze_letrec_star(sexpr, static_env, scope):
    return _analyze_letrec_family(sexpr, static_env, 'letrec*', scope)


def _analyze_letrec_family(sexpr, static_env, name, scope):
    # R7RS 4.2.2: all names visible in every val_expr.
    sexpr = _strip_scope_names(sexpr)
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            name + " requires a binding list and at least one body expression",
            src_of(sexpr))
    bindings_sexpr = sexpr.cdr.car
    pairs = _parse_let_bindings(bindings_sexpr, name)
    # R7RS 4.2.2: a binding name must not appear twice in letrec / letrec*.
    _check_unique_let_names(pairs, name, sexpr)
    names = []
    i = 0
    while i < len(pairs):
        names.append(pairs[i][0])
        i = i + 1
    inner_env = _shadow(static_env, names)
    if pairs:
        sids = _name_sids(pairs)
        scopes = (sids,)
        inner_scope = (sids, scope)
    else:
        scopes = ()
        inner_scope = scope
    inits = []
    i = 0
    while i < len(pairs):
        inits.append(analyze(pairs[i][1], inner_env, inner_scope))
        i = i + 1
    body_cons = sexpr.cdr.cdr
    if _proper_list_length(body_cons) <= 0:
        raise SchemeAnalysisError(
            name + " body cannot be empty", src_of(sexpr))
    body = _analyze_seq(body_cons, inner_env, inner_scope)
    return _with_scope_names(
        sexpr, scopes,
        alloc_cons(_rebuild_bindings(bindings_sexpr, inits), body, sexpr.cdr.src))


def _analyze_named_let(sexpr, static_env, scope):
    # R7RS 4.2.4: (let <name> <bindings> <body>)
    # Equivalent to (letrec ((name (lambda (v1 ...) body))) (name e1 ...))
    if _proper_list_length(sexpr) < 4:
        raise SchemeAnalysisError(
            "named let requires a name, a binding list, and at least one body expression",
            src_of(sexpr))
    name = as_symbol(sexpr.cdr.car)
    bindings_sexpr = sexpr.cdr.cdr.car
    pairs = _parse_let_bindings(bindings_sexpr, 'named let')
    params = []
    i = 0
    while i < len(pairs):
        params.append(pairs[i][0])
        i = i + 1
    seen = set()
    i = 0
    while i < len(params):
        p = params[i]
        if p in seen:
            raise SchemeAnalysisError(
                "duplicate parameter name in named let: " + p,
                src_of(sexpr.cdr.cdr.car))
        seen.add(p)
        i = i + 1
    # The Evaluator evaluates the inits inside the scope that holds the loop
    # name, so they are resolved there.  They cannot see the name itself: the
    # Expander renames the binding (R7RS 4.2.4).  Arity-wise they are still
    # checked against the enclosing static env, as before.
    loop_sids = (intern_symbol(name),)
    loop_scope = (loop_sids, scope)
    inits = []
    i = 0
    while i < len(pairs):
        inits.append(analyze(pairs[i][1], static_env, loop_scope))
        i = i + 1
    # Body sees `name` with arity (n, n) and its params shadow outer names.
    name_env = dict(static_env)
    name_env[name] = (len(params), len(params))
    body_env = _shadow(name_env, params)
    body_cons = sexpr.cdr.cdr.cdr
    if _proper_list_length(body_cons) <= 0:
        raise SchemeAnalysisError(
            "named let body cannot be empty", src_of(sexpr))
    param_sids = closure_scope_names(params, None)
    body = _analyze_seq(body_cons, body_env, (param_sids, loop_scope))
    return _with_scope_names(
        sexpr, (loop_sids, param_sids),
        alloc_cons(sexpr.cdr.car,
                   alloc_cons(_rebuild_bindings(bindings_sexpr, inits), body,
                              sexpr.cdr.cdr.src),
                   sexpr.cdr.src))


def _analyze_cond(sexpr, static_env, scope):
    # R7RS 4.2.1 clause forms:
    #   (<test>)
    #   (<test> <expr>...)
    #   (<test> => <proc>)
    #   (else <expr>...)         (must be last)
    if _proper_list_length(sexpr) < 2:
        raise SchemeAnalysisError(
            "cond must have at least one clause", src_of(sexpr))
    clauses = _cons_to_list(sexpr.cdr)
    total = len(clauses)
    new_clauses = []
    i = 0
    while i < total:
        clause = clauses[i]
        clen = _proper_list_length(clause)
        if clen <= 0:
            raise SchemeAnalysisError(
                "cond clause must be a non-empty list, got " + _render(clause),
                src_of(clause))
        head = clause.car
        # Auxiliary keywords `else` and `=>` are recognized only when not
        # shadowed by a lexical binding (R7RS hygiene).  static_env tracks
        # in-scope names; if `else`/`=>` appears there, it's a user variable
        # and the clause is treated as a regular test.
        head_is_else = (_is_symbol_named(head, 'else')
                        and 'else' not in static_env)
        head_is_arrow = (clen == 3 and _is_symbol_named(clause.cdr.car, '=>')
                         and '=>' not in static_env)
        if head_is_else:
            if i != total - 1:
                raise SchemeAnalysisError(
                    "cond 'else' clause must be the last clause", src_of(clause))
            body_cons = clause.cdr
            if _proper_list_length(body_cons) <= 0:
                raise SchemeAnalysisError(
                    "cond 'else' clause must have at least one expression",
                    src_of(clause))
            new_clauses.append(_rebuild(
                clause, head, _analyze_seq(body_cons, static_env, scope)))
        elif head_is_arrow:
            test = analyze(clause.car, static_env, scope)
            arrow_cell = clause.cdr
            proc_cell = arrow_cell.cdr
            proc = analyze(proc_cell.car, static_env, scope)
            new_clauses.append(_rebuild(
                clause, test,
                _rebuild(arrow_cell, arrow_cell.car,
                         _rebuild(proc_cell, proc, proc_cell.cdr))))
        else:
            # (<test>) or (<test> <expr>...)
            new_clauses.append(_analyze_seq(clause, static_env, scope))
        i = i + 1
    return _rebuild(sexpr, sexpr.car, _rebuild_list(sexpr.cdr, new_clauses))


def _rebuild_list(cell, items):
    """The proper list cell with its i-th element replaced by items[i],
    reusing every cell whose element and tail are unchanged."""
    cells = []
    cur = cell
    while is_cons(cur):
        cells.append(cur)
        cur = cur.cdr
    result = cur
    i = len(cells) - 1
    while i >= 0:
        result = _rebuild(cells[i], items[i], result)
        i = i - 1
    return result


def _analyze_case_body(clause, body_cons, static_env, scope, arrow_ok, msg):
    """The clause rebuilt around its analyzed body: (=> proc) when arrow_ok
    and the body starts with =>, else a sequence of expressions."""
    if (arrow_ok and is_cons(body_cons)
            and _is_symbol_named(body_cons.car, '=>')):
        if not (is_cons(body_cons.cdr) and is_nil(body_cons.cdr.cdr)):
            raise SchemeAnalysisError(msg, src_of(clause))
        proc_cell = body_cons.cdr
        proc = analyze(proc_cell.car, static_env, scope)
        return _rebuild(clause, clause.car,
                        _rebuild(body_cons, body_cons.car,
                                 _rebuild(proc_cell, proc, proc_cell.cdr)))
    return _rebuild(clause, clause.car,
                    _analyze_seq(body_cons, static_env, scope))


def _analyze_case(sexpr, static_env, scope):
    # R7RS 4.2.1: (case <key> <clause>...)
    # Each non-else clause: ((<datum>...) <expr>...)
    # else clause:          (else <expr>...)   (must be last)
    # Datums are implicitly quoted literals; we do NOT analyze them.
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            "case requires a key and at least one clause", src_of(sexpr))
    key = analyze(sexpr.cdr.car, static_env, scope)   # the key
    clauses = _cons_to_list(sexpr.cdr.cdr)
    total = len(clauses)
    new_clauses = []
    i = 0
    while i < total:
        clause = clauses[i]
        clen = _proper_list_length(clause)
        if clen < 2:
            raise SchemeAnalysisError(
                "case clause must be (<datum-list> <expr>...) or (else <expr>...), got "
                + _render(clause),
                src_of(clause))
        head = clause.car
        # case `else` recognized only when not shadowed (R7RS hygiene).
        if _is_symbol_named(head, 'else') and 'else' not in static_env:
            if i != total - 1:
                raise SchemeAnalysisError(
                    "case 'else' clause must be the last clause", src_of(clause))
            # (else => proc-expr) or (else <expr>...)
            new_clauses.append(_analyze_case_body(
                clause, clause.cdr, static_env, scope, '=>' not in static_env,
                "case 'else =>' clause must have exactly one expression"))
        else:
            if not (is_cons(head) or is_nil(head)):
                raise SchemeAnalysisError(
                    "case clause head must be a list of datums, got " +
                    _render(head),
                    src_of(head))
            if _proper_list_length(head) < 0:
                raise SchemeAnalysisError(
                    "case datum list must be a proper list", src_of(head))
            # ((<datum>...) => proc-expr) or ((<datum>...) <expr>...)
            new_clauses.append(_analyze_case_body(
                clause, clause.cdr, static_env, scope, True,
                "case '=>' clause must have exactly one expression"))
        i = i + 1
    return _rebuild(sexpr, sexpr.car,
                    _rebuild(sexpr.cdr, key,
                             _rebuild_list(sexpr.cdr.cdr, new_clauses)))


def _analyze_unquote_outside(sexpr, static_env, scope):
    # unquote and unquote-splicing only have meaning inside a quasiquote.
    # At any other position, surface a clear diagnostic.
    head_name = as_symbol(sexpr.car)
    raise SchemeAnalysisError(
        head_name + " is only valid inside a quasiquote template",
        src_of(sexpr))


def _analyze_case_lambda(sexpr, static_env, scope):
    # (case-lambda <clause>...)  where each clause is (formals body...).
    # Zero clauses is permitted by R7RS §4.2.9; the resulting procedure raises
    # a SchemeArityError on any call (no matching clause).
    # Each clause is validated by the shared _analyze_lambda_shape, which
    # tags error messages with the form_name 'case-lambda'.
    new_clauses = []
    cur = sexpr.cdr
    while is_cons(cur):
        clause = cur.car
        if not is_cons(clause):
            raise SchemeAnalysisError(
                "case-lambda clause must be a list, got " + _render(clause),
                src_of(clause))
        if _proper_list_length(clause) < 2:
            raise SchemeAnalysisError(
                "case-lambda clause must have formals and a non-empty body, got "
                + _render(clause), src_of(clause))
        body = _analyze_lambda_shape(
            clause.car, clause.cdr, 'case-lambda', src_of(clause),
            static_env, scope)
        new_clauses.append(_rebuild(clause, clause.car, body))
        cur = cur.cdr
    return _rebuild(sexpr, sexpr.car, _rebuild_list(sexpr.cdr, new_clauses))


def _analyze_cond_expand(sexpr, static_env, scope):
    # Well-formed cond-expand is expanded away.  This handler fires only
    # when the Expander punted - either shape error or no clause matched.
    if _proper_list_length(sexpr) < 2:
        raise SchemeAnalysisError(
            "cond-expand requires at least one clause", src_of(sexpr))
    clauses = _cons_to_list(sexpr.cdr)
    total = len(clauses)
    i = 0
    while i < total:
        clause = clauses[i]
        if _proper_list_length(clause) < 1:
            raise SchemeAnalysisError(
                "cond-expand clause must be a non-empty list", src_of(clause))
        i = i + 1
    # If we fall through here, shape is OK but no clause matched - that
    # is the R7RS error condition.
    raise SchemeAnalysisError(
        "cond-expand: no clause matched", src_of(sexpr))


def _analyze_include(sexpr, static_env, scope):
    # (include "file1" "file2" ...)
    # Well-formed include is expanded by the Expander; this handler
    # fires only on shape errors (no args or non-string arg).
    n = _proper_list_length(sexpr) - 1
    if n < 1:
        raise SchemeAnalysisError(
            "include requires at least one filename string", src_of(sexpr))
    cur = sexpr.cdr
    while is_cons(cur):
        arg = cur.car
        if not is_string(arg):
            raise SchemeAnalysisError(
                "include arguments must be string literals, got " +
                _render(arg),
                src_of(arg))
        cur = cur.cdr
    return sexpr


def _analyze_do(sexpr, static_env, scope):
    # (do ((var init [step]) ...) (test result...) command...)
    # R7RS 4.2.4.  Well-formed do expressions are desugared by the
    # Expander; this handler fires only when the Expander punted on a
    # shape error, so it exists to produce a useful diagnostic.
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            "do requires a binding list and a test clause", src_of(sexpr))
    bindings_sexpr = sexpr.cdr.car
    test_sexpr = sexpr.cdr.cdr.car

    if _proper_list_length(bindings_sexpr) < 0:
        raise SchemeAnalysisError(
            "do bindings must be a proper list, got " +
            _render(bindings_sexpr),
            src_of(bindings_sexpr))
    seen = {}
    cur = bindings_sexpr
    while is_cons(cur):
        b = cur.car
        blen = _proper_list_length(b)
        if blen != 2 and blen != 3:
            raise SchemeAnalysisError(
                "do binding must be (var init) or (var init step), got "
                + _render(b), src_of(b))
        name = _require_symbol(b.car, 'do binding')
        if name in seen:
            raise SchemeAnalysisError(
                "duplicate variable name in do bindings: " + name,
                src_of(b.car))
        seen[name] = True
        cur = cur.cdr

    if not is_cons(test_sexpr):
        raise SchemeAnalysisError(
            "do test clause must be a list starting with a test expression",
            src_of(test_sexpr))
    if _proper_list_length(test_sexpr) < 1:
        raise SchemeAnalysisError(
            "do test clause must be a proper list",
            src_of(test_sexpr))
    return sexpr


def _analyze_and(sexpr, static_env, scope):
    return _rebuild(sexpr, sexpr.car, _analyze_seq(sexpr.cdr, static_env, scope))


def _analyze_or(sexpr, static_env, scope):
    return _rebuild(sexpr, sexpr.car, _analyze_seq(sexpr.cdr, static_env, scope))


def _analyze_when(sexpr, static_env, scope):
    return _analyze_when_unless(sexpr, static_env, 'when', scope)


def _analyze_unless(sexpr, static_env, scope):
    return _analyze_when_unless(sexpr, static_env, 'unless', scope)


def _analyze_when_unless(sexpr, static_env, name, scope):
    if _proper_list_length(sexpr) < 3:
        raise SchemeAnalysisError(
            name + " requires a test and at least one body expression",
            src_of(sexpr))
    return _rebuild(sexpr, sexpr.car, _analyze_seq(sexpr.cdr, static_env, scope))


def _analyze_quote(sexpr, static_env, scope):
    n = _proper_list_length(sexpr) - 1
    if n != 1:
        raise SchemeAnalysisError(
            "quote requires exactly 1 argument, got " + str(n),
            src_of(sexpr))
    # Quoted data is literal - do NOT walk or analyze its contents.
    return sexpr


def _analyze_delay(sexpr, static_env, scope):
    name = as_symbol(sexpr.car)
    n = _proper_list_length(sexpr) - 1
    if n != 1:
        raise SchemeAnalysisError(
            name + " requires exactly 1 argument, got " + str(n),
            src_of(sexpr))
    # The expression runs when the promise's thunk (no parameters) is called:
    # in an empty scope under this one.
    expr = analyze(sexpr.cdr.car, static_env, ((), scope))
    return _rebuild(sexpr, sexpr.car, _rebuild(sexpr.cdr, expr, sexpr.cdr.cdr))


def _analyze_library_form(sexpr, static_env, scope):
    # define-library / import / export: shape is validated at evaluation
    # time in the Evaluator (where the library registry lives).  We just
    # accept the form here so it doesn't fall through to the generic
    # application path and arity-check against a fictitious primitive.
    return sexpr


_SPECIAL_FORMS = {
    'lambda':       _analyze_lambda,
    'case-lambda':  _analyze_case_lambda,
    'define':       _analyze_define,
    'set!':         _analyze_set,
    'if':           _analyze_if,
    'begin':        _analyze_begin,
    'let':          _analyze_let,
    'let*':         _analyze_let_star,
    'letrec':       _analyze_letrec,
    'letrec*':      _analyze_letrec_star,
    'cond':         _analyze_cond,
    'case':         _analyze_case,
    'do':           _analyze_do,
    'include':      _analyze_include,
    'include-ci':   _analyze_include,
    'cond-expand':  _analyze_cond_expand,
    'and':          _analyze_and,
    'or':           _analyze_or,
    'when':         _analyze_when,
    'unless':       _analyze_unless,
    'quote':        _analyze_quote,
    'unquote':          _analyze_unquote_outside,
    'unquote-splicing': _analyze_unquote_outside,
    'delay':        _analyze_delay,
    'delay-force':  _analyze_delay,
    'define-library':               _analyze_library_form,
    'import':                       _analyze_library_form,
    'export':                       _analyze_library_form,
    'include-library-declarations': _analyze_library_form,
    # Macro forms are handled entirely by the Expander; if one survives
    # to the Analyzer, accept it silently (usually as part of a quoted
    # or still-unexpanded datum).  No static validation here.
    'define-syntax':    _analyze_library_form,
    'let-syntax':       _analyze_library_form,
    'letrec-syntax':    _analyze_library_form,
    'syntax-rules':     _analyze_library_form,
}


# -------- Self-test --------

if __name__ == '__main__':
    from pyscheme.Parser import parse_one
    from pyscheme.Expander import expand

    n_pass = 0
    n_fail = 0

    # Happy-path: each source must analyze without raising.
    happy = [
        '42', '3.14', '#t', '"hi"', 'x',
        '(lambda (x) x)',
        '(lambda (x y) x y)',
        '(lambda () 42)',
        '(lambda args args)',
        '(lambda (x . rest) rest)',
        '(lambda (x y . rest) rest)',
        '(lambda (x) "doc" x)',
        '(lambda () "hello")',
        '(define x 5)',
        '(define (id x) x)',
        '(set! x 5)',
        '(if #t 1 2)',
        '(if #t 1)',
        '(f 1 2)',
        '(f)',
        '(begin)',
        '(begin 1 2 3)',
        '(let ((x 1) (y 2)) x)',
        '(let* ((x 1) (y x)) y)',
        '(letrec ((f (lambda (x) x))) (f 1))',
        '(letrec* ((f (lambda (x) x))) (f 1))',
        '(let loop ((x 1)) x)',
        '(cond (#t 1) (else 2))',
        '(cond ((f 1)) (else 2))',
        '(cond ((f 1) => g) (else 2))',
        '(and 1 2)',
        '(or)',
        '(when #t 1)',
        '(quote x)',
        "'x",
    ]

    print('-- analyze (post-expander) --')
    i = 0
    while i < len(happy):
        source = happy[i]
        try:
            got = analyze(expand(parse_one(source)))
        except Exception as e:
            print("[FAIL] %r: %s: %s" % (source, type(e).__name__, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
        if got is not None:
            print("[ OK ] %r" % source)
            n_pass = n_pass + 1
        else:
            print("[FAIL] %r: returned None" % source)
            n_fail = n_fail + 1
        i = i + 1

    # Semantic errors that should be raised by the analyzer.
    errors = [
        ('()',                     'empty list'),
        ('(lambda 1 x)',           'parameter list must be a list or identifier'),
        ('(lambda (1) x)',         'expected an identifier'),
        ('(lambda (x x) x)',       'duplicate parameter name'),
        ('(lambda (x . x) x)',     'rest parameter name conflicts'),
        ('(lambda (x . 1) x)',     'expected an identifier'),
        ('(lambda (x) )',          'body cannot be empty'),
        ('(define)',               'requires a name and a value'),
        ('(define 5 6)',           'expected an identifier'),
        ('(set!)',                 'requires a name and a value'),
        ('(set! 1 2)',             'expected an identifier'),
        ('(if)',                   'requires 2 or 3 arguments'),
        ('(if a b c d)',           'requires 2 or 3 arguments'),
        ('(let)',                  'binding list'),
        ('(let ((x)) x)',          'binding must be'),
        ('(let ((1 2)) x)',        'expected an identifier'),
        ('(let () )',              'body cannot be empty'),
        ('(cond)',                 'at least one clause'),
        ('(cond ())',              'non-empty list'),
        ('(cond (else) (#t 1))',   "'else' clause must be the last"),
        ('(cond (else))',          "must have at least one expression"),
        ('(when)',                 'requires a test'),
        ('(when #t)',              'requires a test'),
        ('(quote)',                'exactly 1 argument'),
        ('(quote a b)',            'exactly 1 argument'),
    ]

    print()
    print('-- analyzer errors --')
    i = 0
    while i < len(errors):
        source = errors[i][0]
        expected_fragment = errors[i][1]
        try:
            analyze(expand(parse_one(source)))
        except SchemeAnalysisError as e:
            if expected_fragment in str(e):
                print("[ OK ] %r  ->  %s" % (source, e))
                n_pass = n_pass + 1
            else:
                print("[WARN] %r  ->  %s" % (source, e))
                print("        expected substring: %r" % expected_fragment)
                n_pass = n_pass + 1
            i = i + 1
            continue
        except Exception as e:
            print("[FAIL] %r: wrong exception %s: %s" %
                  (source, type(e).__name__, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
        print("[FAIL] %r: expected SchemeAnalysisError" % source)
        n_fail = n_fail + 1
        i = i + 1

    # Static arity errors (caught at analyze time, not runtime).
    print()
    print('-- static arity errors --')
    arity_errors = [
        ('((lambda (x y) x) 1)',             '1 argument provided; 2 expected'),
        ('((lambda (x) x) 1 2)',             '2 arguments provided; 1 expected'),
        ('((lambda () 1) 99)',               '1 argument provided; 0 expected'),
        ('((lambda (a b . rest) rest) 1)',
            '1 argument provided; at least 2 expected'),
        ('(cons 1)',                         'cons: 1 argument provided; 2 expected'),
        ('(cons 1 2 3)',                     'cons: 3 arguments provided; 2 expected'),
        ('(car)',                            'car: 0 arguments provided; 1 expected'),
        ('(car 1 2)',                        'car: 2 arguments provided; 1 expected'),
    ]
    i = 0
    while i < len(arity_errors):
        source = arity_errors[i][0]
        expected_fragment = arity_errors[i][1]
        try:
            analyze(expand(parse_one(source)))
        except SchemeArityError as e:
            if expected_fragment in str(e):
                print("[ OK ] %r  ->  %s: %s" % (source, type(e).__name__, e))
                n_pass = n_pass + 1
            else:
                print("[WARN] %r  ->  %s" % (source, e))
                print("        expected substring: %r" % expected_fragment)
                n_pass = n_pass + 1
            i = i + 1
            continue
        except Exception as e:
            print("[FAIL] %r: wrong exception %s: %s" %
                  (source, type(e).__name__, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
        print("[FAIL] %r: expected SchemeArityError" % source)
        n_fail = n_fail + 1
        i = i + 1

    # Cross-form static arity (shared static_env across multiple top-level forms).
    print()
    print('-- cross-form static arity --')
    cross_cases = [
        (['(define (f x) x)', '(f 1 2)'],
         'f: 2 arguments provided; 1 expected'),
        (['(define (g a b) a)', '(g 1 2)'], None),
        (['(define (h x) x)', '(define (h x y) y)', '(h 1)'],
            'h: 1 argument provided; 2 expected'),
        (['(define (g a b) a)', '(define h2 g)', '(h2 1)'],
            'h2: 1 argument provided; 2 expected'),
        (['(define (fact n) (fact n n))'],
            'fact: 2 arguments provided; 1 expected'),
        (['((lambda (cons) (cons 1 2 3)) 1)'], None),
    ]
    i = 0
    while i < len(cross_cases):
        sources = cross_cases[i][0]
        expected_fragment = cross_cases[i][1]
        env = dict(PRIMITIVE_ARITIES)
        err = None
        j = 0
        while j < len(sources):
            try:
                sexpr = expand(parse_one(sources[j]))
                analyze(sexpr, env)
                extend_static_env_with_define(env, sexpr)
            except SchemeAnalysisError as e:
                err = e
                break
            except SchemeArityError as e:
                err = e
                break
            j = j + 1
        if expected_fragment is None:
            if err is None:
                print("[ OK ] %r" % sources)
                n_pass = n_pass + 1
            else:
                print("[FAIL] %r: unexpected error %s: %s" %
                      (sources, type(err).__name__, err))
                n_fail = n_fail + 1
        else:
            if err is None:
                print("[FAIL] %r: expected error with %r" %
                      (sources, expected_fragment))
                n_fail = n_fail + 1
            elif expected_fragment in str(err):
                print("[ OK ] %r  ->  %s: %s" %
                      (sources, type(err).__name__, err))
                n_pass = n_pass + 1
            else:
                print("[FAIL] %r: got %s: %s" %
                      (sources, type(err).__name__, err))
                print("        expected substring: %r" % expected_fragment)
                n_fail = n_fail + 1
        i = i + 1

    print()
    print("%d passed, %d failed" % (n_pass, n_fail))
