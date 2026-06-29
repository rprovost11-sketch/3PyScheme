"""CEK machine evaluator: dispatches on cons-cell shape directly.

Pipeline position:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> cek_eval

The Analyzer validates shape and returns the cons-cell tree unchanged.
This evaluator dispatches on cons-cell shape:

    is_cons(C) and is_symbol(car(C)) and as_symbol(car(C)) is a keyword
        -> handle that special form
    is_cons(C)
        -> application: evaluate fn, then args, then apply
    is_symbol(C)
        -> variable lookup
    otherwise (atom, NIL, VOID)
        -> self-evaluating; V = C

Two CEK machine states encoded lexically:
    inner top loop    -> EVAL state
    inner bottom loop -> APPLY state
    outer loop        -> restarts EVAL after APPLY

Body forms are stored as cons chains throughout (not pre-extracted to
Python lists) so the runtime data shape matches the source.  Arg lists
and let bindings are pre-walked into Python lists at dispatch time
(maps to the C++ reference's ArgBuf / std::vector<Value>).

Value forms (tagged tuples - arms of the Value variant):
   (VOID,)                       singleton
   (BOOLEAN,   b,   src)
   (INTEGER,   n,   src)
   (REAL,      x,   src)
   (RATIONAL,  n, d, src)
   (CHARACTER, c,   src)
   (STRING,    s,   src)
   (SYMBOL,    s,   src)         self-evaluates from quoted data
   (CLOSURE,   params, body_cons, env, rest_name, docstring)
   (PRIMITIVE, name, fn)
   ConsCell { car, cdr, src }    PAIR
   NIL_VALUE = (NIL,)            empty list singleton

src may be None for runtime-constructed atoms (e.g., (+ 1 2) -> (INTEGER, 3, None)).

Frame forms (runtime continuation state):
   (FRAME_DEFINE,    name, env)
   (FRAME_SET,       name, env, name_src)
   (FRAME_IF,        then_br, else_br, env)
   (FRAME_ARG,       args_list, env, app_node)
                        args_list is Python list of arg expressions
                        app_node is the application cons (for arity error position)
   (FRAME_CALL,      fn_value, collected_list, remaining_list, env, app_node)
                        baton frame walking through args
   (FRAME_SEQ,       remaining_body_cons, env)
                        sequencing for multi-expr body; cons chain
   (FRAME_WHEN,      body_cons, env)
   (FRAME_UNLESS,    body_cons, env)
   (FRAME_AND,       remaining_cons, env)
   (FRAME_OR,        remaining_cons, env)
   (FRAME_COND,      current_clause, remaining_clauses_cons, env)
   (FRAME_COND_ARROW, test_value, env)
   (FRAME_LET,       names_list, collected_list, remaining_pairs_list, body_cons, saved_env)
   (FRAME_LET_STAR,  name, remaining_pairs_list, body_cons, env)
   (FRAME_LETREC,    name, remaining_pairs_list, body_cons, env)
   (FRAME_CASE,      current_clause, remaining_clauses_cons, env)
                        waits for the key value to pop, then eqv?-matches
                        it against the current clause's datum list
   (FRAME_CASE_ARROW, key_value, env)
"""
from __future__ import annotations

import time

from pyscheme.Environment import (
    Environment,
    _PositionedSchemeError,
    SchemeArityError, SchemeUnboundError, SchemeTypeError,
    SchemeRuntimeError, SchemeUserError, SchemeRaised, SchemeFileError,
    arity_mismatch_msg,
)
from pyscheme.AST import (
    ConsCell, NIL_VALUE, VOID_VALUE, alloc_cons,
    is_cons, is_nil, is_void, is_symbol, is_boolean, is_integer, is_real,
    is_character, is_string, is_primitive,
    is_closure, is_promise,
    is_case_closure, is_multi_values, is_parameter, is_continuation,
    is_environment, is_record, is_record_accessor, is_record_mutator,
    is_vector, as_vector_items, make_vector, make_string, make_character,
    as_symbol, as_symbol_id, as_boolean, as_integer, as_real, as_character,
    as_string, as_primitive_fn, as_primitive_name, as_primitive_kind,
    PRIM_CALL_CC, PRIM_APPLY, PRIM_CALL_WITH_VALUES, PRIM_FORCE,
    PRIM_MAKE_PARAMETER, PRIM_WITH_EXCEPTION_HANDLER, PRIM_GUARD_EVAL,
    PRIM_RAISE, PRIM_RAISE_CONTINUABLE, PRIM_EVAL, PRIM_ERROR,
    PRIM_WITH_PARAMETERS, PRIM_DYNAMIC_WIND, PRIM_ORDINARY,
    PRIM_CONTINUATION_DEPTH, PRIM_MAP, PRIM_FOR_EACH, PRIM_FILTER,
    PRIM_VECTOR_MAP, PRIM_VECTOR_FOR_EACH, PRIM_STRING_MAP, PRIM_STRING_FOR_EACH,
    PRIM_MEMBER, PRIM_ASSOC, PRIM_PORT_RUNNER, PRIM_LOAD,
    as_closure_params, as_closure_body, as_closure_env, as_closure_rest_name,
    as_case_closure_clauses, as_case_closure_env, as_parameter_value,
    as_continuation_k, as_continuation_wind, as_continuation_shadow,
    as_promise_is_done, as_promise_is_iterative, as_promise_payload,
    as_multi_values_list, as_environment, as_continuation_handlers,
    as_record_type, as_record_fields,
    as_record_accessor_type, as_record_accessor_index, as_record_accessor_name,
    as_record_mutator_type, as_record_mutator_index, as_record_mutator_name,
    as_record_type_name,
    promise_resolve, promise_become, set_parameter_value,
    as_parameter_converter,
    make_boolean, make_closure, make_case_closure, make_promise_lazy, make_integer,
    make_continuation, make_multi_values, make_primitive, make_parameter, make_symbol,
    make_read_error_object,
    mark_literal_immutable,
    eqv_atom, intern_symbol,
    src_of,
)


# -------- Frame-tag constants (runtime continuation state) ----------

FRAME_DEFINE = 0
FRAME_SET = 1
FRAME_IF = 2
FRAME_ARG = 3
FRAME_CALL = 4
FRAME_SEQ = 5
FRAME_WHEN = 6
FRAME_UNLESS = 7
FRAME_AND = 8
FRAME_OR = 9
FRAME_COND = 10
FRAME_COND_ARROW = 11
FRAME_LET = 12
FRAME_LET_STAR = 13
FRAME_LETREC = 14
FRAME_CASE = 15
FRAME_CASE_ARROW = 22
FRAME_DYNAMIC_WIND_AFTER = 16
FRAME_CWV_CONSUMER = 17
FRAME_FORCE_RESULT = 18
FRAME_MAKE_PARAMETER = 19
FRAME_POP_HANDLER = 20
FRAME_REINSTALL_HANDLER = 21
FRAME_SHADOW_POP = 23
FRAME_TRACE_EXIT = 24
FRAME_NONCONTIN_RETURN = 25
FRAME_GUARD = 26
FRAME_HOF_STEP = 27
FRAME_HOF_STEP_IDX = 28
FRAME_SEARCH_STEP = 29
FRAME_RESTORE_VALUE = 30
FRAME_DYNAMIC_WIND_BEFORE_DONE = 31
FRAME_PARAMETERIZE_STEP = 32
FRAME_WIND_STEP = 33
FRAME_ERROR_UNWIND = 34
FRAME_EVAL_FORMS = 35
FRAME_LIB_FINALIZE = 36
FRAME_IMPORT_STEP = 37
FRAME_ENSURE_LOADED = 38

# Sentinel marking the very first FRAME_HOF_STEP entry, where V holds the
# higher-order call's last argument (not a callback result) and must be
# ignored.  Identity-compared, never a real Scheme value.
_HOF_START = object()


# --- Special-form dispatch kinds (optimization #1) ---------------------
# The CEK eval loop, on every cons-with-symbol-head, formerly walked a
# ladder of ~21 sequential string == comparisons to recognize a special
# form before falling through to the application path.  Every application
# paid all 21 compares.  Instead, stamp each keyword's interned symbol id
# with an integer kind in _SPECIAL_FORM_KIND; the loop does ONE dict.get
# on the head's symbol id.  Applications (the hot case) get None in one
# miss and skip the ladder entirely; special forms dispatch on the int
# kind via an inlined switch (if/elif).  C port: a switch on these kinds.
_SF_QUOTE = 1
_SF_LAMBDA = 2
_SF_CASE_LAMBDA = 3
_SF_DELAY = 4
_SF_DELAY_FORCE = 5
_SF_IMPORT = 6
_SF_DEFINE_LIBRARY = 7
_SF_IF = 8
_SF_DEFINE = 9
_SF_SET = 10
_SF_BEGIN = 11
_SF_WHEN = 12
_SF_UNLESS = 13
_SF_AND = 14
_SF_OR = 15
_SF_COND = 16
_SF_CASE = 17
_SF_LET = 18
_SF_LET_STAR = 19
_SF_LETREC = 20    # letrec and letrec* share one body
_SF_TRACE = 21
_SF_UNTRACE = 22

_SPECIAL_FORM_KIND = {
    intern_symbol('quote'):          _SF_QUOTE,
    intern_symbol('lambda'):         _SF_LAMBDA,
    intern_symbol('case-lambda'):    _SF_CASE_LAMBDA,
    intern_symbol('delay'):          _SF_DELAY,
    intern_symbol('delay-force'):    _SF_DELAY_FORCE,
    intern_symbol('import'):         _SF_IMPORT,
    intern_symbol('define-library'): _SF_DEFINE_LIBRARY,
    intern_symbol('if'):             _SF_IF,
    intern_symbol('define'):         _SF_DEFINE,
    intern_symbol('set!'):           _SF_SET,
    intern_symbol('begin'):          _SF_BEGIN,
    intern_symbol('when'):           _SF_WHEN,
    intern_symbol('unless'):         _SF_UNLESS,
    intern_symbol('and'):            _SF_AND,
    intern_symbol('or'):             _SF_OR,
    intern_symbol('cond'):           _SF_COND,
    intern_symbol('case'):           _SF_CASE,
    intern_symbol('let'):            _SF_LET,
    intern_symbol('let*'):           _SF_LET_STAR,
    intern_symbol('letrec'):         _SF_LETREC,
    intern_symbol('letrec*'):        _SF_LETREC,
    intern_symbol('trace'):          _SF_TRACE,
    intern_symbol('untrace'):        _SF_UNTRACE,
}


# Frames that are not single-value continuations: FRAME_CWV_CONSUMER
# unpacks multi-values; FRAME_SEQ discards V (begin/body sequencing);
# the wind / handler pop frames forward V transparently to the next
# frame.  Multi-values may legitimately arrive at any of these.
_MULTI_VALUES_OK_FRAMES = frozenset([
    FRAME_CWV_CONSUMER,
    FRAME_SEQ,
    FRAME_DYNAMIC_WIND_AFTER,
    FRAME_POP_HANDLER,
    FRAME_REINSTALL_HANDLER,
    FRAME_NONCONTIN_RETURN,
    FRAME_SHADOW_POP,
    FRAME_TRACE_EXIT,
    FRAME_GUARD,
    FRAME_HOF_STEP,
    FRAME_HOF_STEP_IDX,
    FRAME_SEARCH_STEP,
    # FRAME_RESTORE_VALUE discards the incoming V (a wind after-thunk's result)
    # and reinstates a saved value, so a multi-valued after result is harmless.
    FRAME_RESTORE_VALUE,
    # FRAME_DYNAMIC_WIND_BEFORE_DONE discards the before-thunk's result before
    # tail-calling the body, so a multi-valued before result is harmless.
    FRAME_DYNAMIC_WIND_BEFORE_DONE,
    # FRAME_PARAMETERIZE_STEP collects each converter's result as an installed
    # parameter value; tolerate a multi-valued converter result as the old
    # synchronous converter application did.
    FRAME_PARAMETERIZE_STEP,
    # FRAME_WIND_STEP discards each wind thunk's result and finally installs the
    # continuation's value (which may itself be multiple values).
    FRAME_WIND_STEP,
    # FRAME_ERROR_UNWIND discards each unwind after-thunk's result before either
    # dispatching the handler or re-raising; multi-values are irrelevant here.
    FRAME_ERROR_UNWIND,
    # FRAME_EVAL_FORMS discards each top-level form's result (load / library
    # loading sequence the forms for effect), so multi-values are harmless.
    FRAME_EVAL_FORMS,
    # FRAME_LIB_FINALIZE builds + registers a library's exports; the incoming V
    # (the last library form's result) is discarded.
    FRAME_LIB_FINALIZE,
    # FRAME_IMPORT_STEP / FRAME_ENSURE_LOADED sequence import resolution and
    # library file loading for effect; the incoming V is irrelevant.
    FRAME_IMPORT_STEP,
    FRAME_ENSURE_LOADED,
])

_SHADOW_DEPTH_LIMIT = 50

# Global environment reference for the .py extension loader.  Set by
# Interpreter.reboot() via set_global_env(); mirrors the pattern used
# by Expander._runtime_env_ref.
_global_env_ref = [None]


def set_global_env(env):
    _global_env_ref[0] = env


# -------- Helper functions ------------------------------------------

def _shadow_label(app_node):
    """Return the display label for a shadow-stack entry: the operator
    symbol name if the call site is a symbol application, else '#<procedure>'."""
    if app_node is not None and is_cons(app_node) and is_symbol(app_node.car):
        return as_symbol(app_node.car)
    return '#<procedure>'


def _shadow_push(ctx, K, app_node):
    """Push a shadow-stack entry for a closure entry.  If the top of K is
    FRAME_SHADOW_POP this is a tail call: replace the current top entry
    rather than pushing a new one (keeps the shadow stack bounded under TCO).
    Otherwise push a new entry and a FRAME_SHADOW_POP return marker onto K."""
    ss = ctx.shadow_stack
    label = _shadow_label(app_node)
    src = src_of(app_node) if app_node is not None else None
    if K and K[-1][0] == FRAME_SHADOW_POP:
        if ss:
            top = ss[-1]
            if top[0] == label and top[1] is src:
                top[2] = top[2] + 1
                return
            ss[-1] = [label, src, 1]
        return
    if len(ss) >= _SHADOW_DEPTH_LIMIT:
        return
    if ss:
        top = ss[-1]
        if top[0] == label and top[1] is src:
            top[2] = top[2] + 1
            K.append((FRAME_SHADOW_POP,))
            return
    ss.append([label, src, 1])
    K.append((FRAME_SHADOW_POP,))


def _sorted_sym_list(fns):
    """Build a Scheme proper list of symbols from a frozenset of name strings,
    sorted lexicographically.  Used by (trace) and (untrace) return values."""
    names = list(fns)
    names.sort()
    result = NIL_VALUE
    i = len(names) - 1
    while i >= 0:
        result = alloc_cons(make_symbol(names[i], None), result, None)
        i = i - 1
    return result


def isFalse(value):
    """Scheme falsity: only #f is false; everything else (including 0, '',
    '()) is truthy.  Safe against ConsCell / any non-tuple value."""
    return is_boolean(value) and as_boolean(value) is False


def _collect_cons_to_list(cell):
    """Walk a proper cons-cell list into a Python list of its elements.
    Returns empty list for NIL_VALUE.  Caller must guarantee proper list."""
    items = []
    cur = cell
    while is_cons(cur):
        items.append(cur.car)
        cur = cur.cdr
    return items


def _collect_let_bindings(bindings_cons):
    """Walk a let bindings list into a Python list of (name, val_expr) pairs."""
    pairs = []
    cur = bindings_cons
    while is_cons(cur):
        b = cur.car
        var_name = as_symbol(b.car)
        val_expr = b.cdr.car
        pairs.append((var_name, val_expr))
        cur = cur.cdr
    return pairs


def _make_closure_from_lambda(lam_cons, env):
    """Build a CLOSURE value from a (lambda params-form body...) cons cell."""
    params_sexpr = lam_cons.cdr.car
    body_cons = lam_cons.cdr.cdr
    params = []
    rest_name = None
    if is_symbol(params_sexpr):
        rest_name = as_symbol(params_sexpr)
    elif is_cons(params_sexpr) or is_nil(params_sexpr):
        cur = params_sexpr
        while is_cons(cur):
            params.append(as_symbol(cur.car))
            cur = cur.cdr
        if is_symbol(cur):
            rest_name = as_symbol(cur)
    docstring = ''
    if is_cons(body_cons) and is_cons(body_cons.cdr):
        first = body_cons.car
        if is_string(first):
            docstring = as_string(first)
            body_cons = body_cons.cdr
    return make_closure(params, body_cons, env, rest_name, docstring)


def _make_case_closure_from_form(cl_cons, env):
    """Build a CASE_CLOSURE value from (case-lambda (formals body...) ...)."""
    clauses = []
    cur = cl_cons.cdr
    while is_cons(cur):
        clause = cur.car
        params_sexpr = clause.car
        body_cons = clause.cdr
        params = []
        rest_name = None
        if is_symbol(params_sexpr):
            rest_name = as_symbol(params_sexpr)
        elif is_cons(params_sexpr) or is_nil(params_sexpr):
            p_cur = params_sexpr
            while is_cons(p_cur):
                params.append(as_symbol(p_cur.car))
                p_cur = p_cur.cdr
            if is_symbol(p_cur):
                rest_name = as_symbol(p_cur)
        clauses.append((params, body_cons, rest_name))
        cur = cur.cdr
    return make_case_closure(clauses, env, '')


class _BetaResult:
    """Return container for _beta_reduce: the freshly built call env
    and the closure's body cons chain.  Ports to a C struct."""

    def __init__(self, new_env, body):
        self.new_env = new_env
        self.body = body


def _beta_reduce_core(params, body, clo_env, rest, arg_values, app_node):
    """Validate arity against (params, rest), build the call env, return a _BetaResult."""
    n_fixed = len(params)
    n_args = len(arg_values)
    if rest is None:
        if n_fixed != n_args:
            raise SchemeArityError(
                arity_mismatch_msg('', n_fixed, n_fixed, n_args),
                src_of(app_node) if app_node is not None else None)
    else:
        if n_args < n_fixed:
            raise SchemeArityError(
                arity_mismatch_msg('', n_fixed, None, n_args),
                src_of(app_node) if app_node is not None else None)
    new_env = Environment(clo_env)
    i = 0
    while i < n_fixed:
        new_env.bind(params[i], arg_values[i])
        i = i + 1
    if rest is not None:
        rest_value = NIL_VALUE
        i = n_args - 1
        while i >= n_fixed:
            rest_value = alloc_cons(arg_values[i], rest_value, None)
            i = i - 1
        new_env.bind(rest, rest_value)
    return _BetaResult(new_env, body)


def _beta_reduce(closure, arg_values, app_node=None):
    """Validate arity, build the call env, and return a _BetaResult."""
    return _beta_reduce_core(
        as_closure_params(closure),
        as_closure_body(closure),
        as_closure_env(closure),
        as_closure_rest_name(closure),
        arg_values, app_node)


# Special-primitive interception is dispatched on the integer kind tag set
# by make_primitive (AST.PRIM_*), read once per application in the
# FRAME_CALL handler.  This replaced a ladder of ~15 per-call
# _is_X_primitive name comparisons (optimization #2).


# Thread-local CEK exception handler stack and dynamic-wind stack.
# Each thread sees its own list; this maps to __thread storage in the C
# port so concurrent evaluations don't share continuation-relevant state.
def _restore_handler_stack(ctx, snapshot):
    """Replace ctx.handler_stack contents with snapshot in place.  Continuation
    invocation uses this so a captured continuation's K-stack frames
    (including FRAME_POP_HANDLER) find the matching handler entries."""
    ctx.handler_stack.clear()
    ctx.handler_stack.extend(snapshot)


def _restore_shadow_stack(ctx, snapshot):
    ctx.shadow_stack.clear()
    ctx.shadow_stack.extend(snapshot)


def _resolve_parameterize_params(params_list, values_list, ctx, app_node):
    """Phase 1 of parameterize setup: walk the parameter / value lists,
    resolve current-port accessor primitives to their backing parameters,
    and validate.  Returns (params, new_vals_raw).  Pure -- no converter
    application, no install -- so the converters can run on the K stack
    (FRAME_PARAMETERIZE_STEP) instead of re-entering cek_eval."""
    params = []
    cur = params_list
    while is_cons(cur):
        p = cur.car
        if not is_parameter(p) and is_primitive(p):
            # R7RS 6.13.1: current-output-port / current-input-port /
            # current-error-port ARE parameter objects.  They are exposed as
            # accessor primitives; map the accessor to its backing parameter so
            # parameterize can rebind it.
            from pyscheme.primitives.ports import port_parameter_for_accessor
            backing = port_parameter_for_accessor(as_primitive_name(p), ctx)
            if backing is not None:
                p = backing
        if not is_parameter(p):
            raise SchemeTypeError(
                '%with-parameters: non-parameter in parameterize binding',
                app_node)
        params.append(p)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            '%with-parameters: parameter list must be proper', app_node)

    new_vals_raw = []
    cur = values_list
    while is_cons(cur):
        new_vals_raw.append(cur.car)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            '%with-parameters: value list must be proper', app_node)

    if len(params) != len(new_vals_raw):
        raise SchemeTypeError(
            '%with-parameters: parameter / value count mismatch', app_node)
    return (params, new_vals_raw)


def _finalize_parameterize_winds(params, installed, ctx):
    """Phase 2 of parameterize setup, run once FRAME_PARAMETERIZE_STEP has
    applied every converter and collected the `installed` values: save the
    current values, install the new ones so the thunk sees them, and return a
    pair of Python-backed primitives (install_thunk, restore_thunk) that
    FRAME_WIND_STEP and FRAME_DYNAMIC_WIND_AFTER invoke as Scheme procedures.
    Saving after the converters (not before) matches the old
    _build_parameterize_winds ordering."""
    saved_values = []
    i = 0
    while i < len(params):
        saved_values.append(as_parameter_value(params[i]))
        i = i + 1

    # Install new values now so the thunk sees them.
    i = 0
    while i < len(params):
        set_parameter_value(params[i], installed[i])
        i = i + 1

    # Build installer (called by FRAME_WIND_STEP on continuation re-entry) and
    # restorer (called by FRAME_DYNAMIC_WIND_AFTER / FRAME_ERROR_UNWIND).
    def installer(ctx2, env2, args2, app_node2):
        j = 0
        while j < len(params):
            set_parameter_value(params[j], installed[j])
            j = j + 1
        return VOID_VALUE

    def restorer(ctx2, env2, args2, app_node2):
        j = 0
        while j < len(params):
            set_parameter_value(params[j], saved_values[j])
            j = j + 1
        return VOID_VALUE

    return (make_primitive('%parameterize-install', installer),
            make_primitive('%parameterize-restore', restorer))


def _build_hof_frame(fn_value, kind, collected, app_node):
    """If fn_value is a higher-order primitive whose per-element calls are
    driven on the K stack -- map / for-each / filter (FRAME_HOF_STEP), the
    vector/string variants (FRAME_HOF_STEP_IDX), or the 3-arg member / assoc
    forms (FRAME_SEARCH_STEP) -- build and return its driver frame; otherwise
    return None.

    Shared by the FRAME_CALL terminal dispatch and _enter_proc so that one of
    these primitives reached as a *callback* (e.g. the per-element proc of an
    outer map, as in (map filter ...)) is driven on frames too, rather than
    re-entering cek_eval through its _prim_* fallback body.  Keeping the single
    source of truth here guarantees the callback path and the direct-operator
    path agree on arity checks, type checks, and frame layout."""
    src = src_of(app_node) if app_node is not None else None
    if kind == PRIM_MAP or kind == PRIM_FOR_EACH or kind == PRIM_FILTER:
        name = as_primitive_name(fn_value)
        if kind == PRIM_FILTER:
            if len(collected) != 2:
                raise SchemeArityError(
                    arity_mismatch_msg(name, 2, 2, len(collected)), src)
        elif len(collected) < 2:
            raise SchemeArityError(
                arity_mismatch_msg(name, 2, None, len(collected)), src)
        return (FRAME_HOF_STEP, kind, collected[0],
                tuple(collected[1:]), NIL_VALUE, app_node, _HOF_START)
    if (kind == PRIM_VECTOR_MAP or kind == PRIM_VECTOR_FOR_EACH
            or kind == PRIM_STRING_MAP or kind == PRIM_STRING_FOR_EACH):
        name = as_primitive_name(fn_value)
        if len(collected) < 2:
            raise SchemeArityError(
                arity_mismatch_msg(name, 2, None, len(collected)), src)
        is_vec = (kind == PRIM_VECTOR_MAP or kind == PRIM_VECTOR_FOR_EACH)
        seqs = []
        shortest = None
        j = 1
        while j < len(collected):
            seq = collected[j]
            if is_vec:
                if not is_vector(seq):
                    raise SchemeTypeError(
                        '%s: argument %d must be a vector' % (name, j + 1), src)
                seq_len = len(as_vector_items(seq))
            else:
                if not is_string(seq):
                    raise SchemeTypeError(
                        '%s: argument %d must be a string' % (name, j + 1), src)
                seq_len = len(as_string(seq))
            seqs.append(seq)
            if shortest is None or seq_len < shortest:
                shortest = seq_len
            j = j + 1
        return (FRAME_HOF_STEP_IDX, kind, collected[0], tuple(seqs), 0,
                shortest, NIL_VALUE, app_node, False)
    if (kind == PRIM_MEMBER or kind == PRIM_ASSOC) and len(collected) == 3:
        return (FRAME_SEARCH_STEP, kind, collected[2], collected[0],
                collected[1], app_node, False)
    return None


def _enter_proc(fn_value, args, ctx, saved_env, app_node):
    """Dispatch a procedure application with known args.  Returns a
    next-state descriptor so frame handlers can update the CEK state
    without duplicating the FRAME_CALL terminal-dispatch logic:
      ('value', V)                 - primitive or parameter produced V
      ('cont',  new_K, new_V)      - continuation invoked; restore K and V
      ('frame', frame)             - higher-order primitive reached as a
                                     callback; push `frame` on K and resume the
                                     APPLY loop (drives the call on frames
                                     instead of re-entering cek_eval)
      ('enter', C, new_env, seq)   - closure entered; eval C in new_env;
                                     push FRAME_SEQ(seq, new_env) if seq is
                                     not None (multi-form body)"""
    if is_continuation(fn_value):
        # Drive the wind walk on the K stack (FRAME_WIND_STEP), which then
        # installs the continuation.  Delivered via the ('frame', ...)
        # descriptor every _enter_proc caller already handles, so the wind
        # before/after thunks run without re-entering cek_eval.  (All evaluation
        # now runs on one loop, so a continuation is always installed in place --
        # no escape across native frames is ever needed.)
        return ('frame',
                (FRAME_WIND_STEP,
                 _compute_wind_ops(ctx, as_continuation_wind(fn_value)),
                 0, fn_value, _continuation_value(fn_value, args)))
    pv = _apply_parameter_if(fn_value, len(args), app_node)
    if pv is not None:
        return ('value', pv)
    if is_primitive(fn_value):
        pkind = as_primitive_kind(fn_value)
        hof_frame = _build_hof_frame(fn_value, pkind, args, app_node)
        if hof_frame is not None:
            return ('frame', hof_frame)
        if pkind == PRIM_LOAD:
            # load reached as a callback (e.g. (for-each load files)): read +
            # parse and drive its forms on the K stack, like the FRAME_CALL
            # interception, rather than re-entering cek_eval via _prim_load.
            from pyscheme.primitives.meta import load_setup
            _lf = load_setup(args, saved_env, app_node)
            return ('frame', (FRAME_EVAL_FORMS, _lf[0], _lf[1], 0, {}, True))
        V = as_primitive_fn(fn_value)(ctx, saved_env, args, app_node)
        return ('value', V)
    # Record accessors / mutators are first-class procedures (R7RS 5.5), so the
    # FRAME_HOF_STEP / FRAME_CWV_CONSUMER paths that tail-call through here must
    # apply them too, matching the FRAME_CALL terminal dispatch.
    if is_record_accessor(fn_value):
        if len(args) != 1:
            raise SchemeArityError(
                arity_mismatch_msg(as_record_accessor_name(fn_value),
                                   1, 1, len(args)), app_node)
        rt = as_record_accessor_type(fn_value)
        rec = args[0]
        if not is_record(rec) or as_record_type(rec) is not rt:
            raise SchemeTypeError(
                as_record_accessor_name(fn_value) + ': argument is not a '
                + as_record_type_name(rt), app_node)
        return ('value', as_record_fields(rec)[as_record_accessor_index(fn_value)])
    if is_record_mutator(fn_value):
        if len(args) != 2:
            raise SchemeArityError(
                arity_mismatch_msg(as_record_mutator_name(fn_value),
                                   2, 2, len(args)), app_node)
        rt = as_record_mutator_type(fn_value)
        rec = args[0]
        if not is_record(rec) or as_record_type(rec) is not rt:
            raise SchemeTypeError(
                as_record_mutator_name(fn_value) + ': first argument is not a '
                + as_record_type_name(rt), app_node)
        as_record_fields(rec)[as_record_mutator_index(fn_value)] = args[1]
        return ('value', VOID_VALUE)
    if is_closure(fn_value) or is_case_closure(fn_value):
        r = _apply_value(fn_value, args, app_node)
        if is_cons(r.body.cdr):
            return ('enter', r.body.car, r.new_env, r.body.cdr)
        return ('enter', r.body.car, r.new_env, None)
    raise SchemeTypeError(
        'expected a procedure', app_node)


def _apply_enter_result(result, K, V):
    """Apply an _enter_proc descriptor to the CEK registers and tell the APPLY
    loop what to do next.  K is mutated in place when a frame must be pushed.
    Two outcomes:
      ('apply', V)    -- a value was produced (or a frame-driven callback was
                         pushed); V is the value register to carry forward
                         (caller: set V, then continue / resume the APPLY phase)
      ('eval',  C, E) -- a closure body was entered; evaluate expression C in
                         environment E (caller: set C and E, then run EVAL)
    _enter_proc never yields a 'cont' descriptor since the single-loop rewrite
    -- a continuation arrives as a FRAME_WIND_STEP 'frame' -- so there is no
    K / V reinstatement case here."""
    tag = result[0]
    if tag == 'value':
        return ('apply', result[1])
    if tag == 'frame':
        K.append(result[1])
        return ('apply', V)
    # 'enter': a closure body to evaluate; push FRAME_SEQ for a multi-form body.
    new_C = result[1]
    new_E = result[2]
    if result[3] is not None:
        K.append((FRAME_SEQ, result[3], new_E))
    return ('eval', new_C, new_E)


def _unpack_apply_args(collected, app_node):
    """(apply proc arg1 arg2 ... argN list) has the tail list spliced onto
    the leading args.  Returns (proc, flat_args) or raises SchemeTypeError."""
    if len(collected) < 2:
        raise SchemeArityError(
            arity_mismatch_msg('apply', 2, None, len(collected)),
            src_of(app_node) if app_node is not None else None)
    proc = collected[0]
    flat_args = []
    i = 1
    while i < len(collected) - 1:
        flat_args.append(collected[i])
        i = i + 1
    last = collected[len(collected) - 1]
    cur = last
    while is_cons(cur):
        flat_args.append(cur.car)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'apply: last argument must be a proper list', app_node)
    return (proc, flat_args)


def _continuation_value(cont, arg_values):
    """Value to install when invoking a continuation with the given arg list.
    0 args -> VOID, 1 arg -> that arg, 2+ -> MULTI_VALUES container."""
    if len(arg_values) == 0:
        return VOID_VALUE
    if len(arg_values) == 1:
        return arg_values[0]
    return make_multi_values(list(arg_values))


def _compute_wind_ops(ctx, target):
    """Return the ordered wind operations that transform ctx.wind_stack into
    `target` (a continuation's wind snapshot), WITHOUT mutating the stack or
    running any thunk:
      ('exit', after_thunk)         -- leaving an extent: pop the top, run after
      ('enter', before_thunk, entry)-- entering an extent: push entry, run before
    Exits come first (innermost-first), then enters (outermost-first).
    FRAME_WIND_STEP performs the pops/pushes and runs the thunks on the K stack,
    so a continuation jump across dynamic-wind / parameterize runs entirely on
    the one loop (no re-entrant cek_eval)."""
    ws = ctx.wind_stack
    common = 0
    while common < len(ws) and common < len(target):
        cur = ws[common]
        tgt = target[common]
        if cur[0] is not tgt[0] or cur[1] is not tgt[1]:
            break
        common = common + 1
    ops = []
    j = len(ws)
    while j > common:
        ops.append(('exit', ws[j - 1][1]))
        j = j - 1
    i = common
    while i < len(target):
        ops.append(('enter', target[i][0], target[i]))
        i = i + 1
    return ops


def _apply_parameter_if(V, n_args, app_node):
    """If V is a parameter object, enforce 0-arg call semantics.  Returns
    the parameter's current value on a 0-arg call, or raises on arity
    mismatch.  Returns the sentinel None when V is not a parameter, so
    callers can fall through to ordinary dispatch.  (None is safe as a
    sentinel because parameter values are never Python None in practice -
    every Scheme value is a tuple / class instance.)"""
    if not is_parameter(V):
        return None
    if n_args != 0:
        raise SchemeArityError(
            arity_mismatch_msg('parameter', 0, 0, n_args),
            src_of(app_node) if app_node is not None else None)
    return as_parameter_value(V)


def _apply_value(V, arg_values, app_node):
    """Dispatch a procedure-valued V to its call-env builder.  Handles
    ordinary CLOSURE and CASE_CLOSURE (arity-dispatched).  Returns a
    _BetaResult or raises SchemeArityError / SchemeTypeError."""
    if is_case_closure(V):
        clauses = as_case_closure_clauses(V)
        clo_env = as_case_closure_env(V)
        n_args = len(arg_values)
        i = 0
        while i < len(clauses):
            c = clauses[i]
            params = c[0]
            body = c[1]
            rest = c[2]
            n_fixed = len(params)
            if rest is None:
                if n_fixed == n_args:
                    return _beta_reduce_core(params, body, clo_env, None,
                                             arg_values, app_node)
            else:
                if n_args >= n_fixed:
                    return _beta_reduce_core(params, body, clo_env, rest,
                                             arg_values, app_node)
            i = i + 1
        raise SchemeArityError(
            'case-lambda: no clause matches ' + str(n_args) + ' arguments',
            src_of(app_node) if app_node is not None else None)
    if not is_closure(V):
        from pyscheme.PrettyPrinter import pretty_print
        raise SchemeTypeError(
            'application of non-procedure: ' + pretty_print(V),
            src_of(app_node) if app_node is not None else None)
    return _beta_reduce(V, arg_values, app_node)


def _is_aux_keyword(sym, name, env):
    """R7RS auxiliary syntax: a symbol matches `name` only when not user-bound.
    With alpha-renaming, any user binding of `else` etc. will have a gensym
    name, so plain name comparison is sufficient."""
    if not is_symbol(sym) or as_symbol(sym) != name:
        return False
    if env is None:
        return True
    return env.lookup_optional(name) is None


def _classify_cond_clause(clause, env=None):
    """Return ('else', body_cons) | ('arrow', test, proc) |
    ('test-only', test) | ('body', test, body_cons).  Caller has
    already verified clause is a non-nil cons (Analyzer validated).
    `env` enables hygienic recognition of auxiliary keywords `else`
    and `=>`: shadowing them in scope reverts to a normal test."""
    head = clause.car
    if _is_aux_keyword(head, 'else', env):
        return ('else', clause.cdr)
    if is_nil(clause.cdr):
        return ('test-only', head)
    if (is_cons(clause.cdr)
        and _is_aux_keyword(clause.cdr.car, '=>', env)
            and is_cons(clause.cdr.cdr) and is_nil(clause.cdr.cdr.cdr)):
        return ('arrow', head, clause.cdr.cdr.car)
    return ('body', head, clause.cdr)


# -------- The CEK machine -------------------------------------------

def cek_eval(expr, env, ctx=None):
    """Evaluate expr against env.  Returns the resulting Value.

    ctx (if given) carries interpreter-wide state like the output stream;
    it is threaded into primitive calls.  When ctx is None we construct
    a default Context whose outStrm is sys.stdout."""
    if ctx is None:
        from pyscheme.Context import Context
        ctx = Context()
    # Snapshot the wind-stack depth at entry so we can unwind any
    # FRAME_DYNAMIC_WIND_AFTER frames installed during this cek_eval call
    # if an exception escapes the inner loop.  The inner loop is in a
    # helper function so we can wrap it in a single try/except.
    wind_depth_entry = len(ctx.wind_stack)
    handler_depth_entry = len(ctx.handler_stack)
    from pyscheme.Parser import SchemeSyntaxError
    from pyscheme.Analyzer import SchemeAnalysisError
    # SchemeAnalysisError is catchable too: a malformed form analyzed DURING
    # evaluation (e.g. (eval '(lambda (1) 1) env), or a macro expanding to a bad
    # form) should reach an in-scope guard, exactly as in cppScheme2 (whose guard
    # catches all SchemeError).  Top-level program analyze errors are raised outside
    # cek_eval and still abort, unchanged.
    _CATCHABLE = (SchemeRaised, SchemeTypeError, SchemeArityError,
                  SchemeUnboundError, SchemeSyntaxError, SchemeAnalysisError)
    try:
        return _cek_loop(expr, env, ctx)
    except _CATCHABLE as e:
        # _cek_loop's in-loop exception dispatch already walked K for any
        # handler frame installed during this cek_eval call.  Reaching
        # here means no handler in scope caught the condition; clean up
        # any dynamic-wind entries installed during this call (their
        # after-thunks errors are swallowed, matching the C++ reference)
        # and re-raise so the caller (often an outer cek_eval) can continue
        # propagation.  Truncate handler_stack defensively in case a
        # handler push escaped without its matching pop.
        _unwind_winds_on_error(ctx, wind_depth_entry)
        while len(ctx.handler_stack) > handler_depth_entry:
            ctx.handler_stack.pop()
        if isinstance(e, _PositionedSchemeError) and e.call_stack is None and ctx.shadow_stack:
            _cs = []
            _j = 0
            while _j < len(ctx.shadow_stack):
                _cs.append(ctx.shadow_stack[_j])
                _j = _j + 1
            e.call_stack = _cs
        ctx.shadow_stack.clear()
        raise
    except BaseException:
        _unwind_winds_on_error(ctx, wind_depth_entry)
        while len(ctx.handler_stack) > handler_depth_entry:
            ctx.handler_stack.pop()
        ctx.shadow_stack.clear()
        raise


# --- Library search path -----------------------------------------------
# The search path used for .sld auto-discovery is held in a `current-
# library-path` parameter (an R7RS parameter object), so a program can
# read it, rebind it for a dynamic extent with `parameterize`, or replace
# it persistently with `set-library-path!`.  The loader reads the
# parameter's *current* value, so all three take effect.  The parameter is
# created per-interpreter in Interpreter.reboot via make_library_path_param
# and registered here; until then (e.g. the Evaluator self-test) the loader
# falls back to the default '.' + SCHEME_LIBRARY_PATH path.
_library_path_param_ref = [None]


def set_library_path_param(p):
    """Install the current-library-path parameter as the loader's source
    of truth.  Called by Interpreter.reboot."""
    _library_path_param_ref[0] = p


def _env_library_path_parts():
    """The SCHEME_LIBRARY_PATH portion of the default load path: the env
    var split on os.pathsep (';' on Windows, ':' on Unix), empties dropped."""
    import os
    raw = os.environ.get('SCHEME_LIBRARY_PATH', '').split(os.pathsep)
    parts = []
    i = 0
    while i < len(raw):
        if raw[i]:
            parts.append(raw[i])
        i = i + 1
    return parts


def _srfi_library_part():
    """The shared SRFI/ directory from pyscheme-cppscheme2-common, if present,
    as a one-element list (else []).  Appended to the library search path so
    (srfi N) resolves without an explicit -L; user-supplied -L/SCHEME_LIBRARY_PATH
    entries precede it and so win on conflicts."""
    from pyscheme.common_dir import common_subdir
    srfi = common_subdir('SRFI')
    return [srfi] if srfi is not None else []


def _default_library_path():
    """Default search path when no current-library-path parameter is in
    effect: current directory, then SCHEME_LIBRARY_PATH entries, then the
    shared SRFI/ directory."""
    return ['.'] + _env_library_path_parts() + _srfi_library_part()


def build_library_path_list(cli_paths):
    """Build the initial library search path: current directory, then the CLI
    -L/-I paths (in command-line order), then SCHEME_LIBRARY_PATH, then the
    shared SRFI/ directory."""
    parts = ['.']
    i = 0
    while i < len(cli_paths):
        if cli_paths[i]:
            parts.append(cli_paths[i])
        i = i + 1
    parts.extend(_env_library_path_parts())
    parts.extend(_srfi_library_part())
    return parts


def _make_scheme_string_list(py_paths):
    """Build a Scheme proper list of immutable strings from a Python list
    of directory-name strings."""
    result = NIL_VALUE
    i = len(py_paths) - 1
    while i >= 0:
        s = make_string(py_paths[i])
        s.immutable = True
        result = alloc_cons(s, result)
        i = i - 1
    return result


def normalize_library_path_value(val, app_node=None):
    """Validate that `val` is a proper list of strings and return a fresh
    Scheme list of immutable string copies.  Used as the current-library-
    path parameter's converter (so parameterize is validated) and by
    set-library-path!.  Raises SchemeTypeError on a non-list or a
    non-string element."""
    from pyscheme.Environment import SchemeTypeError
    paths = []
    cur = val
    while is_cons(cur):
        elt = cur.car
        if not is_string(elt):
            raise SchemeTypeError(
                'library path must be a list of strings', src_of(app_node))
        paths.append(as_string(elt))
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'library path must be a proper list', src_of(app_node))
    return _make_scheme_string_list(paths)


def _library_path_converter_fn(ctx, env, args, app_node):
    return normalize_library_path_value(args[0], app_node)


def make_library_path_param(cli_paths):
    """Create the current-library-path parameter from '.' + CLI paths +
    SCHEME_LIBRARY_PATH, install it as the loader's source of truth, and
    return it so the caller can bind it into the global env."""
    init_val = _make_scheme_string_list(
        build_library_path_list(cli_paths))
    converter = make_primitive('%library-path-converter',
                               _library_path_converter_fn)
    param = make_parameter(init_val, converter)
    set_library_path_param(param)
    return param


def library_path_param_assign(normalized):
    """Persistently replace the current-library-path parameter's value (the
    engine behind set-library-path!)."""
    param = _library_path_param_ref[0]
    if param is not None:
        set_parameter_value(param, normalized)


def _library_load_path():
    """Return the search path for .sld auto-discovery as a Python list of
    directory strings.  When a current-library-path parameter is in effect
    (a booted interpreter), its current value is used so parameterize and
    set-library-path! take effect; otherwise falls back to the default
    ('.' + SCHEME_LIBRARY_PATH)."""
    param = _library_path_param_ref[0]
    if param is None:
        return _default_library_path()
    paths = []
    cur = as_parameter_value(param)
    while is_cons(cur):
        elt = cur.car
        if is_string(elt):
            paths.append(as_string(elt))
        cur = cur.cdr
    return paths


def _load_py_extension(path):
    """Import a .py extension file and call its register(env) entry point.
    The module receives the global environment so it can install new
    primitives with env.bind(name, make_primitive(name, fn)).  It may
    also call library_register directly if it is a standalone module
    without a companion .sld file."""
    import importlib.util
    import sys
    import os
    key = os.path.splitext(os.path.basename(path))[0]
    mod_name = 'pyscheme_ext.' + key
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    if hasattr(module, 'register'):
        module.register(_global_env_ref[0])


def _process_one_lib_decl(decl, lib_env, export_names, eval_forms,
                          import_sets, ctx):
    """Process a single library declaration during define-library setup.
    Collects export names into export_names, import-set sexprs into import_sets,
    and begin / unknown-decl forms (unexpanded) into eval_forms -- all in
    declaration order.  Nothing is resolved or evaluated here: the imports are
    bound (loading library files if needed) and the forms evaluated later on the
    main K stack (FRAME_IMPORT_STEP / FRAME_EVAL_FORMS), so no re-entrant
    cek_eval.  Recursive: include-library-declarations and cond-expand decls
    call back into this function for the forms they produce."""
    from pyscheme.Parser import SchemeSyntaxError, parse
    from pyscheme.Expander import expand, _include_base_dir, _feature_req_matches
    if not is_cons(decl) or not is_symbol(decl.car):
        raise SchemeSyntaxError(
            'define-library: declaration must be a list starting with a symbol',
            src_of(decl))
    dsym = as_symbol(decl.car)
    dbody = decl.cdr

    if dsym == 'import':
        sets = dbody
        while is_cons(sets):
            import_sets.append(sets.car)
            sets = sets.cdr
        return

    if dsym == 'export':
        specs = dbody
        while is_cons(specs):
            spec = specs.car
            specs = specs.cdr
            if is_symbol(spec):
                nm = as_symbol(spec)
                export_names.append((nm, nm))
            elif (is_cons(spec) and is_symbol(spec.car)
                  and as_symbol(spec.car) == 'rename'):
                r = spec.cdr
                if (not is_cons(r) or not is_symbol(r.car)
                        or not is_cons(r.cdr) or not is_symbol(r.cdr.car)):
                    raise SchemeSyntaxError(
                        'define-library: malformed export rename',
                        src_of(spec))
                export_names.append((as_symbol(r.car), as_symbol(r.cdr.car)))
            else:
                raise SchemeSyntaxError(
                    'define-library: malformed export spec', src_of(spec))
        return

    if dsym == 'begin':
        forms = dbody
        while is_cons(forms):
            eval_forms.append(forms.car)
            forms = forms.cdr
        return

    if dsym == 'include-library-declarations':
        # (include-library-declarations <filename>...) - read each file,
        # parse, and process its top-level forms as library declarations.
        import os
        base_dir = _include_base_dir(src_of(decl))
        paths = dbody
        while is_cons(paths):
            path_val = paths.car
            if not is_string(path_val):
                raise SchemeSyntaxError(
                    'include-library-declarations: filename must be a string',
                    src_of(path_val))
            requested = as_string(path_val)
            resolved = os.path.join(
                base_dir, requested) if base_dir else requested
            try:
                f = open(resolved, 'r', encoding='utf-8')
            except FileNotFoundError:
                raise SchemeSyntaxError(
                    'include-library-declarations: file not found: ' + resolved,
                    src_of(decl))
            source = f.read()
            f.close()
            inner_forms = parse(source, resolved)
            for inner in inner_forms:
                _process_one_lib_decl(
                    inner, lib_env, export_names, eval_forms,
                    import_sets, ctx)
            paths = paths.cdr
        return

    if dsym == 'cond-expand':
        # (cond-expand <clause>...).  Each clause is (<feature-req> <decl>...)
        # or (else <decl>...).  Pick the first matching clause and process
        # its declarations recursively.
        cur_clause = dbody
        while is_cons(cur_clause):
            clause = cur_clause.car
            cur_clause = cur_clause.cdr
            if not is_cons(clause):
                raise SchemeSyntaxError(
                    'cond-expand: clause must be a list', src_of(clause))
            req = clause.car
            body = clause.cdr
            if _feature_req_matches(req):
                cur_inner = body
                while is_cons(cur_inner):
                    _process_one_lib_decl(
                        cur_inner.car, lib_env, export_names, eval_forms,
                        import_sets, ctx)
                    cur_inner = cur_inner.cdr
                return
        # No clause matched: silently produce no declarations (R7RS).
        return

    # Unknown declaration keyword: collect for evaluation in lib_env on the
    # main loop.  Covers stray (define ...) forms or other top-level shapes;
    # the later expand step routes define-syntax through the active per-library
    # macro scope (FRAME_EVAL_FORMS expands while _runtime_env_ref is lib_env).
    eval_forms.append(decl)


def _make_runtime_env_setter(target_env):
    """Return a Python-backed primitive that sets _runtime_env_ref[0] =
    target_env.  Used as a define-library wind's before/after so the per-library
    macro scope (for define-syntax) is established on entry and restored on exit,
    on the main loop -- across normal return, error unwind, and continuation
    re-entry alike (replacing the old try/finally restore)."""
    from pyscheme.Expander import _runtime_env_ref

    def setter(ctx2, env2, args2, app_node2):
        _runtime_env_ref[0] = target_env
        return VOID_VALUE
    return setter


def define_library_setup(C, ctx):
    """Pre-pass for (define-library <name> <decl>...): validate, create the
    library env, and process the declarations IN ORDER -- binding imports,
    collecting export names, flattening include / cond-expand -- while
    COLLECTING the begin / unknown-decl forms (unexpanded) into eval_forms for
    evaluation on the main K stack.  Returns (lib_env, eval_forms, export_names,
    key).  Evaluates no form and does NOT swap _runtime_env_ref (no
    define-syntax runs here); the evaluator swaps it around the FRAME_EVAL_FORMS
    phase and registers the library via FRAME_LIB_FINALIZE."""
    from pyscheme.library import library_name_to_key
    from pyscheme.Parser import SchemeSyntaxError
    if not is_cons(C.cdr):
        raise SchemeSyntaxError(
            'define-library: missing library name', src_of(C))
    name_sexpr = C.cdr.car
    decls_cons = C.cdr.cdr
    try:
        key = library_name_to_key(name_sexpr)
    except ValueError as e:
        raise SchemeSyntaxError('define-library: ' + str(e), src_of(C))

    lib_env = Environment(parent=None)
    export_names = []          # Python list of (internal, external) pairs
    eval_forms = []            # begin / unknown-decl forms, in declaration order
    import_sets = []           # import-set sexprs, in declaration order
    d = decls_cons
    while is_cons(d):
        _process_one_lib_decl(d.car, lib_env, export_names, eval_forms,
                              import_sets, ctx)
        d = d.cdr
    return (lib_env, eval_forms, export_names, key, import_sets)


def _finalize_define_library(lib_env, export_names, key, C):
    """Build the exports env from export_names + lib_env and register the
    library under key.  Run by FRAME_LIB_FINALIZE after the library's forms have
    been evaluated on the main loop (so exported names are defined)."""
    from pyscheme.library import library_register
    from pyscheme.Parser import SchemeSyntaxError
    # Build exports env: copy each (internal, external) entry out of
    # lib_env; missing names are hard errors.
    from pyscheme.AST import is_syntax_transformer
    exports_env = Environment(parent=None)
    i = 0
    while i < len(export_names):
        internal = export_names[i][0]
        external = export_names[i][1]
        if intern_symbol(internal) not in lib_env._bindings:
            raise SchemeSyntaxError(
                'define-library: exported name not defined: ' + internal,
                src_of(C))
        val = lib_env.lookup(internal)
        exports_env.bind(external, val)
        # A macro's free-identifier aliases (gensyms bound to the library's
        # def-time values) are part of its hygiene closure.  They were bound in
        # lib_env (the library's own parentless "global"), so carry them into the
        # exports env so they travel with the import and the macro's template
        # references resolve at the use site (R7RS 4.3 referential transparency).
        if is_syntax_transformer(val):
            for fid in val.free_id_map:
                gs = val.free_id_map[fid]
                gs_sid = intern_symbol(gs)
                if gs_sid in lib_env._bindings and gs_sid not in exports_env._bindings:
                    exports_env.bind(gs, lib_env.lookup(gs))
        i = i + 1
    exports_env.freeze()
    library_register(key, exports_env)


def _unwind_winds_on_error(ctx, target_depth):
    """Pop wind entries installed during a failed cek_eval call and call
    their after thunks.  R7RS does not specify what happens when an
    after-thunk itself raises during cleanup; the C++ reference (see
    evaluator.cpp's exception dispatch) deliberately swallows them so
    the original error continues to propagate, and we match that choice
    here for C-port consistency."""
    from pyscheme.primitives.meta import _apply_scheme_proc
    ws = ctx.wind_stack
    while len(ws) > target_depth:
        wf = ws[len(ws) - 1]
        ws.pop()
        try:
            _apply_scheme_proc(wf[1], [], ctx, None, None)
        except BaseException:
            pass


def _cek_loop(expr, env, ctx):
    """The CEK machine's main loop.  Wraps the eval/apply state machine
    in an outer try-while so that catchable exceptions (raise, error, and
    the impl errors SchemeTypeError / SchemeArityError / SchemeUnboundError
    / SchemeSyntaxError) dispatch the handler stack INSIDE the same
    activation record - mirrors the C++ reference's for(;;) try{ ... }
    pattern in evaluator.cpp.  Handler invocation sets CEK state via
    _enter_proc, so a continuation captured during handler execution
    sees the outer K-stack and not just the handler body's frames."""
    from pyscheme.Parser import SchemeSyntaxError
    from pyscheme.Analyzer import SchemeAnalysisError
    # See _CATCHABLE in cek_eval: an analyze error raised DURING evaluation must be
    # routable to an in-scope guard handler (parity with cppScheme2).
    _CATCHABLE_LOCAL = (SchemeRaised, SchemeTypeError, SchemeArityError,
                        SchemeUnboundError, SchemeSyntaxError, SchemeAnalysisError)
    C = expr
    V = None
    E = env
    K = []
    skip_eval = False

    while True:
        try:
            while True:
                ctx._timeout_step = (ctx._timeout_step + 1) & 0xFFFF
                if ctx._timeout_step == 0 and ctx.timeout_at and time.monotonic() > ctx.timeout_at:
                    raise SchemeRuntimeError('Evaluation timed out.')

                # ----- EVAL: descend until a value is produced at a leaf -----
                while True:
                    if skip_eval:
                        skip_eval = False
                        break

                    if is_cons(C):
                        if ctx._instrumented:
                            if ctx._debugging:
                                ctx.debugger.on_expr(C, E, K, ctx)

                        head = C.car

                        if is_symbol(head):
                            # One dict.get on the head's interned symbol id (head[1],
                            # i.e. as_symbol_id) replaces the former ~21-compare string
                            # ladder.  Applications (the hot case) miss -> None and take
                            # the application path immediately; keywords dispatch on the
                            # integer kind below via an inlined switch (optimization #1).
                            sf_kind = _SPECIAL_FORM_KIND.get(head[1])
                            if sf_kind is None:
                                # Symbol head but not a keyword - application.
                                # Walk arg cons chain into Python list.
                                args = _collect_cons_to_list(C.cdr)
                                K.append((FRAME_ARG, args, E, C))
                                C = head
                                continue

                            if sf_kind == _SF_QUOTE:
                                # (quote datum) - datum self-evaluates
                                V = C.cdr.car
                                mark_literal_immutable(V)
                                break

                            elif sf_kind == _SF_LAMBDA:
                                V = _make_closure_from_lambda(C, E)
                                break

                            elif sf_kind == _SF_CASE_LAMBDA:
                                V = _make_case_closure_from_form(C, E)
                                break

                            elif sf_kind == _SF_DELAY or sf_kind == _SF_DELAY_FORCE:
                                # Both produce a lazy promise whose thunk evaluates expr
                                # in the current env.  delay-force tail-chases into a
                                # promise result; plain delay returns it as-is (R7RS 4.2.5).
                                expr = C.cdr.car
                                body = alloc_cons(expr, NIL_VALUE, None)
                                thunk = make_closure([], body, E, None, '')
                                iterative = (sf_kind == _SF_DELAY_FORCE)
                                V = make_promise_lazy(thunk, iterative)
                                break

                            elif sf_kind == _SF_IMPORT:
                                # (import <import-set>...) - resolve each set and bind
                                # each exported name into the current env, loading
                                # library files on the K stack if needed.  Returns
                                # VOID.  Driven by FRAME_IMPORT_STEP (no re-entrant
                                # cek_eval).
                                _imp_sets = _collect_cons_to_list(C.cdr)
                                K.append((FRAME_IMPORT_STEP, _imp_sets, 0, E,
                                          False, 'import: '))
                                V = VOID_VALUE
                                break

                            elif sf_kind == _SF_DEFINE_LIBRARY:
                                # (define-library <name> <decl>...) - install a new
                                # library.  Pre-pass resolves the pure decls and
                                # collects the begin/unknown forms; those evaluate on
                                # the main loop (FRAME_EVAL_FORMS) with _runtime_env_ref
                                # swapped to lib_env -- restored via a wind so it resets
                                # on normal return, error, and continuation escape --
                                # then FRAME_LIB_FINALIZE builds + registers exports.
                                # No re-entrant cek_eval.  Returns VOID.
                                from pyscheme.Expander import _runtime_env_ref
                                _dl = define_library_setup(C, ctx)
                                _dl_env = _dl[0]
                                _dl_install = make_primitive(
                                    '%define-library-install-env',
                                    _make_runtime_env_setter(_dl_env))
                                _dl_restore = make_primitive(
                                    '%define-library-restore-env',
                                    _make_runtime_env_setter(_runtime_env_ref[0]))
                                _runtime_env_ref[0] = _dl_env
                                ctx.wind_stack.append((_dl_install, _dl_restore))
                                K.append((FRAME_DYNAMIC_WIND_AFTER, _dl_restore))
                                K.append(
                                    (FRAME_LIB_FINALIZE, _dl_env, _dl[2], _dl[3], C))
                                K.append((FRAME_EVAL_FORMS, _dl[1], _dl_env,
                                          0, {}, False))
                                # Imports run first (frame-driven, loading library
                                # files on the K stack if needed), before the begin
                                # forms; pushed last so APPLY pops it first.
                                K.append((FRAME_IMPORT_STEP, _dl[4], 0, _dl_env,
                                          False, 'define-library: import: '))
                                V = VOID_VALUE
                                break

                            elif sf_kind == _SF_IF:
                                # (if test then else)  -- expander supplies VOID for missing else
                                K.append(
                                    (FRAME_IF, C.cdr.cdr.car, C.cdr.cdr.cdr.car, E))
                                C = C.cdr.car
                                continue

                            elif sf_kind == _SF_DEFINE:
                                # (define name value)
                                K.append((FRAME_DEFINE, C.cdr.car, E))
                                C = C.cdr.cdr.car
                                continue

                            elif sf_kind == _SF_SET:
                                # (set! name value)
                                name_sexpr = C.cdr.car
                                K.append((FRAME_SET, name_sexpr,
                                         E, src_of(name_sexpr)))
                                C = C.cdr.cdr.car
                                continue

                            elif sf_kind == _SF_BEGIN:
                                body = C.cdr
                                if is_nil(body):
                                    V = VOID_VALUE
                                    break
                                C = body.car
                                if is_cons(body.cdr):
                                    K.append((FRAME_SEQ, body.cdr, E))
                                continue

                            elif sf_kind == _SF_WHEN:
                                # (when test body...)
                                K.append((FRAME_WHEN, C.cdr.cdr, E))
                                C = C.cdr.car
                                continue

                            elif sf_kind == _SF_UNLESS:
                                # (unless test body...)
                                K.append((FRAME_UNLESS, C.cdr.cdr, E))
                                C = C.cdr.car
                                continue

                            elif sf_kind == _SF_AND:
                                body = C.cdr
                                if is_nil(body):
                                    V = make_boolean(True)
                                    break
                                if is_cons(body.cdr):
                                    K.append((FRAME_AND, body.cdr, E))
                                C = body.car
                                continue

                            elif sf_kind == _SF_OR:
                                body = C.cdr
                                if is_nil(body):
                                    V = make_boolean(False)
                                    break
                                if is_cons(body.cdr):
                                    K.append((FRAME_OR, body.cdr, E))
                                C = body.car
                                continue

                            elif sf_kind == _SF_COND:
                                # (cond clauses...) - analyzer ensures non-empty
                                clauses = C.cdr
                                first = clauses.car
                                rest = clauses.cdr
                                kind = _classify_cond_clause(first, E)
                                if kind[0] == 'else':
                                    body = kind[1]
                                    C = body.car
                                    if is_cons(body.cdr):
                                        K.append((FRAME_SEQ, body.cdr, E))
                                    continue
                                K.append((FRAME_COND, first, rest, E))
                                C = kind[1]   # the test expression
                                continue

                            elif sf_kind == _SF_CASE:
                                # (case <key> <clause>...) - analyzer ensures shape:
                                # key present, at least one clause, each clause a proper
                                # list starting with a datum-list or 'else'.
                                clauses = C.cdr.cdr
                                first = clauses.car
                                rest = clauses.cdr
                                K.append((FRAME_CASE, first, rest, E))
                                C = C.cdr.car   # the key expression
                                continue

                            elif sf_kind == _SF_LET:
                                # (let [name] bindings body...)
                                if is_symbol(C.cdr.car):
                                    # named let: desugar at runtime
                                    # (let name ((v1 e1) ...) body...) ==
                                    #   (letrec ((name (lambda (v1 ...) body...))) (name e1 ...))
                                    loop_name_sym = C.cdr.car
                                    loop_name = as_symbol(loop_name_sym)
                                    bindings_cons = C.cdr.cdr.car
                                    body_cons = C.cdr.cdr.cdr
                                    pairs = _collect_let_bindings(
                                        bindings_cons)
                                    params = []
                                    init_exprs = []
                                    i = 0
                                    while i < len(pairs):
                                        params.append(pairs[i][0])
                                        init_exprs.append(pairs[i][1])
                                        i = i + 1
                                    loop_env = Environment(E)
                                    loop_env.bind(loop_name, VOID_VALUE)
                                    closure = make_closure(params, body_cons, loop_env,
                                                           None, '')
                                    loop_env.bind(loop_name, closure)
                                    # Now evaluate (name init1 init2 ...) - i.e., apply closure to init values
                                    # Set up FRAME_ARG-style call: but we don't have an "AST" for this synthesized call.
                                    # Use FRAME_ARG with init_exprs as args list and the current C as app_node.
                                    V = closure
                                    # We want to apply closure to init_exprs.  Push FRAME_ARG with V as fn.
                                    K.append(
                                        (FRAME_ARG, init_exprs, loop_env, C))
                                    break
                                bindings_cons = C.cdr.car
                                body_cons = C.cdr.cdr
                                pairs = _collect_let_bindings(bindings_cons)
                                if not pairs:
                                    # No bindings; just run body in current env
                                    C = body_cons.car
                                    if is_cons(body_cons.cdr):
                                        K.append((FRAME_SEQ, body_cons.cdr, E))
                                    continue
                                names = []
                                val_exprs = []
                                i = 0
                                while i < len(pairs):
                                    names.append(pairs[i][0])
                                    val_exprs.append(pairs[i][1])
                                    i = i + 1
                                remaining = []
                                i = 1
                                while i < len(val_exprs):
                                    remaining.append(val_exprs[i])
                                    i = i + 1
                                K.append(
                                    (FRAME_LET, names, [], remaining, body_cons, E))
                                C = val_exprs[0]
                                # E stays at outer env - all val_exprs evaluate in it
                                continue

                            elif sf_kind == _SF_LET_STAR:
                                # (let* bindings body...) - each val sees prior bindings
                                bindings_cons = C.cdr.car
                                body_cons = C.cdr.cdr
                                pairs = _collect_let_bindings(bindings_cons)
                                if not pairs:
                                    C = body_cons.car
                                    if is_cons(body_cons.cdr):
                                        K.append((FRAME_SEQ, body_cons.cdr, E))
                                    continue
                                remaining = []
                                i = 1
                                while i < len(pairs):
                                    remaining.append(pairs[i])
                                    i = i + 1
                                K.append((FRAME_LET_STAR, pairs[0][0], remaining,
                                          body_cons, E))
                                C = pairs[0][1]
                                continue

                            elif sf_kind == _SF_LETREC:
                                # (letrec bindings body...) - all names visible in val_exprs
                                bindings_cons = C.cdr.car
                                body_cons = C.cdr.cdr
                                pairs = _collect_let_bindings(bindings_cons)
                                if not pairs:
                                    C = body_cons.car
                                    if is_cons(body_cons.cdr):
                                        K.append((FRAME_SEQ, body_cons.cdr, E))
                                    continue
                                new_env = Environment(E)
                                i = 0
                                while i < len(pairs):
                                    new_env.bind(pairs[i][0], VOID_VALUE)
                                    i = i + 1
                                remaining = []
                                i = 1
                                while i < len(pairs):
                                    remaining.append(pairs[i])
                                    i = i + 1
                                K.append((FRAME_LETREC, pairs[0][0], remaining,
                                          body_cons, new_env))
                                C = pairs[0][1]
                                E = new_env
                                continue

                            elif sf_kind == _SF_TRACE:
                                tracer = ctx.tracer
                                args_cons = C.cdr
                                if is_nil(args_cons):
                                    V = _sorted_sym_list(tracer.get_fns())
                                    break
                                cur = args_cons
                                while is_cons(cur):
                                    sym = cur.car
                                    if not is_symbol(sym):
                                        raise SchemeTypeError(
                                            'trace: arguments must be symbols', C)
                                    tracer.add_fn(as_symbol(sym))
                                    cur = cur.cdr
                                V = _sorted_sym_list(tracer.get_fns())
                                break

                            elif sf_kind == _SF_UNTRACE:
                                tracer = ctx.tracer
                                args_cons = C.cdr
                                if is_nil(args_cons):
                                    tracer.remove_all()
                                else:
                                    cur = args_cons
                                    while is_cons(cur):
                                        sym = cur.car
                                        if not is_symbol(sym):
                                            raise SchemeTypeError(
                                                'untrace: arguments must be symbols', C)
                                        tracer.remove_fn(as_symbol(sym))
                                        cur = cur.cdr
                                V = _sorted_sym_list(tracer.get_fns())
                                break

                        # head is not a symbol - application (e.g., immediate lambda).
                        args = _collect_cons_to_list(C.cdr)
                        K.append((FRAME_ARG, args, E, C))
                        C = head
                        continue

                    if is_symbol(C):
                        try:
                            V = E.lookup_id(as_symbol_id(C))
                        except SchemeUnboundError as e:
                            e.src = src_of(C)
                            raise
                        break

                    # Atom (literal), NIL, VOID, or any other tagged value: self-eval.
                    V = C
                    break

                # ----- APPLY: consume V against the top frame -----
                while True:
                    if not K:
                        return V

                    frame = K.pop()
                    ftag = frame[0]

                    # R7RS 6.10: passing multi-values to a continuation not created
                    # by call-with-values is an error.  But not every frame is a
                    # single-value continuation: FRAME_SEQ discards V (begin/body
                    # sequencing - 0-value context, not 1-value), and the wind /
                    # handler pop frames are transparent (they preserve V across
                    # an effect and forward it to whatever's outside).  Only frames
                    # that actually consume V as a single value error here.
                    if is_multi_values(V) and ftag not in _MULTI_VALUES_OK_FRAMES:
                        raise SchemeTypeError(
                            'multiple values delivered to a single-value context',
                            src_of(V))

                    if ftag == FRAME_DEFINE:
                        E = frame[2]
                        try:
                            E.bind_id(as_symbol_id(frame[1]), V)
                        except SchemeTypeError as e:
                            e.src = src_of(frame[1])
                            raise
                        V = VOID_VALUE
                        continue

                    if ftag == FRAME_SET:
                        E = frame[2]
                        try:
                            E.set_id(as_symbol_id(frame[1]), V)
                        except (SchemeUnboundError, SchemeTypeError) as e:
                            e.src = frame[3]
                            raise
                        V = VOID_VALUE
                        continue

                    if ftag == FRAME_IF:
                        if isFalse(V):
                            C = frame[2]
                        else:
                            C = frame[1]
                        E = frame[3]
                        break

                    if ftag == FRAME_DYNAMIC_WIND_AFTER:
                        # frame = (FRAME_DYNAMIC_WIND_AFTER, after_thunk)
                        # The body has produced its value (now in V).  Pop the wind
                        # entry, then run after_thunk on the K stack (not via a
                        # re-entrant _apply_scheme_proc) for its effect, preserving the
                        # body result across it with FRAME_RESTORE_VALUE.
                        after_thunk = frame[1]
                        if ctx.wind_stack:
                            ctx.wind_stack.pop()
                        K.append((FRAME_RESTORE_VALUE, V))
                        result = _enter_proc(after_thunk, [], ctx, None, None)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_RESTORE_VALUE:
                        # frame = (FRAME_RESTORE_VALUE, saved_value)
                        # Discard the incoming V (a wind after-thunk's result) and
                        # reinstate the value saved when the frame was pushed.
                        V = frame[1]
                        continue

                    if ftag == FRAME_DYNAMIC_WIND_BEFORE_DONE:
                        # frame = (FRAME_DYNAMIC_WIND_BEFORE_DONE, before, thunk, after)
                        # The before-thunk has completed (V is its result, discarded).
                        # Now the dynamic extent is active: install the wind entry and
                        # after-frame, then tail-call the body thunk on the K stack.
                        before = frame[1]
                        thunk = frame[2]
                        after = frame[3]
                        ctx.wind_stack.append((before, after))
                        K.append((FRAME_DYNAMIC_WIND_AFTER, after))
                        result = _enter_proc(thunk, [], ctx, None, None)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_PARAMETERIZE_STEP:
                        # frame = (FRAME_PARAMETERIZE_STEP, params, raw_vals, acc,
                        #          index, thunk, app_node, awaiting)
                        # Drives parameterize's value converters on the K stack: for
                        # each parameter with a converter, tail-call it with the raw
                        # value and collect the result; parameters without a converter
                        # take the raw value directly.  When every value is converted,
                        # install the winds (FRAME_DYNAMIC_WIND_AFTER + wind_stack) and
                        # tail-call the body thunk.  awaiting=True means V holds the
                        # converter result for params[index] and must be collected.
                        p_params = frame[1]
                        p_raw = frame[2]
                        p_acc = frame[3]
                        p_i = frame[4]
                        p_thunk = frame[5]
                        p_app = frame[6]
                        p_awaiting = frame[7]
                        if p_awaiting:
                            p_acc = p_acc + [V]
                            p_i = p_i + 1
                        # Parameters needing no converter take the raw value directly.
                        while (p_i < len(p_params)
                               and as_parameter_converter(p_params[p_i]) is None):
                            p_acc = p_acc + [p_raw[p_i]]
                            p_i = p_i + 1
                        if p_i < len(p_params):
                            # params[p_i] has a converter: run it on the K stack.
                            conv = as_parameter_converter(p_params[p_i])
                            K.append((FRAME_PARAMETERIZE_STEP, p_params, p_raw,
                                      p_acc, p_i, p_thunk, p_app, True))
                            result = _enter_proc(conv, [p_raw[p_i]], ctx, None, p_app)
                            sig = _apply_enter_result(result, K, V)
                            if sig[0] == 'apply':
                                V = sig[1]
                                continue
                            C = sig[1]
                            E = sig[2]
                            break
                        # Every value converted: install winds, tail-call the body.
                        _pw = _finalize_parameterize_winds(p_params, p_acc, ctx)
                        ctx.wind_stack.append((_pw[0], _pw[1]))
                        K.append((FRAME_DYNAMIC_WIND_AFTER, _pw[1]))
                        result = _enter_proc(p_thunk, [], ctx, None, p_app)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_WIND_STEP:
                        # frame = (FRAME_WIND_STEP, ops, index, cont, value)
                        # Drives a continuation jump's wind walk on the K stack: each
                        # op exits an extent (pop wind_stack, run its after) or enters
                        # one (push wind_stack, run its before), with the thunk run via
                        # _enter_proc rather than a re-entrant _apply_scheme_proc.  The
                        # incoming V (a wind thunk's result) is discarded.  When the ops
                        # are exhausted, install the continuation: restore the handler /
                        # shadow stacks, swap in its K, and deliver its value.
                        w_ops = frame[1]
                        w_i = frame[2]
                        w_cont = frame[3]
                        w_val = frame[4]
                        if w_i < len(w_ops):
                            op = w_ops[w_i]
                            if op[0] == 'exit':
                                if ctx.wind_stack:
                                    ctx.wind_stack.pop()
                                w_thunk = op[1]
                            else:  # 'enter'
                                ctx.wind_stack.append(op[2])
                                w_thunk = op[1]
                            K.append((FRAME_WIND_STEP, w_ops, w_i + 1,
                                      w_cont, w_val))
                            result = _enter_proc(w_thunk, [], ctx, None, None)
                            sig = _apply_enter_result(result, K, V)
                            if sig[0] == 'apply':
                                V = sig[1]
                                continue
                            C = sig[1]
                            E = sig[2]
                            break
                        # All wind thunks have run: install the continuation.
                        _restore_handler_stack(
                            ctx, as_continuation_handlers(w_cont))
                        K = list(as_continuation_k(w_cont))
                        _restore_shadow_stack(
                            ctx, as_continuation_shadow(w_cont))
                        V = w_val
                        continue

                    if ftag == FRAME_ERROR_UNWIND:
                        # frame = (FRAME_ERROR_UNWIND, afters, index, exc)
                        # Runs the dynamic-wind after-thunks for the extents between a
                        # raise and its handler ON THE K STACK (the except block below
                        # collected them in one scan, preserving its reinstall
                        # accounting, and left the handler frame installed below so it
                        # still protects these afters).  Each after's result is
                        # discarded.  When the afters are exhausted, re-raise the
                        # original condition: the still-installed handler frame is now
                        # at the top of K, so the except block dispatches it via its
                        # no-afters inline path.  Propagate semantics (matches Chez;
                        # R7RS-unspecified): an after that raises becomes the new
                        # in-flight condition, caught by that same handler.
                        eu_afters = frame[1]
                        eu_i = frame[2]
                        eu_exc = frame[3]
                        if eu_i < len(eu_afters):
                            K.append((FRAME_ERROR_UNWIND, eu_afters,
                                      eu_i + 1, eu_exc))
                            result = _enter_proc(
                                eu_afters[eu_i], [], ctx, None, None)
                            sig = _apply_enter_result(result, K, V)
                            if sig[0] == 'apply':
                                V = sig[1]
                                continue
                            C = sig[1]
                            E = sig[2]
                            break
                        raise eu_exc

                    if ftag == FRAME_EVAL_FORMS:
                        # frame = (FRAME_EVAL_FORMS, forms, env, index, static_env,
                        #          do_analyze)
                        # Evaluates a Python list of top-level forms in sequence ON
                        # THE MAIN K STACK (load / library loading), instead of a
                        # re-entrant cek_eval per form.  Each form is expanded and
                        # evaluated; its result is discarded.  do_analyze gates the
                        # Analyzer pass (load analyzes + accumulates defines into
                        # static_env; define-library's begin/decl forms are not
                        # analyzed, matching the old cek_eval(expand(form)) path).
                        # Yields VOID when the forms are exhausted.
                        ef_forms = frame[1]
                        ef_env = frame[2]
                        ef_i = frame[3]
                        ef_static = frame[4]
                        ef_do_analyze = frame[5]
                        if ef_i >= len(ef_forms):
                            V = VOID_VALUE
                            continue
                        from pyscheme.Expander import expand
                        expanded = expand(ef_forms[ef_i])
                        if ef_do_analyze:
                            from pyscheme.Analyzer import (
                                analyze, extend_static_env_with_define)
                            analyze(expanded, ef_static)
                            extend_static_env_with_define(ef_static, expanded)
                        K.append((FRAME_EVAL_FORMS, ef_forms, ef_env,
                                  ef_i + 1, ef_static, ef_do_analyze))
                        C = expanded
                        E = ef_env
                        break

                    if ftag == FRAME_LIB_FINALIZE:
                        # frame = (FRAME_LIB_FINALIZE, lib_env, export_names, key, C)
                        # The library's forms have evaluated on the main loop; build +
                        # register its exports.  Reached only on normal completion (a
                        # library form that raised unwinds past this frame), so a
                        # failed library is not registered -- as before.  The
                        # _runtime_env_ref restore rides a wind beneath this frame.
                        _finalize_define_library(
                            frame[1], frame[2], frame[3], frame[4])
                        V = VOID_VALUE
                        continue

                    if ftag == FRAME_IMPORT_STEP:
                        # frame = (FRAME_IMPORT_STEP, sets, index, env, post_load,
                        #          err_prefix)
                        # Resolves each import-set and binds its exports into env.
                        # When a set names an unregistered library, frame-drives a
                        # load (FRAME_ENSURE_LOADED) then retries (post_load=True),
                        # so library files evaluate on the main K stack instead of a
                        # re-entrant cek_eval.  Used for top-level import and (via the
                        # define-library dispatch) library imports.
                        im_sets = frame[1]
                        im_i = frame[2]
                        im_env = frame[3]
                        im_post = frame[4]
                        im_prefix = frame[5]
                        if im_i >= len(im_sets):
                            V = VOID_VALUE
                            continue
                        from pyscheme.library import (
                            resolve_import_set, library_name_to_key,
                            library_registered_p)
                        from pyscheme.Parser import SchemeSyntaxError
                        import_set = im_sets[im_i]
                        try:
                            bindings = resolve_import_set(import_set)
                        except ValueError as ie:
                            _isrc = src_of(import_set)
                            if not im_post and is_cons(import_set):
                                try:
                                    _ikey = library_name_to_key(import_set)
                                except ValueError:
                                    _ikey = None
                                if (_ikey is not None
                                        and not library_registered_p(_ikey)):
                                    K.append((FRAME_IMPORT_STEP, im_sets, im_i,
                                              im_env, True, im_prefix))
                                    K.append((FRAME_ENSURE_LOADED, _ikey,
                                              import_set, _library_load_path(), 0))
                                    continue
                            raise SchemeSyntaxError(
                                im_prefix + str(ie), _isrc)
                        for n in bindings:
                            im_env.bind(n, bindings[n])
                        K.append((FRAME_IMPORT_STEP, im_sets, im_i + 1,
                                  im_env, False, im_prefix))
                        continue

                    if ftag == FRAME_ENSURE_LOADED:
                        # frame = (FRAME_ENSURE_LOADED, key, name_sexpr, dirs, di)
                        # Walks the load path for a library: per dir loads <key>.py
                        # (native) then drives <key>.sld's forms on the K stack via
                        # FRAME_EVAL_FORMS, re-checking registration after each dir.
                        # Yields when the library is registered or the dirs run out.
                        el_key = frame[1]
                        el_name = frame[2]
                        el_dirs = frame[3]
                        el_di = frame[4]
                        from pyscheme.library import library_registered_p
                        if library_registered_p(el_key) or el_di >= len(el_dirs):
                            V = VOID_VALUE
                            continue
                        import os as _os
                        _base = el_dirs[el_di]
                        _bpath = _os.path.join(*el_key.split('.'))
                        _prefix = (_os.path.join(_base, _bpath)
                                   if _base else _bpath)
                        if _os.path.isfile(_prefix + '.py'):
                            _load_py_extension(_prefix + '.py')
                        _sld = _prefix + '.sld'
                        K.append((FRAME_ENSURE_LOADED, el_key, el_name,
                                  el_dirs, el_di + 1))
                        if _os.path.isfile(_sld):
                            from pyscheme.Parser import parse
                            _fh = open(_sld, 'r', encoding='utf-8')
                            _src = _fh.read()
                            _fh.close()
                            K.append((FRAME_EVAL_FORMS, parse(_src, _sld),
                                      Environment(parent=None), 0, {}, False))
                        continue

                    if ftag == FRAME_CWV_CONSUMER:
                        # frame = (FRAME_CWV_CONSUMER, consumer, app_node)
                        # V is whatever the producer returned.  Unpack multi-values (if
                        # applicable) and tail-call the consumer via _enter_proc.
                        consumer = frame[1]
                        app_node = frame[2]
                        if is_multi_values(V):
                            consumer_args = as_multi_values_list(V)
                        else:
                            consumer_args = [V]
                        result = _enter_proc(
                            consumer, consumer_args, ctx, E, app_node)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_HOF_STEP:
                        # frame = (FRAME_HOF_STEP, mode, proc, cursors, acc,
                        #          app_node, pending)
                        # Drives map / for-each / filter on the K stack so nested
                        # higher-order calls cost K (heap) rather than Python stack.
                        #   mode    - PRIM_MAP / PRIM_FOR_EACH / PRIM_FILTER
                        #   cursors - tuple of the cons cells still to be read, one
                        #             per list argument (one element for filter)
                        #   acc     - reversed result list built so far, an immutable
                        #             cons snapshot so a continuation captured mid-
                        #             iteration can re-run correctly
                        #   pending - filter's element under test, or _HOF_START on
                        #             the first entry (when V is the call's last arg,
                        #             not a callback result)
                        hof_mode = frame[1]
                        hof_proc = frame[2]
                        hof_cursors = frame[3]
                        hof_acc = frame[4]
                        app_node = frame[5]
                        hof_pending = frame[6]
                        if hof_pending is not _HOF_START:
                            if hof_mode == PRIM_MAP:
                                hof_acc = alloc_cons(V, hof_acc, None)
                            elif hof_mode == PRIM_FILTER:
                                if not isFalse(V):
                                    hof_acc = alloc_cons(hof_pending, hof_acc, None)
                            # PRIM_FOR_EACH discards V.
                        hof_ready = True
                        for hof_c in hof_cursors:
                            if not is_cons(hof_c):
                                hof_ready = False
                                break
                        if not hof_ready:
                            # R7RS 6.10: unequal-length lists stop at the shortest;
                            # only a genuinely improper (non-nil, non-pair) tail errors.
                            for hof_c in hof_cursors:
                                if not is_cons(hof_c) and not is_nil(hof_c):
                                    if hof_mode == PRIM_FILTER:
                                        hof_msg = 'filter: list argument must be a proper list'
                                    elif hof_mode == PRIM_FOR_EACH:
                                        hof_msg = 'for-each: list arguments must be proper lists'
                                    else:
                                        hof_msg = 'map: list arguments must be proper lists'
                                    raise SchemeTypeError(
                                        hof_msg,
                                        src_of(app_node) if app_node is not None else None)
                            if hof_mode == PRIM_FOR_EACH:
                                V = VOID_VALUE
                            else:
                                # acc is reversed; rebuild in original order.
                                hof_res = NIL_VALUE
                                hof_cur = hof_acc
                                while is_cons(hof_cur):
                                    hof_res = alloc_cons(hof_cur.car, hof_res, None)
                                    hof_cur = hof_cur.cdr
                                V = hof_res
                            continue
                        hof_row = [hof_c.car for hof_c in hof_cursors]
                        hof_next = tuple(hof_c.cdr for hof_c in hof_cursors)
                        hof_next_pending = (hof_row[0] if hof_mode == PRIM_FILTER
                                            else None)
                        K.append((FRAME_HOF_STEP, hof_mode, hof_proc, hof_next,
                                  hof_acc, app_node, hof_next_pending))
                        result = _enter_proc(hof_proc, hof_row, ctx, E, app_node)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_HOF_STEP_IDX:
                        # frame = (FRAME_HOF_STEP_IDX, mode, proc, seqs, idx,
                        #          shortest, acc, app_node, started)
                        # Indexed analogue of FRAME_HOF_STEP, for vector-map /
                        # vector-for-each / string-map / string-for-each.
                        #   seqs     - tuple of the source vector/string values
                        #   idx      - next element index to dispatch
                        #   shortest - precomputed min sequence length
                        #   acc      - reversed result list (map variants only), an
                        #              immutable cons snapshot for re-entrancy
                        #   started  - False on the first entry (V is the call's last
                        #              argument then, not a callback result)
                        hof_mode = frame[1]
                        hof_proc = frame[2]
                        hof_seqs = frame[3]
                        hof_idx = frame[4]
                        hof_short = frame[5]
                        hof_acc = frame[6]
                        app_node = frame[7]
                        hof_started = frame[8]
                        if hof_started:
                            if hof_mode == PRIM_VECTOR_MAP:
                                hof_acc = alloc_cons(V, hof_acc, None)
                            elif hof_mode == PRIM_STRING_MAP:
                                if not is_character(V):
                                    raise SchemeTypeError(
                                        'string-map: proc must return a character',
                                        src_of(app_node) if app_node is not None else None)
                                hof_acc = alloc_cons(V, hof_acc, None)
                            # vector-for-each / string-for-each discard V.
                        if hof_idx >= hof_short:
                            if (hof_mode == PRIM_VECTOR_FOR_EACH
                                    or hof_mode == PRIM_STRING_FOR_EACH):
                                V = VOID_VALUE
                            elif hof_mode == PRIM_VECTOR_MAP:
                                # acc is reversed; rebuild element order.
                                hof_items = []
                                hof_cur = hof_acc
                                while is_cons(hof_cur):
                                    hof_items.append(hof_cur.car)
                                    hof_cur = hof_cur.cdr
                                hof_items.reverse()
                                V = make_vector(hof_items)
                            else:  # PRIM_STRING_MAP
                                hof_chars = []
                                hof_cur = hof_acc
                                while is_cons(hof_cur):
                                    hof_chars.append(as_character(hof_cur.car))
                                    hof_cur = hof_cur.cdr
                                hof_chars.reverse()
                                V = make_string(''.join(hof_chars))
                            continue
                        hof_row = []
                        for hof_seq in hof_seqs:
                            if (hof_mode == PRIM_VECTOR_MAP
                                    or hof_mode == PRIM_VECTOR_FOR_EACH):
                                hof_row.append(as_vector_items(hof_seq)[hof_idx])
                            else:
                                hof_row.append(
                                    make_character(as_string(hof_seq)[hof_idx]))
                        K.append((FRAME_HOF_STEP_IDX, hof_mode, hof_proc, hof_seqs,
                                  hof_idx + 1, hof_short, hof_acc, app_node, True))
                        result = _enter_proc(hof_proc, hof_row, ctx, E, app_node)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_SEARCH_STEP:
                        # frame = (FRAME_SEARCH_STEP, mode, proc, target,
                        #          cursor, app_node, started)
                        # Drives the 3-arg member / assoc comparator search on the
                        # K stack so a deeply-recursing comparator costs K (heap)
                        # rather than the Python stack; mirrors FRAME_HOF_STEP.
                        #   mode    - PRIM_MEMBER / PRIM_ASSOC
                        #   target  - the object being searched for
                        #   cursor  - the cons cell currently under test
                        #   started - False on the first entry, where V is the
                        #             call's last argument, not a comparator result
                        s_mode = frame[1]
                        s_proc = frame[2]
                        s_target = frame[3]
                        s_cursor = frame[4]
                        app_node = frame[5]
                        s_started = frame[6]
                        s_name = 'member' if s_mode == PRIM_MEMBER else 'assoc'
                        if s_started:
                            # V is the comparator's verdict for the entry at cursor.
                            if not isFalse(V):
                                # member returns the matching sublist; assoc the pair.
                                V = (s_cursor if s_mode == PRIM_MEMBER
                                     else s_cursor.car)
                                continue
                            s_cursor = s_cursor.cdr
                        if not is_cons(s_cursor):
                            if not is_nil(s_cursor):
                                raise SchemeTypeError(
                                    s_name + ': second argument must be a proper list',
                                    src_of(app_node) if app_node is not None else None)
                            V = make_boolean(False)
                            continue
                        if s_mode == PRIM_MEMBER:
                            s_item = s_cursor.car
                        else:
                            s_entry = s_cursor.car
                            if not is_cons(s_entry):
                                raise SchemeTypeError(
                                    s_name + ': alist entries must be pairs',
                                    src_of(app_node) if app_node is not None else None)
                            s_item = s_entry.car
                        K.append((FRAME_SEARCH_STEP, s_mode, s_proc, s_target,
                                  s_cursor, app_node, True))
                        result = _enter_proc(s_proc, [s_target, s_item], ctx, E, app_node)
                        sig = _apply_enter_result(result, K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_POP_HANDLER:
                        # Thunk returned normally; pop the installed handler and let V
                        # flow.  No work needed beyond popping the stack entry.
                        if ctx.handler_stack:
                            ctx.handler_stack.pop()
                        continue

                    if ftag == FRAME_GUARD:
                        # Guard body returned normally; pop the guard handler, V flows.
                        if ctx.handler_stack:
                            ctx.handler_stack.pop()
                        continue

                    if ftag == FRAME_REINSTALL_HANDLER:
                        # raise-continuable's handler returned; push it back so nested
                        # raises in the enclosing with-exception-handler scope still
                        # see it.  V (handler's return) flows back to the raise-
                        # continuable's call site unchanged.
                        ctx.handler_stack.append(frame[1])
                        continue

                    if ftag == FRAME_NONCONTIN_RETURN:
                        # Handler returned from a non-continuable raise - R7RS §6.11.
                        # frame[1] is the original raised value (included as irritant).
                        from pyscheme.AST import make_error_object
                        raise SchemeRaised(
                            make_error_object(
                                'exception handler returned', [frame[1]]),
                            None, continuable=False)

                    if ftag == FRAME_MAKE_PARAMETER:
                        # frame = (FRAME_MAKE_PARAMETER, converter)
                        # V is the converter's return value; wrap it as a Parameter.
                        V = make_parameter(V, frame[1])
                        continue

                    if ftag == FRAME_FORCE_RESULT:
                        # frame = (FRAME_FORCE_RESULT, promise)
                        # The promise's thunk has produced V.  Resolve or become, and
                        # iterate if we ended up with another unforced promise.
                        # Only delay-force promises tail-chase into a promise result;
                        # plain delay resolves to it as-is (R7RS 4.2.5).
                        p = frame[1]
                        if as_promise_is_iterative(p) and is_promise(V):
                            promise_become(p, V)
                            if as_promise_is_done(p):
                                V = as_promise_payload(p)
                                continue
                            # Still not done - iterate: push another FORCE_RESULT and
                            # tail-call the (now inner) thunk.
                            K.append((FRAME_FORCE_RESULT, p))
                            thunk = as_promise_payload(p)
                            result = _enter_proc(thunk, [], ctx, E, None)
                            sig = _apply_enter_result(result, K, V)
                            if sig[0] == 'apply':
                                V = sig[1]
                                continue
                            C = sig[1]
                            E = sig[2]
                            break
                        promise_resolve(p, V)
                        continue

                    if ftag == FRAME_ARG:
                        # frame = (FRAME_ARG, args_list, env, app_node)
                        args = frame[1]
                        saved_env = frame[2]
                        app_node = frame[3]
                        if len(args) == 0:
                            if is_continuation(V):
                                K.append((FRAME_WIND_STEP,
                                          _compute_wind_ops(
                                              ctx, as_continuation_wind(V)),
                                          0, V, _continuation_value(V, [])))
                                continue
                            pv = _apply_parameter_if(V, 0, app_node)
                            if pv is not None:
                                V = pv
                                continue
                            _trc_printed = False
                            _trc_name = None
                            _trc_depth = 0
                            if ctx._instrumented:
                                _trc = ctx.tracer
                                if _trc._active:
                                    if is_symbol(app_node.car):
                                        _trc_name = as_symbol(app_node.car)
                                    if _trc_name is None and is_primitive(V):
                                        _trc_name = as_primitive_name(V)
                                    if _trc_name is not None and _trc_name in _trc._fns_to_trace:
                                        _trc_depth = _trc._depth
                                        _trc_printed = _trc.trace_enter(
                                            _trc_name, [], _trc_depth, ctx.outStrm)
                                        if _trc_printed:
                                            _trc._depth = _trc_depth + 1
                            if is_primitive(V):
                                if as_primitive_kind(V) == PRIM_CONTINUATION_DEPTH:
                                    # Internal probe: report the live continuation-stack
                                    # (K) length so tail-call tests can assert bounded
                                    # continuation space.  K is in scope here but not in
                                    # a normal primitive body.
                                    V = make_integer(len(K))
                                    continue
                                result = as_primitive_fn(V)(
                                    ctx, saved_env, [], app_node)
                                if _trc_printed:
                                    ctx.tracer._depth = _trc_depth
                                    ctx.tracer.trace_exit(
                                        _trc_name, result, _trc_depth, ctx.outStrm)
                                V = result
                                continue
                            r = _apply_value(V, [], app_node)
                            _shadow_push(ctx, K, app_node)
                            if _trc_printed:
                                K.append(
                                    (FRAME_TRACE_EXIT, _trc_name, _trc_depth))
                            E = r.new_env
                            C = r.body.car
                            if is_cons(r.body.cdr):
                                K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                            break
                        # Push baton FRAME_CALL and start on first arg.
                        remaining = []
                        i = 1
                        while i < len(args):
                            remaining.append(args[i])
                            i = i + 1
                        K.append(
                            (FRAME_CALL, V, [], remaining, saved_env, app_node))
                        C = args[0]
                        E = saved_env
                        break

                    if ftag == FRAME_CALL:
                        # frame = (FRAME_CALL, fn_value, collected, remaining, env, app_node)
                        fn_value = frame[1]
                        collected = frame[2]
                        remaining = frame[3]
                        saved_env = frame[4]
                        app_node = frame[5]
                        original_fn = fn_value
                        new_collected = list(collected)
                        new_collected.append(V)
                        if len(remaining) == 0:
                            # Invoke continuation: replace K with its snapshot.
                            if is_continuation(fn_value):
                                # Drive the wind walk on the K stack via
                                # FRAME_WIND_STEP, which then restores the handler /
                                # shadow stacks, swaps in the continuation's K, and
                                # delivers its value -- entirely on the one loop.
                                K.append((FRAME_WIND_STEP,
                                          _compute_wind_ops(
                                              ctx, as_continuation_wind(fn_value)),
                                          0, fn_value,
                                          _continuation_value(fn_value, new_collected)))
                                continue
                            # #2: classify the operator once, then dispatch on the
                            # integer kind below instead of ~15 _is_X_primitive name
                            # comparisons.  Ordinary primitives and closures get
                            # PRIM_ORDINARY and fall straight through.  Cases that
                            # rewrite fn_value re-classify it so a later case can
                            # catch the new operator, preserving the original
                            # top-to-bottom fall-through semantics.
                            kind = (as_primitive_kind(fn_value)
                                    if is_primitive(fn_value) else PRIM_ORDINARY)
                            # Capture continuation: call/cc intercepted before its body.
                            if kind == PRIM_CALL_CC:
                                if len(new_collected) != 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(as_primitive_name(fn_value),
                                                           1, 1, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                cont = make_continuation(
                                    list(K), list(ctx.wind_stack), list(
                                        ctx.handler_stack),
                                    list(ctx.shadow_stack))
                                user_proc = new_collected[0]
                                # Apply the user proc with the continuation as its arg,
                                # reusing the normal dispatch paths below.
                                fn_value = user_proc
                                new_collected = [cont]
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # apply: splice the list argument, rewrite the dispatch so
                            # the target proc is tail-called through the normal CEK
                            # path.  Avoids the Python stack frame that _prim_apply's
                            # re-entry into cek_eval would create.  Loops so (apply apply
                            # ...) collapses rather than firing the stub body.
                            while kind == PRIM_APPLY:
                                _apply_result = _unpack_apply_args(
                                    new_collected, app_node)
                                proc = _apply_result[0]
                                flat_args = _apply_result[1]
                                if not (is_primitive(proc) or is_closure(proc)
                                        or is_case_closure(proc) or is_continuation(proc)
                                        or is_parameter(proc) or is_record_accessor(proc)
                                        or is_record_mutator(proc)):
                                    raise SchemeTypeError(
                                        'apply: first argument must be a procedure', app_node)
                                fn_value = proc
                                new_collected = flat_args
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # map / for-each / filter, the vector/string variants,
                            # and 3-arg member / assoc: drive the per-element calls
                            # through the K stack (FRAME_HOF_STEP / _IDX /
                            # FRAME_SEARCH_STEP) instead of the _prim_* Python loop,
                            # which re-enters cek_eval per element and grows the host
                            # stack when such calls nest (e.g. a for-each tree walk).
                            # _build_hof_frame is the single source of truth, shared
                            # with _enter_proc so the same primitive reached as a
                            # callback is driven on frames too -- no _prim_* re-entry
                            # on any path.  Reached here for both direct operator
                            # position and (apply map ...), since apply collapses into
                            # this dispatch above.
                            _hof_frame = _build_hof_frame(
                                fn_value, kind, new_collected, app_node)
                            if _hof_frame is not None:
                                K.append(_hof_frame)
                                continue
                            # call-with-values: install consumer frame, tail-call producer.
                            if kind == PRIM_CALL_WITH_VALUES:
                                if len(new_collected) != 2:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('call-with-values', 2, 2,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                producer = new_collected[0]
                                consumer = new_collected[1]
                                K.append(
                                    (FRAME_CWV_CONSUMER, consumer, app_node))
                                fn_value = producer
                                new_collected = []
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # force: install result frame, tail-call the thunk (or return
                            # the cached value immediately if the promise is already done).
                            # R7RS-small 6.10 leaves force-of-non-promise implementation-
                            # defined; we return non-promises unchanged so callers can
                            # write (force x) without first checking promise?, matching
                            # SRFI 155 and most R6RS impls.
                            if kind == PRIM_FORCE:
                                if len(new_collected) != 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            'force', 1, 1, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                p = new_collected[0]
                                if not is_promise(p):
                                    V = p
                                    continue
                                if as_promise_is_done(p):
                                    V = as_promise_payload(p)
                                    continue
                                K.append((FRAME_FORCE_RESULT, p))
                                fn_value = as_promise_payload(p)
                                new_collected = []
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # make-parameter: if a converter is given, tail-call it with
                            # the init value and wrap its return as a Parameter via
                            # FRAME_MAKE_PARAMETER.  Without a converter, build the
                            # parameter inline.
                            if kind == PRIM_MAKE_PARAMETER:
                                if len(new_collected) not in (1, 2):
                                    raise SchemeArityError(
                                        arity_mismatch_msg('make-parameter', 1, 2,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                if len(new_collected) == 1:
                                    V = make_parameter(new_collected[0], None)
                                    continue
                                converter = new_collected[1]
                                init = new_collected[0]
                                if not (is_primitive(converter) or is_closure(converter)
                                        or is_case_closure(converter)):
                                    raise SchemeTypeError(
                                        'make-parameter: converter must be a procedure',
                                        app_node)
                                K.append((FRAME_MAKE_PARAMETER, converter))
                                fn_value = converter
                                new_collected = [init]
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # with-exception-handler: push handler on handler_stack,
                            # push FRAME_POP_HANDLER, tail-call thunk.  Handler is
                            # popped on normal return via FRAME_POP_HANDLER, or
                            # consumed by raise / raise-continuable.
                            if kind == PRIM_WITH_EXCEPTION_HANDLER:
                                if len(new_collected) != 2:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('with-exception-handler', 2, 2,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                handler = new_collected[0]
                                thunk = new_collected[1]
                                # Do NOT drop the enclosing handler when a nested
                                # with-exception-handler is in tail position.  The outer
                                # handler must remain on handler_stack so that exceptions
                                # raised inside the inner handler body (e.g. from a nested
                                # raise or raise-continuable) can propagate to it.
                                ctx.handler_stack.append(handler)
                                K.append((FRAME_POP_HANDLER,))
                                fn_value = thunk
                                new_collected = []
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # %guard-eval: guard's dedicated evaluator.  Uses FRAME_GUARD
                            # (not FRAME_POP_HANDLER) so the tail-call optimization only
                            # fires within guard chains, not across weh/guard boundaries.
                            # Guard handlers may return normally (no FRAME_NONCONTIN_RETURN).
                            if kind == PRIM_GUARD_EVAL:
                                if len(new_collected) != 2:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('%guard-eval', 2, 2,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                handler = new_collected[0]
                                thunk = new_collected[1]
                                # Tail-call optimization: replace a prior FRAME_GUARD only
                                # when this is the SAME guard form (tail-recursive loop).
                                # Check by comparing handler body cons cell identity:
                                # same parsed lambda means same guard form, not a new
                                # nested guard.  Different forms (nested guards) must NOT
                                # be replaced so exception propagation to outer guards works.
                                # Skip past FRAME_SHADOW_POP frames to find the real top.
                                _gi = len(K) - 1
                                while _gi >= 0 and K[_gi][0] == FRAME_SHADOW_POP:
                                    _gi -= 1
                                if _gi >= 0 and K[_gi][0] == FRAME_GUARD:
                                    _prev = ctx.handler_stack[-1] if ctx.handler_stack else None
                                    if (_prev is not None and
                                        is_closure(_prev) and is_closure(handler) and
                                            _prev[2] is handler[2]):
                                        # Pop shadow frames above FRAME_GUARD, then FRAME_GUARD itself
                                        while len(K) - 1 > _gi:
                                            K.pop()
                                            if ctx.shadow_stack:
                                                ctx.shadow_stack.pop()
                                        K.pop()
                                        ctx.handler_stack.pop()
                                ctx.handler_stack.append(handler)
                                K.append((FRAME_GUARD,))
                                fn_value = thunk
                                new_collected = []
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # raise (non-continuable): throw Python SchemeRaised so the
                            # exception unwinds the CEK loop.  cek_eval's except block
                            # routes to the topmost installed handler if any.
                            if kind == PRIM_RAISE:
                                if len(new_collected) != 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            'raise', 1, 1, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                raise SchemeRaised(new_collected[0], app_node,
                                                   continuable=False)
                            # raise-continuable: handler's return value flows back to
                            # the raise-continuable call site (R7RS-correct).  Pop the
                            # handler so a re-raise inside the handler reaches the next
                            # outer one; FRAME_REINSTALL_HANDLER puts it back on return.
                            if kind == PRIM_RAISE_CONTINUABLE:
                                if len(new_collected) != 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('raise-continuable', 1, 1,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                raised_val = new_collected[0]
                                if not ctx.handler_stack:
                                    raise SchemeRaised(
                                        raised_val, app_node, continuable=True)
                                handler = ctx.handler_stack.pop()
                                K.append((FRAME_REINSTALL_HANDLER, handler))
                                fn_value = handler
                                new_collected = [raised_val]
                                kind = (as_primitive_kind(fn_value)
                                        if is_primitive(fn_value) else PRIM_ORDINARY)
                            # eval: expand and analyze the datum once, then set C to the
                            # expanded form and continue in the same cek_eval call.  Tail
                            # calls inside the eval'd expression compose with the
                            # surrounding continuation, so deep recursion through eval
                            # doesn't add Python stack.  The optional env-spec argument
                            # selects the evaluation environment: an env value from
                            # (interaction-environment) or (environment ...).  Without
                            # it, the caller's global env is used.
                            if kind == PRIM_EVAL:
                                if len(new_collected) not in (1, 2):
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            'eval', 1, 2, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                datum = new_collected[0]
                                if len(new_collected) == 2:
                                    env_arg = new_collected[1]
                                    if not is_environment(env_arg):
                                        raise SchemeTypeError(
                                            'eval: second argument must be an environment',
                                            src_of(app_node) if app_node is not None else None)
                                    target_env = as_environment(env_arg)
                                else:
                                    target_env = saved_env.getGlobalEnv()
                                from pyscheme.Expander import expand
                                from pyscheme.Analyzer import analyze
                                from pyscheme.primitives import PRIMITIVE_ARITIES
                                expanded = expand(datum)
                                analyze(expanded, dict(PRIMITIVE_ARITIES))
                                C = expanded
                                E = target_env
                                break
                            # error: build an ErrorObject and throw Python SchemeUserError
                            # (which subclasses SchemeRaised), letting cek_eval's except
                            # path route to the handler stack.
                            if kind == PRIM_ERROR:
                                if len(new_collected) < 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            'error', 1, None, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                msg_arg = new_collected[0]
                                if not is_string(msg_arg):
                                    raise SchemeTypeError(
                                        'error: first argument must be a string', app_node)
                                msg = as_string(msg_arg)
                                irritants = []
                                i = 1
                                while i < len(new_collected):
                                    irritants.append(new_collected[i])
                                    i = i + 1
                                raise SchemeUserError(msg, irritants, app_node)
                            # %with-parameters: apply converters, save old, install new,
                            # push wind frame so we integrate with dynamic-wind / continuation
                            # re-entry, tail-call thunk.
                            if kind == PRIM_WITH_PARAMETERS:
                                if len(new_collected) != 3:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('%with-parameters', 3, 3,
                                                           len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                # TCO: if K's top is a FRAME_DYNAMIC_WIND_AFTER from a prior
                                # parameterize restore, eagerly fire it now so that K and
                                # wind_stack stay O(1) for tail-recursive parameterize loops.
                                # Mirrors the %guard-eval FRAME_GUARD replacement above.
                                # Skip past FRAME_SHADOW_POP frames to find the real top.
                                _pi = len(K) - 1
                                while _pi >= 0 and K[_pi][0] == FRAME_SHADOW_POP:
                                    _pi -= 1
                                if (_pi >= 0 and K[_pi][0] == FRAME_DYNAMIC_WIND_AFTER and
                                        is_primitive(K[_pi][1]) and
                                        as_primitive_name(K[_pi][1]) == '%parameterize-restore'):
                                    _prev_restore = K[_pi][1]
                                    # Pop shadow frames above FRAME_DYNAMIC_WIND_AFTER, then it
                                    while len(K) - 1 > _pi:
                                        K.pop()
                                        if ctx.shadow_stack:
                                            ctx.shadow_stack.pop()
                                    K.pop()
                                    if ctx.wind_stack:
                                        ctx.wind_stack.pop()
                                    as_primitive_fn(_prev_restore)(
                                        ctx, saved_env, [], app_node)
                                # Resolve params/values (pure), then drive the value
                                # converters on the K stack via FRAME_PARAMETERIZE_STEP;
                                # its final step installs the winds and tail-calls the
                                # body thunk.  No re-entrant _apply_scheme_proc.
                                _pp = _resolve_parameterize_params(
                                    new_collected[0], new_collected[1], ctx, app_node)
                                K.append((FRAME_PARAMETERIZE_STEP, _pp[0], _pp[1],
                                          [], 0, new_collected[2], app_node, False))
                                continue
                            # dynamic-wind: install the wind frame in the CEK machine
                            # so continuation captures see it and FRAME_DYNAMIC_WIND_AFTER
                            # runs the after thunk when the body returns.
                            if kind == PRIM_DYNAMIC_WIND:
                                if len(new_collected) != 3:
                                    raise SchemeArityError(
                                        arity_mismatch_msg('dynamic-wind',
                                                           3, 3, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                # Run the before-thunk on the K stack (not via a
                                # re-entrant _apply_scheme_proc); when it returns,
                                # FRAME_DYNAMIC_WIND_BEFORE_DONE installs the wind +
                                # after-frame and tail-calls the body.  If before
                                # raises, no wind is installed (the frame is discarded
                                # during unwind) -- matching the old eager call.
                                K.append((FRAME_DYNAMIC_WIND_BEFORE_DONE,
                                          new_collected[0], new_collected[1],
                                          new_collected[2]))
                                fn_value = new_collected[0]
                                new_collected = []
                            # call-with-port / call-with-{input,output}-file /
                            # with-{input,output}-{from,to}-{file,string}: open and
                            # set up as the _prim_* bodies did, then ride the
                            # dynamic-wind machinery -- a native after-thunk does the
                            # close / parameter-restore on every exit path (normal,
                            # error, escape), and the proc/thunk is tail-called on the
                            # K stack instead of re-entering cek_eval.  Reached for the
                            # operator position and (apply call-with-port ...) alike.
                            if kind == PRIM_PORT_RUNNER:
                                from pyscheme.primitives.ports import (
                                    port_runner_setup)
                                _pr = port_runner_setup(
                                    as_primitive_name(fn_value), ctx, saved_env,
                                    new_collected, app_node)
                                ctx.wind_stack.append((_pr[0], _pr[1]))
                                K.append((FRAME_DYNAMIC_WIND_AFTER, _pr[1]))
                                fn_value = _pr[2]
                                new_collected = _pr[3]
                            # load: read + parse the file (native), then evaluate its
                            # top-level forms on the K stack via FRAME_EVAL_FORMS
                            # instead of a re-entrant cek_eval per form.  Reached for
                            # the operator position and (apply load ...) alike.
                            if kind == PRIM_LOAD:
                                from pyscheme.primitives.meta import load_setup
                                _lf = load_setup(new_collected, saved_env, app_node)
                                K.append((FRAME_EVAL_FORMS, _lf[0], _lf[1],
                                          0, {}, True))
                                continue
                            pv = _apply_parameter_if(
                                fn_value, len(new_collected), app_node)
                            if pv is not None:
                                V = pv
                                continue
                            # Record accessor: type-check the arg using the call-site
                            # app_node so the error position points at the user's call,
                            # not the define-record-type form.
                            if is_record_accessor(fn_value):
                                if len(new_collected) != 1:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            as_record_accessor_name(fn_value),
                                            1, 1, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                rt = as_record_accessor_type(fn_value)
                                rec = new_collected[0]
                                if not is_record(rec) or as_record_type(rec) is not rt:
                                    raise SchemeTypeError(
                                        as_record_accessor_name(fn_value)
                                        + ': argument is not a ' +
                                        as_record_type_name(rt),
                                        src_of(app_node) if app_node is not None else None)
                                V = as_record_fields(
                                    rec)[as_record_accessor_index(fn_value)]
                                continue
                            # Record mutator: same pattern; V is VOID after assignment.
                            if is_record_mutator(fn_value):
                                if len(new_collected) != 2:
                                    raise SchemeArityError(
                                        arity_mismatch_msg(
                                            as_record_mutator_name(fn_value),
                                            2, 2, len(new_collected)),
                                        src_of(app_node) if app_node is not None else None)
                                rt = as_record_mutator_type(fn_value)
                                rec = new_collected[0]
                                if not is_record(rec) or as_record_type(rec) is not rt:
                                    raise SchemeTypeError(
                                        as_record_mutator_name(fn_value)
                                        + ': first argument is not a ' +
                                        as_record_type_name(rt),
                                        src_of(app_node) if app_node is not None else None)
                                as_record_fields(rec)[as_record_mutator_index(
                                    fn_value)] = new_collected[1]
                                V = VOID_VALUE
                                continue
                            _trc_printed = False
                            _trc_name = None
                            _trc_depth = 0
                            if ctx._instrumented:
                                _trc = ctx.tracer
                                if _trc._active:
                                    if is_symbol(app_node.car):
                                        _trc_name = as_symbol(app_node.car)
                                    if _trc_name is None and is_primitive(fn_value):
                                        _trc_name = as_primitive_name(fn_value)
                                    if _trc_name is not None and _trc_name in _trc._fns_to_trace:
                                        _trc_depth = _trc._depth
                                        _trc_printed = _trc.trace_enter(
                                            _trc_name, new_collected, _trc_depth, ctx.outStrm)
                                        if _trc_printed:
                                            _trc._depth = _trc_depth + 1
                            if is_primitive(fn_value):
                                V = as_primitive_fn(fn_value)(
                                    ctx, saved_env, new_collected, app_node)
                                if _trc_printed:
                                    ctx.tracer._depth = _trc_depth
                                    ctx.tracer.trace_exit(
                                        _trc_name, V, _trc_depth, ctx.outStrm)
                                continue
                            r = _apply_value(fn_value, new_collected, app_node)
                            if fn_value is original_fn:
                                _shadow_push(ctx, K, app_node)
                            if _trc_printed:
                                K.append(
                                    (FRAME_TRACE_EXIT, _trc_name, _trc_depth))
                            E = r.new_env
                            C = r.body.car
                            if is_cons(r.body.cdr):
                                K.append((FRAME_SEQ, r.body.cdr, r.new_env))
                            break
                        new_remaining = []
                        i = 1
                        while i < len(remaining):
                            new_remaining.append(remaining[i])
                            i = i + 1
                        K.append((FRAME_CALL, fn_value, new_collected,
                                  new_remaining, saved_env, app_node))
                        C = remaining[0]
                        E = saved_env
                        break

                    if ftag == FRAME_SEQ:
                        # frame = (FRAME_SEQ, remaining_body_cons, env)
                        remaining = frame[1]
                        saved_env = frame[2]
                        E = saved_env
                        C = remaining.car
                        if is_cons(remaining.cdr):
                            K.append((FRAME_SEQ, remaining.cdr, saved_env))
                        break

                    if ftag == FRAME_WHEN:
                        body = frame[1]
                        saved_env = frame[2]
                        if isFalse(V):
                            V = VOID_VALUE
                            continue
                        E = saved_env
                        C = body.car
                        if is_cons(body.cdr):
                            K.append((FRAME_SEQ, body.cdr, saved_env))
                        break

                    if ftag == FRAME_UNLESS:
                        body = frame[1]
                        saved_env = frame[2]
                        if isFalse(V):
                            E = saved_env
                            C = body.car
                            if is_cons(body.cdr):
                                K.append((FRAME_SEQ, body.cdr, saved_env))
                            break
                        V = VOID_VALUE
                        continue

                    if ftag == FRAME_AND:
                        remaining = frame[1]
                        saved_env = frame[2]
                        if isFalse(V):
                            continue   # short-circuit: V stays #f
                        if is_nil(remaining):
                            continue   # V is the last truthy
                        if is_cons(remaining.cdr):
                            K.append((FRAME_AND, remaining.cdr, saved_env))
                        C = remaining.car
                        E = saved_env
                        break

                    if ftag == FRAME_OR:
                        remaining = frame[1]
                        saved_env = frame[2]
                        if not isFalse(V):
                            continue   # short-circuit: V stays truthy
                        if is_nil(remaining):
                            continue   # V stays as last false
                        if is_cons(remaining.cdr):
                            K.append((FRAME_OR, remaining.cdr, saved_env))
                        C = remaining.car
                        E = saved_env
                        break

                    if ftag == FRAME_COND:
                        current = frame[1]
                        remaining = frame[2]
                        saved_env = frame[3]
                        if isFalse(V):
                            # Test failed - advance to next clause.
                            if is_nil(remaining):
                                V = VOID_VALUE
                                continue
                            nxt = remaining.car
                            rest = remaining.cdr
                            kind = _classify_cond_clause(nxt, saved_env)
                            E = saved_env
                            if kind[0] == 'else':
                                body = kind[1]
                                C = body.car
                                if is_cons(body.cdr):
                                    K.append((FRAME_SEQ, body.cdr, saved_env))
                                break
                            K.append((FRAME_COND, nxt, rest, saved_env))
                            C = kind[1]
                            break
                        # Test truthy - dispatch on clause kind.
                        kind = _classify_cond_clause(current, saved_env)
                        if kind[0] == 'test-only':
                            continue   # V stays as the test value
                        if kind[0] == 'body':
                            body = kind[2]
                            E = saved_env
                            C = body.car
                            if is_cons(body.cdr):
                                K.append((FRAME_SEQ, body.cdr, saved_env))
                            break
                        # 'arrow' - apply proc to test value
                        proc_expr = kind[2]
                        K.append((FRAME_COND_ARROW, V, saved_env))
                        C = proc_expr
                        E = saved_env
                        break

                    if ftag == FRAME_COND_ARROW or ftag == FRAME_CASE_ARROW:
                        # (cond (test => recv)) / (case key (... => recv)): apply
                        # the receiver to the single value (test / key, held in
                        # frame[1]) through the one unified application path
                        # (_enter_proc), so primitives, continuations, parameters,
                        # record accessors/mutators, and frame-driven HOFs behave
                        # exactly as they do in operator position -- rather than
                        # the old inline ladder that mishandled the last two.
                        arg_value = frame[1]
                        saved_env = frame[2]
                        sig = _apply_enter_result(
                            _enter_proc(V, [arg_value], ctx, saved_env, None), K, V)
                        if sig[0] == 'apply':
                            V = sig[1]
                            continue
                        C = sig[1]
                        E = sig[2]
                        break

                    if ftag == FRAME_CASE:
                        # V is the (possibly-matched) key value on first entry, or the
                        # outcome of the prior clause's no-match check on subsequent
                        # entries (ignored - we always look at the key, held in frame).
                        current_clause = frame[1]
                        remaining = frame[2]
                        saved_env = frame[3]
                        # First entry: V holds the key value.  We stash it back on any
                        # retry by re-pushing FRAME_CASE frames that carry the key
                        # alongside the remaining clauses.  Walk the current clause.
                        head = current_clause.car
                        if _is_aux_keyword(head, 'else', saved_env):
                            body = current_clause.cdr
                            E = saved_env
                            if is_cons(body) and is_symbol(body.car) and as_symbol(body.car) == '=>':
                                K.append((FRAME_CASE_ARROW, V, saved_env))
                                C = body.cdr.car
                            else:
                                C = body.car
                                if is_cons(body.cdr):
                                    K.append((FRAME_SEQ, body.cdr, saved_env))
                            break
                        # Datum-list match: head is the list of literal datums.
                        matched = False
                        cur = head
                        while is_cons(cur):
                            if eqv_atom(V, cur.car):
                                matched = True
                                break
                            cur = cur.cdr
                        if matched:
                            body = current_clause.cdr
                            E = saved_env
                            if is_cons(body) and is_symbol(body.car) and as_symbol(body.car) == '=>':
                                K.append((FRAME_CASE_ARROW, V, saved_env))
                                C = body.cdr.car
                            else:
                                C = body.car
                                if is_cons(body.cdr):
                                    K.append((FRAME_SEQ, body.cdr, saved_env))
                            break
                        # No match; advance to the next clause (V stays as the key).
                        if is_nil(remaining):
                            V = VOID_VALUE
                            continue
                        nxt = remaining.car
                        rst = remaining.cdr
                        K.append((FRAME_CASE, nxt, rst, saved_env))
                        continue

                    if ftag == FRAME_LET:
                        names = frame[1]
                        collected = frame[2]
                        remaining = frame[3]
                        body = frame[4]
                        saved_env = frame[5]
                        new_collected = list(collected)
                        new_collected.append(V)
                        if len(remaining) == 0:
                            new_env = Environment(saved_env)
                            i = 0
                            while i < len(names):
                                new_env.bind(names[i], new_collected[i])
                                i = i + 1
                            E = new_env
                            C = body.car
                            if is_cons(body.cdr):
                                K.append((FRAME_SEQ, body.cdr, new_env))
                            break
                        new_remaining = []
                        i = 1
                        while i < len(remaining):
                            new_remaining.append(remaining[i])
                            i = i + 1
                        K.append((FRAME_LET, names, new_collected,
                                  new_remaining, body, saved_env))
                        C = remaining[0]
                        E = saved_env
                        break

                    if ftag == FRAME_LET_STAR:
                        name = frame[1]
                        remaining = frame[2]
                        body = frame[3]
                        saved_env = frame[4]
                        new_env = Environment(saved_env)
                        new_env.bind(name, V)
                        if len(remaining) == 0:
                            E = new_env
                            C = body.car
                            if is_cons(body.cdr):
                                K.append((FRAME_SEQ, body.cdr, new_env))
                            break
                        new_remaining = []
                        i = 1
                        while i < len(remaining):
                            new_remaining.append(remaining[i])
                            i = i + 1
                        next_pair = remaining[0]
                        K.append((FRAME_LET_STAR, next_pair[0], new_remaining,
                                  body, new_env))
                        C = next_pair[1]
                        E = new_env
                        break

                    if ftag == FRAME_LETREC:
                        name = frame[1]
                        remaining = frame[2]
                        body = frame[3]
                        saved_env = frame[4]
                        saved_env.set(name, V)
                        if len(remaining) == 0:
                            E = saved_env
                            C = body.car
                            if is_cons(body.cdr):
                                K.append((FRAME_SEQ, body.cdr, saved_env))
                            break
                        new_remaining = []
                        i = 1
                        while i < len(remaining):
                            new_remaining.append(remaining[i])
                            i = i + 1
                        next_pair = remaining[0]
                        K.append((FRAME_LETREC, next_pair[0], new_remaining,
                                  body, saved_env))
                        C = next_pair[1]
                        E = saved_env
                        break

                    if ftag == FRAME_SHADOW_POP:
                        if ctx.shadow_stack:
                            ctx.shadow_stack.pop()
                        continue

                    if ftag == FRAME_TRACE_EXIT:
                        fn_name = frame[1]
                        depth = frame[2]
                        tracer = ctx.tracer
                        tracer._depth = depth
                        tracer.trace_exit(fn_name, V, depth, ctx.outStrm)
                        continue

                    raise RuntimeError("unknown frame tag: " + str(ftag))

                # fall through to outer `while True` - restart EVAL

        except _CATCHABLE_LOCAL as e:
            # Walk K once to find a handler frame, COLLECTING (not running) the
            # FRAME_DYNAMIC_WIND_AFTER thunks for the extents between the raise and
            # the handler.  A single scan keeps the reinstall accounting correct;
            # the collected afters are then run on the K stack by FRAME_ERROR_UNWIND
            # (so an after-thunk's continuation/HOF no longer re-enters cek_eval),
            # which performs the terminal action -- dispatch handler, or re-raise if
            # none.  Propagate semantics: an after that raises becomes the new
            # in-flight condition (matches Chez; R7RS-unspecified).  Handler dispatch
            # rewrites C/E/K/V in place; a continuation captured inside the handler
            # body sees the outer K-stack as its captured K.
            from pyscheme.AST import make_error_object
            handler = None
            is_guard_handler = False
            unwind_afters = []
            # A raise-continuable pops its handler off handler_stack but leaves the
            # handler's FRAME_POP_HANDLER / FRAME_GUARD on K, with a
            # FRAME_REINSTALL_HANDLER above it.  When the handler then raises and we
            # unwind, those orphaned frames must be skipped WITHOUT popping
            # handler_stack, or K frames and handler_stack drift out of alignment and
            # we pair a frame with the wrong handler (e.g. treat a guard handler as a
            # plain one and spuriously raise "exception handler returned").  Count
            # the FRAME_REINSTALL_HANDLER frames and skip that many handler frames.
            pending_reinstalls = 0
            while K:
                frame = K.pop()
                ftag = frame[0]
                if ftag == FRAME_REINSTALL_HANDLER:
                    pending_reinstalls += 1
                    continue
                if ftag == FRAME_POP_HANDLER:
                    if pending_reinstalls > 0:
                        pending_reinstalls -= 1
                        continue
                    if not ctx.handler_stack:
                        break
                    if unwind_afters:
                        # Leave the handler installed: the collected afters run within
                        # its dynamic extent, so an after that raises must reach it.
                        # FRAME_ERROR_UNWIND re-raises once the afters are done, and
                        # this frame -- now atop K -- is dispatched with no afters.
                        K.append(frame)
                        break
                    handler = ctx.handler_stack.pop()
                    break
                if ftag == FRAME_GUARD:
                    if pending_reinstalls > 0:
                        pending_reinstalls -= 1
                        continue
                    if not ctx.handler_stack:
                        break
                    if unwind_afters:
                        K.append(frame)
                        break
                    handler = ctx.handler_stack.pop()
                    is_guard_handler = True
                    break
                if ftag == FRAME_DYNAMIC_WIND_AFTER:
                    if ctx.wind_stack:
                        ctx.wind_stack.pop()
                    unwind_afters.append(frame[1])
            if unwind_afters:
                # Run the collected afters on the K stack, then re-raise (the handler
                # frame, if any, was left installed above and is dispatched then).
                # skip_eval: resume in the APPLY phase to process FRAME_ERROR_UNWIND;
                # WITHOUT this the loop would re-EVAL the unchanged C (e.g. re-run a
                # define-library whose FRAME_LIB_FINALIZE raised) -- an infinite loop.
                K.append((FRAME_ERROR_UNWIND, unwind_afters, 0, e))
                skip_eval = True
                continue
            if handler is None:
                raise
            if isinstance(e, SchemeRaised):
                raised_value = e.value
            elif isinstance(e, SchemeSyntaxError):
                raised_value = make_read_error_object(e.msg, [])
            else:
                raised_value = make_error_object(e.msg, [])
            # No winds to run: dispatch the handler inline.
            if isinstance(e, SchemeRaised) and not e.continuable and not is_guard_handler:
                K.append((FRAME_NONCONTIN_RETURN, raised_value))
            result = _enter_proc(handler, [raised_value], ctx, E, None)
            sig = _apply_enter_result(result, K, V)
            if sig[0] == 'apply':
                # Handler produced a value, or is itself a frame-driven HOF
                # primitive whose driver was just pushed; resume the APPLY loop
                # (the start-frame ignores the current V).
                V = sig[1]
                skip_eval = True
            else:  # 'eval': handler is a closure body to evaluate
                C = sig[1]
                E = sig[2]
            continue


# -------- Self-test --------

if __name__ == '__main__':
    # When run as `python -m pyscheme.Evaluator` this module loads as __main__.
    # The primitives package below imports cek_eval and _apply_value via
    # `from pyscheme.Evaluator import ...`, which would otherwise load a
    # second copy of this module under the name pyscheme.Evaluator and
    # produce duplicate value tags / class instances.  Alias __main__ in
    # sys.modules so the absolute import resolves to this same module.
    import sys
    sys.modules['pyscheme.Evaluator'] = sys.modules[__name__]

    from pyscheme.Parser import parse, parse_one
    from pyscheme.Expander import expand
    from pyscheme.Analyzer import analyze, extend_static_env_with_define
    from pyscheme.primitives import install_primitives, PRIMITIVE_ARITIES

    def _to_text(v):
        """Brief textual form of a value, for test comparison.  Avoids
        depending on PrettyPrinter (which may not be ported yet)."""
        if isinstance(v, ConsCell):
            items = []
            cur = v
            while isinstance(cur, ConsCell):
                items.append(_to_text(cur.car))
                cur = cur.cdr
            if is_nil(cur):
                return '(' + ' '.join(items) + ')'
            return '(' + ' '.join(items) + ' . ' + _to_text(cur) + ')'
        if is_string(v):
            return '"' + as_string(v) + '"'
        if is_nil(v):
            return '()'
        if is_void(v):
            return '#<void>'
        if is_integer(v):
            return str(as_integer(v))
        if is_real(v):
            return repr(as_real(v))
        if is_boolean(v):
            return '#t' if as_boolean(v) else '#f'
        if is_character(v):
            return '#\\' + as_character(v)
        if is_symbol(v):
            return as_symbol(v)
        if is_closure(v):
            return '#<closure>'
        if is_primitive(v):
            return '#<primitive ' + as_primitive_name(v) + '>'
        return repr(v)

    def _eval_source(source, env, static_env, ctx):
        forms = parse(source)
        last = None
        for form in forms:
            expanded = expand(form)
            analyze(expanded, static_env)
            extend_static_env_with_define(static_env, expanded)
            last = cek_eval(expanded, env, ctx)
        return last

    from pyscheme.Context import Context

    n_pass = 0
    n_fail = 0

    ok_cases = [
        # literals
        ('42',                                  '42'),
        ('#t',                                  '#t'),
        ('#f',                                  '#f'),
        ('"hello"',                             '"hello"'),
        # define + lookup
        ('(define x 5) x',                      '5'),
        ('(define x 5) (set! x 99) x',          '99'),
        # immediate lambda
        ('((lambda (x) x) 7)',                  '7'),
        # if
        ('(if #t 1 2)',                         '1'),
        ('(if #f 1 2)',                         '2'),
        ('(if #t 1)',                           '1'),
        ('(if #f 1)',                           '#<void>'),
        # define a function shorthand
        ('(define (id x) x) (id 42)',           '42'),
        ('(define id (lambda (x) x)) (id 99)',  '99'),
        # multi-arg lambda
        ('((lambda (x y) x) 11 22)',            '11'),
        ('((lambda (a b c) b) 10 20 30)',       '20'),
        ('((lambda () 42))',                    '42'),
        # multi-expression body (implicit begin)
        ('((lambda (x) (set! x 1) (set! x 2) x) 0)', '2'),
        # quoted data
        ("'x",                                  'x'),
        ("'()",                                 '()'),
        ("'(1 2 3)",                            '(1 2 3)'),
        # list primitives
        ('(car (list 1 2 3))',                  '1'),
        ('(cdr (list 1 2 3))',                  '(2 3)'),
        ('(cons 1 (cons 2 (cons 3 (list))))',   '(1 2 3)'),
        ('(cons 1 2)',                          '(1 . 2)'),
        ('(pair? (list 1 2))',                  '#t'),
        ('(pair? (list))',                      '#f'),
        ('(null? (list))',                      '#t'),
        ('(null? (list 1))',                    '#f'),
        # begin
        ('(begin 1 2 3)',                       '3'),
        # when / unless
        ('(when #t 1 2 3)',                     '3'),
        ('(when #f 1)',                         '#<void>'),
        ('(unless #f 1 2 3)',                   '3'),
        ('(unless #t 1)',                       '#<void>'),
        # and / or
        ('(and)',                               '#t'),
        ('(and 1 2 3)',                         '3'),
        ('(and 1 #f 3)',                        '#f'),
        ('(or)',                                '#f'),
        ('(or #f #f 3)',                        '3'),
        ('(or 1 (cons 1 2))',                   '1'),
        # cond
        ('(cond (#t 1))',                       '1'),
        ('(cond (#f 1) (else 2))',              '2'),
        ('(cond (7))',                          '7'),
        ('(cond ((cons 1 2) => car))',          '1'),
        ('(cond (#f => car) (else 42))',        '42'),
        # let / let* / letrec
        ('(let () 42)',                         '42'),
        ('(let ((x 1)) x)',                     '1'),
        ('(let ((x 1) (y 2)) y)',               '2'),
        ('(let ((a 1) (b 2) (c 3)) (list a b c))', '(1 2 3)'),
        ('(let* ((x 1) (y x)) y)',              '1'),
        ('(letrec ((f (lambda (x) x))) (f 1))', '1'),
        ('(letrec* ((f (lambda (x) x))) (f 1))', '1'),
        # named let
        ('(let loop ((x 1)) x)',                '1'),
        ('(let loop ((n 5) (acc 1)) (if (= n 0) acc (loop (- n 1) (* acc n))))', '120'),
        # variadic lambda
        ('((lambda args args) 1 2 3)',          '(1 2 3)'),
        ('((lambda (x . rest) rest) 1 2 3)',    '(2 3)'),
        ('((lambda (x . rest) rest) 1)',        '()'),
        # arithmetic
        ('(+ 1 2 3)',                           '6'),
        ('(* 2 3 4)',                           '24'),
        ('(- 10 3)',                            '7'),
        # recursion
        ('(define (fact n) (if (= n 0) 1 (* n (fact (- n 1))))) (fact 5)', '120'),
    ]

    class TestState:
        def __init__(self, env, static_env, ctx):
            self.env = env
            self.static_env = static_env
            self.ctx = ctx

    def fresh_state():
        from pyscheme.library import register_standard_libraries
        env = Environment()
        install_primitives(env)
        register_standard_libraries(env)
        static_env = dict(PRIMITIVE_ARITIES)
        ctx = Context()
        return TestState(env, static_env, ctx)

    print('-- evaluator: full pipeline --')
    i = 0
    while i < len(ok_cases):
        source = ok_cases[i][0]
        expected = ok_cases[i][1]
        st = fresh_state()
        try:
            got_val = _eval_source(source, st.env, st.static_env, st.ctx)
        except Exception as e:
            print("[FAIL] %r: %s: %s" % (source, type(e).__name__, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
        got = _to_text(got_val) if got_val is not None else ''
        if got == expected:
            print("[ OK ] %r -> %s" % (source, got))
            n_pass = n_pass + 1
        else:
            print("[FAIL] %r" % source)
            print("        expected: %s" % expected)
            print("        got:      %s" % got)
            n_fail = n_fail + 1
        i = i + 1

    # Runtime errors
    print()
    print('-- evaluator: runtime errors --')
    err_cases = [
        ('foo',                              SchemeUnboundError, 'unbound variable'),
        ('(set! foo 1)',                     SchemeUnboundError, 'set! on unbound'),
        # primitive type error
        ('(car 5)',                          SchemeTypeError,    None),
        ('((lambda (x) x))',                 SchemeArityError,   '0 arguments provided'),
    ]
    i = 0
    while i < len(err_cases):
        source = err_cases[i][0]
        expected_cls = err_cases[i][1]
        expected_sub = err_cases[i][2]
        st = fresh_state()
        try:
            _eval_source(source, st.env, st.static_env, st.ctx)
        except expected_cls as e:
            if expected_sub is None or expected_sub in str(e):
                print("[ OK ] %r -> %s: %s" % (source, type(e).__name__, e))
                n_pass = n_pass + 1
            else:
                print("[WARN] %r -> %s: %s" % (source, type(e).__name__, e))
                print("        expected substring: %r" % expected_sub)
                n_pass = n_pass + 1
            i = i + 1
            continue
        except Exception as e:
            print("[FAIL] %r: wrong exception %s: %s" %
                  (source, type(e).__name__, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
        print("[FAIL] %r: expected %s" % (source, expected_cls.__name__))
        n_fail = n_fail + 1
        i = i + 1

    # -------- .sld auto-discovery (cat 6 item 4) --------
    # Set SCHEME_LIBRARY_PATH temporarily so an unregistered library is
    # found via testing/sld-test/lib4.sld.  Verifies that import auto-
    # loads the .sld file and the imported procedure works.
    import os
    _saved_lp = os.environ.get('SCHEME_LIBRARY_PATH')
    os.environ['SCHEME_LIBRARY_PATH'] = 'testing/sld-test'
    try:
        ts = fresh_state()
        result = _eval_source(
            '(import (lib4)) (greet)', ts.env, ts.static_env, ts.ctx)
        if _to_text(result) == 'auto-discovered':
            print("[ OK ] auto-discover lib4.sld via SCHEME_LIBRARY_PATH")
            n_pass = n_pass + 1
        else:
            print("[FAIL] auto-discover lib4.sld: got %r, expected 'auto-discovered'"
                  % _to_text(result))
            n_fail = n_fail + 1
    except Exception as e:
        print("[FAIL] auto-discover lib4.sld: %s: %s" % (type(e).__name__, e))
        n_fail = n_fail + 1
    finally:
        if _saved_lp is None:
            del os.environ['SCHEME_LIBRARY_PATH']
        else:
            os.environ['SCHEME_LIBRARY_PATH'] = _saved_lp

    print()
    print("%d passed, %d failed" % (n_pass, n_fail))
