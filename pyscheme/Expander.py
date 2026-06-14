"""S-expression expander: rewrites sugar (and, eventually, user macros).

Pipeline position:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> Interpreter.simplify -> cek_eval

The Expander operates entirely at the S-expression level: it takes a
tree of cons cells and tagged-tuple atoms and returns a tree in the
same shape with sugar rewritten.  No tagged EXPR_* nodes are involved.

Sugar currently rewritten:

    (define (name args...) body...)
        ==>  (define name (lambda (args...) body...))

    (if t e)
        ==>  (if t e #<void>)
        where #<void> is represented as the literal tuple (VOID,).

The Expander dispatches on head-symbol string (e.g., "define"), so
adding a new sugar form is one entry in _SUGAR_HANDLERS plus the
rewrite function.  Quoted data is preserved as-is (its contents are
literal, not code).

Source-position propagation: every synthesized cons cell's src is the
src of the corresponding input cons cell.  The outer rewritten cons
inherits the outer input cons's src; the synthesized (lambda ...)
cell inherits the signature's src.

Public API:
    expand(sexpr)  ->  S-expression with all sugar (and user macros)
                       rewritten to core forms.
"""
import os

from pyscheme.AST import (
    alloc_cons, NIL_VALUE, VOID_VALUE, list_from_items,
    is_cons, is_nil, is_symbol, is_string, is_syntax_transformer,
    is_vector, as_vector_items, make_vector,
    as_symbol, as_string, src_of,
    make_symbol, ConsCell, REPL_FILENAME,
    intern_symbol, symbol_name,
    SYMBOL, VOID,
)
from pyscheme.syntax_rules import (
    hygiene_gensym, formals_bound_names, let_binding_names,
)


# Runtime environment reference - the Interpreter installs this before
# expansion so define-syntax can bind transformers into the runtime env
# (matching the C++ reference's design: macros live in the same env as
# values, distinguished by their SyntaxTransformer tag).  let-syntax /
# letrec-syntax temporarily swap this to a child env for their body.
# None when no interpreter has wired us up yet.
_runtime_env_ref = [None]


def set_runtime_env(env):
    """Install a reference to the current runtime env; used by define-syntax
    to install transformers and by hygiene to resolve free identifiers."""
    _runtime_env_ref[0] = env


def get_runtime_env():
    return _runtime_env_ref[0]


# Guard against runaway macro expansion (e.g., `(define-syntax f (syntax-rules
# () ((_) (f))))`).
_MAX_EXPAND_ITER = 200

# Guard against mutually-recursive macro expansion that recurses across expand()
# call boundaries (e.g., letrec-syntax with my-even?/my-odd? on a runtime
# argument).  _expand_list calls expand() recursively for each sub-form, so
# depth grows with the form tree plus any macro recursion.  500 is generous for
# normal code but catches runaway cross-call recursion before Python's own limit.
_MAX_EXPAND_DEPTH = 500
_expand_depth = [0]


def _lookup_macro(sym):
    """Return the SyntaxTransformer bound to sym, or None."""
    env = _runtime_env_ref[0]
    if env is None:
        return None
    val = env.lookup_optional(as_symbol(sym))
    if val is not None and is_syntax_transformer(val):
        return val
    return None


def _current_macro_env():
    """Snapshot the current runtime env as a flat name->value dict."""
    merged = {}
    env = _runtime_env_ref[0]
    chain = []
    while env is not None:
        chain.append(env)
        env = env._parent
    i = len(chain) - 1
    while i >= 0:
        for k, v in chain[i]._bindings.items():
            merged[symbol_name(k)] = v
        i = i - 1
    return merged


def expand(sexpr):
    """Expand sugar and macros.  Returns an S-expression."""
    from pyscheme.Parser import SchemeSyntaxError
    _expand_depth[0] = _expand_depth[0] + 1
    if _expand_depth[0] > _MAX_EXPAND_DEPTH:
        raise SchemeSyntaxError(
            'macro expansion depth exceeded - possible mutually-recursive macro loop',
            src_of(sexpr))
    try:
        return _expand_inner(sexpr)
    finally:
        _expand_depth[0] = _expand_depth[0] - 1


def _expand_inner(sexpr):
    """Inner expand loop; called only from expand()."""
    iterations = 0
    while True:
        iterations = iterations + 1
        if iterations > _MAX_EXPAND_ITER:
            from pyscheme.Parser import SchemeSyntaxError
            raise SchemeSyntaxError(
                'macro expansion limit exceeded - possible infinite loop',
                src_of(sexpr))
        if not is_cons(sexpr):
            return sexpr
        head = sexpr.car
        if is_symbol(head):
            name = as_symbol(head)
            # 1. Syntax forms that install or extend the macro env.
            if name == 'define-syntax':
                return _expand_define_syntax(sexpr)
            if name == 'let-syntax':
                return _expand_let_syntax(sexpr, False)
            if name == 'letrec-syntax':
                return _expand_let_syntax(sexpr, True)
            # 2. User macro expansion: if name is bound to a transformer,
            #    expand once then loop.
            tr = _lookup_macro(head)
            if tr is not None:
                from pyscheme.syntax_rules import apply_syntax_transformer
                sexpr = apply_syntax_transformer(tr, sexpr)
                continue
            # 3. Sugar desugaring.
            handler = _SUGAR_HANDLERS.get(name)
            if handler is not None:
                return handler(sexpr)
        # A transformer value in operator position (substituted in by another
        # macro's hygiene) also triggers expansion.
        if is_syntax_transformer(head):
            from pyscheme.syntax_rules import apply_syntax_transformer
            sexpr = apply_syntax_transformer(head, sexpr)
            continue
        return _expand_list(sexpr)


def _expand_define_syntax(sexpr):
    # (define-syntax <name> (syntax-rules ...))
    # Install the transformer in the current runtime env so the same lookup
    # path that finds runtime values also finds this macro.  Emit a sentinel
    # that evaluates to #<void> so the pipeline after us stays simple.
    from pyscheme.Parser import SchemeSyntaxError
    from pyscheme.syntax_rules import parse_syntax_rules
    if not is_cons(sexpr.cdr) or not is_symbol(sexpr.cdr.car):
        raise SchemeSyntaxError(
            'define-syntax: expected (define-syntax <name> <transformer>)',
            src_of(sexpr))
    macro_sym = sexpr.cdr.car
    macro_name = as_symbol(macro_sym)
    if not is_cons(sexpr.cdr.cdr):
        raise SchemeSyntaxError(
            'define-syntax: missing transformer', src_of(sexpr))
    tr_expr = sexpr.cdr.cdr.car
    if (not is_cons(tr_expr) or not is_symbol(tr_expr.car)
            or as_symbol(tr_expr.car) != 'syntax-rules'):
        raise SchemeSyntaxError(
            'define-syntax: transformer must be (syntax-rules ...)',
            src_of(tr_expr))
    t = parse_syntax_rules(tr_expr.cdr, _current_macro_env(), macro_name,
                           src_of(tr_expr))
    env = _runtime_env_ref[0]
    if env is not None:
        env.bind(macro_name, t)
    return VOID_VALUE


def _expand_let_syntax(sexpr, is_letrec):
    # (let-syntax  ((name transformer)...) body...)
    # (letrec-syntax ((name transformer)...) body...)
    # Build a fresh child env, bind each transformer, expand body in child env.
    # let-syntax: transformers are parsed with outer env (siblings invisible).
    # letrec-syntax: switch to child env before parsing so siblings are visible.
    from pyscheme.Parser import SchemeSyntaxError
    from pyscheme.syntax_rules import parse_syntax_rules
    from pyscheme.Environment import Environment
    if not is_cons(sexpr.cdr):
        raise SchemeSyntaxError('let-syntax: malformed', src_of(sexpr))
    bindings = sexpr.cdr.car
    body = sexpr.cdr.cdr
    if not is_cons(body):
        raise SchemeSyntaxError('let-syntax: empty body', src_of(sexpr))
    outer_env = _runtime_env_ref[0]
    if outer_env is None:
        child_env = Environment()
    else:
        child_env = Environment(parent=outer_env)
    src = sexpr.src
    if is_letrec:
        _runtime_env_ref[0] = child_env
    try:
        transformers = []
        let_rename_table = {}
        cur = bindings
        while is_cons(cur):
            b = cur.car
            if (not is_cons(b) or not is_symbol(b.car) or not is_cons(b.cdr)):
                raise SchemeSyntaxError(
                    'let-syntax: malformed binding', src_of(b))
            bname = as_symbol(b.car)
            tr_expr = b.cdr.car
            if (not is_cons(tr_expr) or not is_symbol(tr_expr.car)
                    or as_symbol(tr_expr.car) != 'syntax-rules'):
                raise SchemeSyntaxError(
                    'let-syntax: transformer must be (syntax-rules ...)',
                    src_of(tr_expr))
            t = parse_syntax_rules(tr_expr.cdr, _current_macro_env(), bname,
                                   src_of(tr_expr))
            if is_letrec:
                child_env.bind(bname, t)
            else:
                # let-syntax: bind under a gensym so sibling templates can't see
                # each other; rename body references via let_rename_table.
                gs = hygiene_gensym(bname)
                child_env.bind(gs, t)
                let_rename_table[bname] = gs
            transformers.append(t)
            cur = cur.cdr
        # For letrec-syntax, fix up self- and mutual-references: any free_id_map
        # alias that maps a name now bound in child_env should point to the new
        # child_env value, not the outer env's shadowed binding.
        if is_letrec and transformers:
            global_env = child_env.getGlobalEnv()
            for t_obj in transformers:
                for fid, gs in t_obj.free_id_map.items():
                    fid_sid = intern_symbol(fid)
                    if fid_sid in child_env._bindings:
                        global_env.bind(gs, child_env._bindings[fid_sid])
        _runtime_env_ref[0] = child_env
        # Wrap the body in (let () body...) -- NOT (begin body...).  A begin in a
        # definition context splices its internal defines into the *enclosing*
        # body; R7RS 4.2.6 makes the let-syntax/letrec-syntax body its own region
        # ("definitions spliced as in let/letrec bodies"), so an internal define
        # must stay local.  (let () ...) reuses the body-scanner's letrec* lowering
        # to give exactly that new scope.  Confirmed against chibi:
        #   (let () (define x 1) (let-syntax () (define x 2) #f) x) => 1
        body_items = [make_symbol('let', src), NIL_VALUE]
        bcur = body
        while is_cons(bcur):
            body_items.append(bcur.car)
            bcur = bcur.cdr
        wrapped = list_from_items(body_items, src)
        if let_rename_table:
            wrapped = _rename_refs_in_form(wrapped, let_rename_table)
        return expand(wrapped)
    finally:
        _runtime_env_ref[0] = outer_env


def _expand_list(cell):
    """Walk a cons chain, recursively expanding each car.  Handles
    proper and improper lists.  Every new cons cell shares the input
    cell's src (they are the same list, just with expanded contents)."""
    items = []
    cur = cell
    while is_cons(cur):
        items.append(expand(cur.car))
        cur = cur.cdr
    src = cell.src
    if is_nil(cur):
        tail = NIL_VALUE
    else:
        tail = expand(cur)
    result = tail
    i = len(items) - 1
    while i >= 0:
        result = alloc_cons(items[i], result, src)
        i = i - 1
    return result


# ---- helpers for list walking ----------------------------------------

def _list_length(cell):
    """Count cons cells in a proper list.  Returns -1 if improper."""
    n = 0
    cur = cell
    while is_cons(cur):
        n = n + 1
        cur = cur.cdr
    if is_nil(cur):
        return n
    return -1


# ---- body expansion (R7RS 5.3.2 internal definitions) ----
# A body in lambda / let / let* / letrec / letrec* / when / unless /
# case-lambda / let-values / let*-values / parameterize / guard may
# start with internal definitions (define and define-values).  R7RS
# requires the body to be equivalent to a letrec*: every binding is
# in scope before any init runs, so closures can mutually reference.
# Body-position begins splice their contents into the outer body.

def _is_head(form, name):
    return (is_cons(form) and is_symbol(form.car)
            and as_symbol(form.car) == name)


def _ensure_local_env(local_env_ref):
    """Create a child env for internal define-syntax if not yet created.
    Saves the outer env in local_env_ref[0] for _expand_body to restore."""
    if local_env_ref[0] is None:
        from pyscheme.Environment import Environment
        outer = _runtime_env_ref[0]
        child = Environment(
            parent=outer) if outer is not None else Environment()
        local_env_ref[0] = outer
        _runtime_env_ref[0] = child


def _prescan_syntax(raw_forms, local_env_ref):
    """Pass 1 of body scanning: install all define-syntax transformers before
    any other forms are expanded (R7RS §5.3.2 - all definitions visible
    throughout the body).  Handles define-syntax directly; recurses into begin
    forms; expands macro calls one level and recurses into begin results."""
    i = 0
    while i < len(raw_forms):
        raw = raw_forms[i]
        if _is_head(raw, 'define-syntax'):
            _ensure_local_env(local_env_ref)
            expand(raw)
        elif _is_head(raw, 'begin'):
            sub = []
            cur = raw.cdr
            while is_cons(cur):
                sub.append(cur.car)
                cur = cur.cdr
            _prescan_syntax(sub, local_env_ref)
        elif is_cons(raw) and is_symbol(raw.car) and _lookup_macro(raw.car) is not None:
            try:
                expanded = expand(raw)
            except Exception:
                i = i + 1
                continue
            if _is_head(expanded, 'begin'):
                sub = []
                cur = expanded.cdr
                while is_cons(cur):
                    sub.append(cur.car)
                    cur = cur.cdr
                _prescan_syntax(sub, local_env_ref)
            elif _is_head(expanded, 'define-syntax'):
                _ensure_local_env(local_env_ref)
                expand(expanded)
        i = i + 1


def _collect_body_forms(body_cons, local_env_ref):
    """Walk a body cons chain with two-pass syntax scanning.

    Pass 1 (_prescan_syntax): install all define-syntax before expanding
    anything so every transformer is visible throughout the body.
    Pass 2: expand remaining forms; splice begin results recursively.

    local_env_ref is a 1-element list [None] shared with _expand_body,
    which restores the outer env in its finally block."""
    raw_forms = []
    cur = body_cons
    while is_cons(cur):
        raw_forms.append(cur.car)
        cur = cur.cdr
    _prescan_syntax(raw_forms, local_env_ref)
    out = []
    i = 0
    while i < len(raw_forms):
        raw = raw_forms[i]
        if _is_head(raw, 'define-syntax'):
            pass
        elif _is_head(raw, 'define-values'):
            out.append(raw)
        else:
            form = expand(raw)
            if _is_head(form, 'begin'):
                out.extend(_collect_body_forms(form.cdr, local_env_ref))
            else:
                out.append(form)
        i = i + 1
    return out


def _expand_body(body_cons, src):
    """Expand a body, hoisting leading internal definitions into a letrec*.
    Handles (define <sym> <init>), (define-values <formals> <expr>), and
    (define-syntax <name> <transformer>); returns a body cons chain ready
    to be placed where the original body went.  No hoisting needed -> the
    body is returned without a wrapper."""
    local_env_ref = [None]
    try:
        forms = _collect_body_forms(body_cons, local_env_ref)
        bindings = []
        body_prefix = []
        i = 0
        while i < len(forms):
            f = forms[i]
            if _is_head(f, 'define'):
                # Canonical (define <sym> <init>) after _expand_define.
                if (not is_cons(f.cdr) or not is_symbol(f.cdr.car)
                        or not is_cons(f.cdr.cdr) or not is_nil(f.cdr.cdr.cdr)):
                    break
                bindings.append((f.cdr.car, f.cdr.cdr.car, f.src))
                i = i + 1
                continue
            if _is_head(f, 'define-values'):
                if (not is_cons(f.cdr) or not is_cons(f.cdr.cdr)
                        or not is_nil(f.cdr.cdr.cdr)):
                    break
                formals_sexpr = f.cdr.car
                expr = f.cdr.cdr.car
                _mv = _mv_collect_formals(formals_sexpr)
                fixed = _mv[0]
                rest = _mv[1]
                if fixed is None:
                    break
                fsrc = f.src
                k = 0
                while k < len(fixed):
                    bindings.append(
                        (make_symbol(fixed[k], fsrc), VOID_VALUE, fsrc))
                    k = k + 1
                if rest is not None:
                    bindings.append(
                        (make_symbol(rest, fsrc), VOID_VALUE, fsrc))
                # Run the multi-value setter IN SOURCE ORDER within the letrec*, as a
                # fresh dummy binding, rather than hoisting it into the body.  The
                # body ran after every binding initializer, so a following
                # (define s (+ p q)) evaluated its init while p/q were still unset.
                # As an in-order binding, letrec* runs the setter before later
                # initializers, so they observe the assigned values (R7RS 5.3.2).
                _dv_setter = _dv_build_setter(fixed, rest, expand(expr), fsrc)
                bindings.append((make_symbol(hygiene_gensym('dv-set'), fsrc),
                                 _dv_setter, fsrc))
                i = i + 1
                continue
            break
        # R7RS §5.3.2: a body is <definition>* <expression>+ -- once the leading
        # run of internal definitions ends (at index i), no further definition
        # may appear.  A define / define-values after an expression is an error.
        _k = i
        while _k < len(forms):
            fk = forms[_k]
            if _is_head(fk, 'define') or _is_head(fk, 'define-values'):
                from pyscheme.Parser import SchemeSyntaxError
                raise SchemeSyntaxError(
                    'definition not allowed after an expression in a body',
                    src_of(fk))
            _k = _k + 1
        if not bindings:
            result = NIL_VALUE
            j = len(forms) - 1
            while j >= 0:
                result = alloc_cons(forms[j], result, src)
                j = j - 1
            return result
        rest_forms = []
        _ri = 0
        while _ri < len(body_prefix):
            rest_forms.append(body_prefix[_ri])
            _ri = _ri + 1
        _ri = i
        while _ri < len(forms):
            rest_forms.append(forms[_ri])
            _ri = _ri + 1
        # Per-application intro scope: count occurrences of each name.
        # Names appearing exactly once go into rename_table so body refs are
        # renamed to the unique gensym.  Names appearing more than once (from
        # repeated macro applications) each get a distinct gensym but are NOT
        # added to rename_table -- body refs stay plain and become unbound,
        # which is the correct R7RS hygiene behavior.
        name_counts = {}
        j = 0
        while j < len(bindings):
            n = as_symbol(bindings[j][0])
            name_counts[n] = name_counts.get(n, 0) + 1
            j = j + 1
        gensym_names = []
        j = 0
        while j < len(bindings):
            n = as_symbol(bindings[j][0])
            gensym_names.append(hygiene_gensym(n))
            j = j + 1
        rename_table = {}
        j = 0
        while j < len(bindings):
            n = as_symbol(bindings[j][0])
            if name_counts[n] == 1:
                rename_table[n] = gensym_names[j]
            j = j + 1
        bindings_chain = NIL_VALUE
        j = len(bindings) - 1
        while j >= 0:
            gs_sym = make_symbol(gensym_names[j], bindings[j][2])
            init = _rename_refs_in_form(bindings[j][1], rename_table)
            fsrc = bindings[j][2]
            pair = list_from_items([gs_sym, init], fsrc)
            bindings_chain = alloc_cons(pair, bindings_chain, src)
            j = j - 1
        renamed_rest = []
        j = 0
        while j < len(rest_forms):
            renamed_rest.append(_rename_refs_in_form(
                rest_forms[j], rename_table))
            j = j + 1
        rest_chain = NIL_VALUE
        j = len(renamed_rest) - 1
        while j >= 0:
            rest_chain = alloc_cons(renamed_rest[j], rest_chain, src)
            j = j - 1
        letrec_sym = make_symbol('letrec*', src)
        letrec_form = alloc_cons(letrec_sym,
                                 alloc_cons(bindings_chain, rest_chain, src),
                                 src)
        return alloc_cons(letrec_form, NIL_VALUE, src)
    finally:
        if local_env_ref[0] is not None:
            _runtime_env_ref[0] = local_env_ref[0]


# ---- sugar handlers (each returns an S-expression) ----

def _expand_define(sexpr):
    # (define (name args...) body...)      =>  (define name (lambda (args...) body))
    # (define (name . rest) body...)       =>  (define name (lambda rest body))
    # (define (name a b . rest) body...)   =>  (define name (lambda (a b . rest) body))
    # sexpr = cons(define-sym, cons(signature-or-name, body-cons))
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    signature = sexpr.cdr.car
    body_cons = sexpr.cdr.cdr
    # Only (define (name ...) ...) triggers sugar.
    if not is_cons(signature):
        return _expand_list(sexpr)
    if is_nil(body_cons):
        return _expand_list(sexpr)
    name_sexpr = signature.car
    params_tail = signature.cdr
    # Params: pure-variadic if signature's cdr is an atom-symbol (name . rest).
    # Both that case and the list/dotted case carry the tail through verbatim.
    params_node = params_tail
    sig_src = signature.src
    lambda_sym = make_symbol('lambda', sig_src)
    # Apply body hoisting (R7RS 5.3.2) so internal defines inside the
    # procedure body become a letrec*.
    body_chain = _expand_body(body_cons, sig_src)
    # (lambda <params> . body)
    params_and_body = alloc_cons(params_node, body_chain, sig_src)
    lambda_form = alloc_cons(lambda_sym,  params_and_body, sig_src)
    # (define <name> <lambda>)
    define_src = sexpr.src
    define_sym = make_symbol('define', define_src)
    lambda_cons = alloc_cons(lambda_form, NIL_VALUE,  define_src)
    name_cons = alloc_cons(name_sexpr,  lambda_cons, define_src)
    return alloc_cons(define_sym, name_cons, define_src)


# ---- alpha-rename helpers ------------------------------------------------
# Binding sites generate fresh gensym names; references in scope are
# substituted.  This replaces the sets-of-scopes mechanism entirely.

def _collect_formals_names(formals):
    """Set of names bound by a formals cons chain (possibly improper).
    Delegates to the shared binder-extractor in syntax_rules so the two
    hygiene walkers can't disagree on what a formals list binds."""
    return set(formals_bound_names(formals))


def _collect_let_bound_names(bindings):
    """Set of names bound by a let/letrec binding list.  Delegates to the
    shared binder-extractor in syntax_rules (single source of truth)."""
    return set(let_binding_names(bindings))


def _mask_table(table, bound):
    """Return table with keys in bound removed. Returns table itself if bound is empty."""
    if not bound:
        return table
    return {k: v for k, v in table.items() if k not in bound}


def _map_list_cars(cons_list, fn):
    """Apply fn to each car of cons_list; rebuild list only if any car changed.
    Preserves the tail (possibly non-NIL for improper lists)."""
    changed = False
    items = []
    cur = cons_list
    while is_cons(cur):
        new_item = fn(cur.car)
        if new_item is not cur.car:
            changed = True
        items.append((new_item, cur.src))
        cur = cur.cdr
    if not changed:
        return cons_list
    tail = cur
    i = len(items) - 1
    while i >= 0:
        tail = alloc_cons(items[i][0], tail, items[i][1])
        i -= 1
    return tail


def _gensym_rename_formals(formals, rename_table, src):
    """Walk formals (possibly improper list), gensym each name,
    populate rename_table {original: gensym}, return new formals.
    Raises SchemeSyntaxError for duplicate parameter names before renaming."""
    from pyscheme.Parser import SchemeSyntaxError
    if is_symbol(formals):
        name = as_symbol(formals)
        gs = hygiene_gensym(name)
        rename_table[name] = gs
        return make_symbol(gs, src_of(formals))
    if is_nil(formals):
        return formals
    seen = set()
    items = []
    cur = formals
    while is_cons(cur):
        sym = cur.car
        if is_symbol(sym):
            name = as_symbol(sym)
            if name in seen:
                raise SchemeSyntaxError(
                    'duplicate parameter name in lambda: ' + name, src_of(sym))
            seen.add(name)
            gs = hygiene_gensym(name)
            rename_table[name] = gs
            items.append(make_symbol(gs, src_of(sym)))
        else:
            items.append(sym)
        cur = cur.cdr
    if is_nil(cur):
        tail = NIL_VALUE
    elif is_symbol(cur):
        name = as_symbol(cur)
        if name in seen:
            raise SchemeSyntaxError(
                'rest parameter name conflicts with fixed parameter: ' + name, src_of(cur))
        gs = hygiene_gensym(name)
        rename_table[name] = gs
        tail = make_symbol(gs, src_of(cur))
    else:
        tail = cur
    result = tail
    i = len(items) - 1
    while i >= 0:
        result = alloc_cons(items[i], result, src)
        i = i - 1
    return result


def _rename_refs_in_form(form, rename_table):
    """Substitute free variable references named in rename_table.
    Scope-aware: masks names re-bound by inner binding forms.  Does not rename
    inside (quote ...), quasiquoted template data, case datum-lists, or
    binding-site positions.

    Masks the binders of syntax_rules.EXPANDED_BINDING_HEADS (this walker's
    roster -- see that set's comment for why it differs from the template
    walker collect_binding_intros).  Shares the binder extractors
    formals_bound_names / let_binding_names with it."""
    if not rename_table:
        return form
    if is_symbol(form):
        name = as_symbol(form)
        new_name = rename_table.get(name)
        if new_name is not None:
            return make_symbol(new_name, src_of(form))
        return form
    if is_vector(form):
        items = as_vector_items(form)
        new_items = []
        changed = False
        i = 0
        while i < len(items):
            ni = _rename_refs_in_form(items[i], rename_table)
            new_items.append(ni)
            if ni is not items[i]:
                changed = True
            i = i + 1
        if not changed:
            return form
        return make_vector(new_items)
    if not is_cons(form):
        return form
    head = form.car
    if is_symbol(head):
        hname = as_symbol(head)
        if hname == 'quote':
            gs = rename_table.get(hname)
            if gs is None:
                return form
            new_head = make_symbol(gs, src_of(head))
            return alloc_cons(new_head, form.cdr, form.src)
        if hname == 'quasiquote' and rename_table.get(hname) is None \
                and is_cons(form.cdr) and is_nil(form.cdr.cdr):
            # Walk the template with quasiquote-level tracking so that only
            # *active* (unquoted) references are renamed; quoted template data
            # is left untouched.  A plain (quote ...) inside a template must not
            # short-circuit a deeper (unquote ...) -- cf. `,'<var>` patterns.
            tmpl = form.cdr.car
            new_tmpl = _rrif_quasiquote_template(tmpl, rename_table, 1)
            if new_tmpl is tmpl:
                return form
            return alloc_cons(head, alloc_cons(new_tmpl, NIL_VALUE, form.cdr.src), form.src)
        if hname == 'lambda':
            return _rrif_lambda(form, rename_table)
        if hname in ('let', 'let*', 'letrec', 'letrec*'):
            return _rrif_let(form, rename_table, hname)
        if hname == 'case-lambda':
            return _rrif_case_lambda(form, rename_table)
        if hname == 'case':
            return _rrif_case(form, rename_table)
        if hname in ('let-syntax', 'letrec-syntax'):
            return _rrif_let_syntax(form, rename_table, hname == 'letrec-syntax')
    new_head = _rename_refs_in_form(head, rename_table)
    new_cdr = _rename_refs_in_form(form.cdr, rename_table)
    if new_head is head and new_cdr is form.cdr:
        return form
    return alloc_cons(new_head, new_cdr, form.src)


def _rrif_lambda(form, rename_table):
    """_rename_refs_in_form for (lambda formals body...) forms."""
    if not is_cons(form.cdr):
        return _rrif_default(form, rename_table)
    formals = form.cdr.car
    body_cons = form.cdr.cdr
    inner = _mask_table(rename_table, _collect_formals_names(formals))
    new_body = _rename_refs_in_form(body_cons, inner)
    if new_body is body_cons:
        return form
    return alloc_cons(form.car, alloc_cons(formals, new_body, form.cdr.src), form.src)


def _rrif_let(form, rename_table, kind):
    """_rename_refs_in_form for (let|let*|letrec|letrec* ...) forms."""
    head = form.car
    if not is_cons(form.cdr):
        return _rrif_default(form, rename_table)
    if kind == 'let' and is_symbol(form.cdr.car):
        if not is_cons(form.cdr.cdr):
            return _rrif_default(form, rename_table)
        loop_sym = form.cdr.car
        bindings = form.cdr.cdr.car
        body_cons = form.cdr.cdr.cdr
        bound = _collect_let_bound_names(bindings) | {as_symbol(loop_sym)}
        new_bindings = _rrif_bindings_parallel(bindings, rename_table)
        new_body = _rename_refs_in_form(
            body_cons, _mask_table(rename_table, bound))
        if new_bindings is bindings and new_body is body_cons:
            return form
        rest = alloc_cons(new_bindings, new_body, form.cdr.cdr.src)
        return alloc_cons(head, alloc_cons(loop_sym, rest, form.cdr.src), form.src)
    bindings = form.cdr.car
    body_cons = form.cdr.cdr
    body_table = _mask_table(rename_table, _collect_let_bound_names(bindings))
    if kind == 'let':
        new_bindings = _rrif_bindings_parallel(bindings, rename_table)
    elif kind == 'let*':
        new_bindings = _rrif_bindings_let_star(bindings, rename_table)
    else:
        new_bindings = _rrif_bindings_parallel(bindings, body_table)
    new_body = _rename_refs_in_form(body_cons, body_table)
    if new_bindings is bindings and new_body is body_cons:
        return form
    return alloc_cons(head, alloc_cons(new_bindings, new_body, form.cdr.src), form.src)


def _rrif_case_lambda(form, rename_table):
    """_rename_refs_in_form for (case-lambda (formals body...)...) forms."""
    def _rename_clause(clause):
        if not is_cons(clause):
            return clause
        formals = clause.car
        body_cons = clause.cdr
        inner = _mask_table(rename_table, _collect_formals_names(formals))
        new_body = _rename_refs_in_form(body_cons, inner)
        if new_body is body_cons:
            return clause
        return alloc_cons(formals, new_body, clause.src)
    new_cdr = _map_list_cars(form.cdr, _rename_clause)
    if new_cdr is form.cdr:
        return form
    return alloc_cons(form.car, new_cdr, form.src)


def _rrif_default(form, rename_table):
    """Rename all sub-forms (used when head is not a special form)."""
    new_head = _rename_refs_in_form(form.car, rename_table)
    new_cdr = _rename_refs_in_form(form.cdr, rename_table)
    if new_head is form.car and new_cdr is form.cdr:
        return form
    return alloc_cons(new_head, new_cdr, form.src)


def _rrif_case(form, rename_table):
    """_rename_refs_in_form for (case key clause...) forms.
    Renames key and clause bodies; datum lists are literal datums, not renamed."""
    if not is_cons(form.cdr):
        return _rrif_default(form, rename_table)
    new_key = _rename_refs_in_form(form.cdr.car, rename_table)

    def _rename_clause(clause):
        if not is_cons(clause):
            return clause
        head = clause.car
        body = clause.cdr
        # A symbol head (i.e., 'else') is renamed so shadowing is detected.
        # A list head is a datum-list; its contents are literal values, not renamed.
        new_head = _rename_refs_in_form(
            head, rename_table) if is_symbol(head) else head
        new_body = _rename_refs_in_form(body, rename_table)
        if new_head is head and new_body is body:
            return clause
        return alloc_cons(new_head, new_body, clause.src)
    new_clauses = _map_list_cars(form.cdr.cdr, _rename_clause)
    if new_key is form.cdr.car and new_clauses is form.cdr.cdr:
        return form
    return alloc_cons(form.car, alloc_cons(new_key, new_clauses, form.cdr.src), form.src)


def _rrif_let_syntax(form, rename_table, is_letrec):
    """_rename_refs_in_form for (let-syntax|letrec-syntax ...) forms.
    let-syntax: transformer expressions see the outer rename_table (so outer
    renames like f->h.f.A propagate into templates); body masks binding names.
    letrec-syntax: transformer expressions also mask binding names (letrec scope).
    """
    if not is_cons(form.cdr):
        return _rrif_default(form, rename_table)
    bindings = form.cdr.car
    body_cons = form.cdr.cdr
    body_table = _mask_table(rename_table, _collect_let_bound_names(bindings))
    tr_table = body_table if is_letrec else rename_table

    def _rename_binding(b):
        if is_cons(b) and is_symbol(b.car) and is_cons(b.cdr):
            new_tr = _rename_refs_in_form(b.cdr.car, tr_table)
            if new_tr is not b.cdr.car:
                return alloc_cons(b.car, alloc_cons(new_tr, b.cdr.cdr, b.cdr.src), b.src)
        return b
    new_bindings = _map_list_cars(bindings, _rename_binding)
    new_body = _rename_refs_in_form(body_cons, body_table)
    if new_bindings is bindings and new_body is body_cons:
        return form
    new_cdr = alloc_cons(new_bindings, new_body, form.cdr.src)
    return alloc_cons(form.car, new_cdr, form.src)


def _rrif_bindings_parallel(bindings, table):
    """Rename init expressions in binding pairs with table; don't touch names."""
    if not table:
        return bindings

    def _rename_pair(pair):
        if is_cons(pair) and is_cons(pair.cdr):
            new_init = _rename_refs_in_form(pair.cdr.car, table)
            if new_init is not pair.cdr.car:
                return alloc_cons(pair.car,
                                  alloc_cons(new_init, pair.cdr.cdr, pair.cdr.src), pair.src)
        return pair
    return _map_list_cars(bindings, _rename_pair)


def _rrif_bindings_let_star(bindings, table):
    """Rename let* init expressions progressively, masking each name after use."""
    if not table:
        return bindings
    current_table = dict(table)

    def _rename_pair(pair):
        if is_cons(pair) and is_symbol(pair.car) and is_cons(pair.cdr):
            name = as_symbol(pair.car)
            new_init = _rename_refs_in_form(pair.cdr.car, current_table)
            current_table.pop(name, None)
            if new_init is not pair.cdr.car:
                return alloc_cons(pair.car,
                                  alloc_cons(new_init, pair.cdr.cdr, pair.cdr.src), pair.src)
        return pair
    return _map_list_cars(bindings, _rename_pair)


def _rrif_quasiquote_template(tmpl, rename_table, level):
    """Walk a quasiquote template at nesting `level` (>= 1), renaming only the
    active (unquoted) sub-expressions per rename_table and leaving quoted
    template data untouched.  Mirrors the level logic in qq_walk: quasiquote
    raises the level, unquote/unquote-splicing lower it, and an expression
    becomes active when it reaches level 0."""
    if is_cons(tmpl):
        head = tmpl.car
        if is_symbol(head):
            hname = as_symbol(head)
            if hname in ('unquote', 'unquote-splicing') \
                    and is_cons(tmpl.cdr) and is_nil(tmpl.cdr.cdr):
                e = tmpl.cdr.car
                if level == 1:
                    new_e = _rename_refs_in_form(
                        e, rename_table)        # active code
                else:
                    new_e = _rrif_quasiquote_template(
                        e, rename_table, level - 1)
                if new_e is e:
                    return tmpl
                return alloc_cons(head, alloc_cons(new_e, NIL_VALUE, tmpl.cdr.src), tmpl.src)
            if hname == 'quasiquote' and is_cons(tmpl.cdr) and is_nil(tmpl.cdr.cdr):
                e = tmpl.cdr.car
                new_e = _rrif_quasiquote_template(e, rename_table, level + 1)
                if new_e is e:
                    return tmpl
                return alloc_cons(head, alloc_cons(new_e, NIL_VALUE, tmpl.cdr.src), tmpl.src)
        # Ordinary template list (incl. dotted tails): recurse car and cdr at
        # the same level so nested unquotes are still found.
        new_car = _rrif_quasiquote_template(tmpl.car, rename_table, level)
        new_cdr = _rrif_quasiquote_template(tmpl.cdr, rename_table, level)
        if new_car is tmpl.car and new_cdr is tmpl.cdr:
            return tmpl
        return alloc_cons(new_car, new_cdr, tmpl.src)
    if is_vector(tmpl):
        items = as_vector_items(tmpl)
        new_items = []
        changed = False
        i = 0
        while i < len(items):
            ni = _rrif_quasiquote_template(items[i], rename_table, level)
            new_items.append(ni)
            if ni is not items[i]:
                changed = True
            i = i + 1
        if not changed:
            return tmpl
        return make_vector(new_items)
    # Atom (including symbols appearing as template data): unchanged.
    return tmpl


# ---- let family (let, let*, letrec, letrec*) -----------------------------

def _expand_let_family(sexpr, head_name):
    # Generic shape: (let|let*|letrec|letrec* <bindings> <body>...).
    # Plain let also accepts the named form: (let <name> <bindings> <body>...).
    # Generate a fresh gensym for each binding name; rename refs in body
    # (and inits for letrec/letrec*); expand inits; expand body.
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    src = sexpr.src
    head_sym = make_symbol(head_name, src)
    first = sexpr.cdr.car
    rest = sexpr.cdr.cdr
    named_name_sym = None
    if head_name == 'let' and is_symbol(first) and is_cons(rest):
        named_name_sym = first
        bindings_form = rest.car
        body_cons = rest.cdr
    else:
        bindings_form = first
        body_cons = rest
    if not (is_cons(bindings_form) or is_nil(bindings_form)):
        return _expand_list(sexpr)
    if is_nil(body_cons):
        return _expand_list(sexpr)
    # Collect raw (name, init, pair_src) triples.
    raw_pairs = []
    cur = bindings_form
    while is_cons(cur):
        pair = cur.car
        if (not is_cons(pair) or not is_symbol(pair.car)
                or not is_cons(pair.cdr) or not is_nil(pair.cdr.cdr)):
            return _expand_list(sexpr)
        raw_pairs.append((as_symbol(pair.car), pair.cdr.car, pair.src))
        cur = cur.cdr
    if not is_nil(cur):
        return _expand_list(sexpr)
    # Check for duplicate binding names (error for let/letrec/letrec*, ok for let*).
    if head_name != 'let*':
        from pyscheme.Parser import SchemeSyntaxError
        seen_names = set()
        i = 0
        while i < len(raw_pairs):
            n = raw_pairs[i][0]
            if n in seen_names:
                raise SchemeSyntaxError(
                    'duplicate variable name in ' + head_name + ' bindings: ' + n,
                    src)
            seen_names.add(n)
            i = i + 1
    # Generate gensyms for all binding names.
    rename_table = {}
    i = 0
    while i < len(raw_pairs):
        name = raw_pairs[i][0]
        rename_table[name] = hygiene_gensym(name)
        i = i + 1
    new_named_sym = None
    if named_name_sym is not None:
        loop_name = as_symbol(named_name_sym)
        loop_gs = hygiene_gensym(loop_name)
        rename_table[loop_name] = loop_gs
        new_named_sym = make_symbol(loop_gs, src_of(named_name_sym))
    # Build new binding pairs with renamed names + expanded inits.
    new_pairs = []
    if head_name == 'let':
        # inits see outer scope: don't apply rename_table to inits
        i = 0
        while i < len(raw_pairs):
            orig_name, init_expr, pair_src = raw_pairs[i][0], raw_pairs[i][1], raw_pairs[i][2]
            gs_sym = make_symbol(rename_table[orig_name], src)
            new_init = expand(init_expr)
            new_pairs.append(list_from_items([gs_sym, new_init], pair_src))
            i = i + 1
    elif head_name == 'let*':
        # each init sees only the preceding bindings' renamed names
        progressive = {}
        i = 0
        while i < len(raw_pairs):
            orig_name, init_expr, pair_src = raw_pairs[i][0], raw_pairs[i][1], raw_pairs[i][2]
            renamed_init = _rename_refs_in_form(init_expr, progressive)
            new_init = expand(renamed_init)
            gs_sym = make_symbol(rename_table[orig_name], src)
            new_pairs.append(list_from_items([gs_sym, new_init], pair_src))
            progressive[orig_name] = rename_table[orig_name]
            i = i + 1
    else:
        # letrec / letrec*: all binding names visible in all inits
        i = 0
        while i < len(raw_pairs):
            orig_name, init_expr, pair_src = raw_pairs[i][0], raw_pairs[i][1], raw_pairs[i][2]
            renamed_init = _rename_refs_in_form(init_expr, rename_table)
            new_init = expand(renamed_init)
            gs_sym = make_symbol(rename_table[orig_name], src)
            new_pairs.append(list_from_items([gs_sym, new_init], pair_src))
            i = i + 1
    # Build new bindings cons chain.
    new_bindings = NIL_VALUE
    i = len(new_pairs) - 1
    while i >= 0:
        new_bindings = alloc_cons(new_pairs[i], new_bindings, src)
        i = i - 1
    # Rename body refs and expand.
    renamed_body = _rename_refs_in_form(body_cons, rename_table)
    expanded_body = _expand_body(renamed_body, src)
    if new_named_sym is not None:
        tail = alloc_cons(new_named_sym,
                          alloc_cons(new_bindings, expanded_body, src),
                          src)
    else:
        tail = alloc_cons(new_bindings, expanded_body, src)
    return alloc_cons(head_sym, tail, src)


def _expand_let(sexpr):
    return _expand_let_family(sexpr, 'let')


def _expand_let_star(sexpr):
    return _expand_let_family(sexpr, 'let*')


def _expand_letrec(sexpr):
    return _expand_let_family(sexpr, 'letrec')


def _expand_letrec_star(sexpr):
    return _expand_let_family(sexpr, 'letrec*')


def _expand_lambda(sexpr):
    # (lambda <formals> <body>...)
    # Gensym each formal name; rename refs in body; expand body with hoisting.
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    formals = sexpr.cdr.car
    body = sexpr.cdr.cdr
    if is_nil(body):
        return _expand_list(sexpr)
    src = sexpr.src
    rename_table = {}
    new_formals = _gensym_rename_formals(formals, rename_table, src)
    renamed_body = _rename_refs_in_form(body, rename_table)
    expanded_body = _expand_body(renamed_body, src)
    lambda_sym = make_symbol('lambda', src)
    return alloc_cons(lambda_sym,
                      alloc_cons(new_formals, expanded_body, src),
                      src)


def _expand_when_unless(sexpr, head_name):
    # (when <test> <body>...) / (unless <test> <body>...)
    # Body supports internal definitions (R7RS 4.2.1).
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    test = sexpr.cdr.car
    body = sexpr.cdr.cdr
    if is_nil(body):
        return _expand_list(sexpr)
    src = sexpr.src
    head_sym = make_symbol(head_name, src)
    expanded_body = _expand_body(body, src)
    return alloc_cons(head_sym,
                      alloc_cons(expand(test), expanded_body, src),
                      src)


def _expand_when(sexpr):
    return _expand_when_unless(sexpr, 'when')


def _expand_unless(sexpr):
    return _expand_when_unless(sexpr, 'unless')


def _expand_case_lambda(sexpr):
    # (case-lambda <clause>...)  where each clause is (<formals> <body>...).
    # Gensym each clause's formals; rename refs in body; expand body.
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    src = sexpr.src
    head_sym = make_symbol('case-lambda', src)
    expanded_clauses = []
    cur = sexpr.cdr
    while is_cons(cur):
        clause = cur.car
        if not is_cons(clause) or not is_cons(clause.cdr):
            return _expand_list(sexpr)
        formals = clause.car
        body = clause.cdr
        rename_table = {}
        new_formals = _gensym_rename_formals(formals, rename_table, src)
        renamed_body = _rename_refs_in_form(body, rename_table)
        expanded_body = _expand_body(renamed_body, clause.src)
        expanded_clauses.append(alloc_cons(
            new_formals, expanded_body, clause.src))
        cur = cur.cdr
    if not is_nil(cur):
        return _expand_list(sexpr)
    tail = NIL_VALUE
    i = len(expanded_clauses) - 1
    while i >= 0:
        tail = alloc_cons(expanded_clauses[i], tail, src)
        i = i - 1
    return alloc_cons(head_sym, tail, src)


def _expand_if(sexpr):
    # (if t e)  =>  (if t e #<void>)
    if _list_length(sexpr) != 3:
        return _expand_list(sexpr)
    test = sexpr.cdr.car
    then_br = sexpr.cdr.cdr.car
    src = sexpr.src
    if_sym = make_symbol('if', src)
    tail3 = alloc_cons(VOID_VALUE,      NIL_VALUE, src)
    tail2 = alloc_cons(expand(then_br), tail3,     src)
    tail1 = alloc_cons(expand(test),    tail2,     src)
    return alloc_cons(if_sym, tail1, src)


def _expand_quote(sexpr):
    # Quoted data is literal - do not walk into the datum.
    return sexpr


def _expand_do(sexpr):
    # (do ((var init [step]) ...) (test result...) command...)
    # Rewrite to:
    #    (let %do-loop% ((var init) ...)
    #       (if test <result-form> <body-form>))
    # where result-form is VOID for empty, bare expr for one, (begin ...)
    # otherwise; body-form is the recursive call alone, or (begin body...
    # recur-call) when commands are present.  Variables with no step
    # expression stay at their current value (the variable is passed
    # forward to itself).
    #
    # If any shape check fails, punt so _analyze_do can raise a
    # diagnostic with the original do's source position.
    if _list_length(sexpr) < 3:
        return _expand_list(sexpr)
    bindings_cons = sexpr.cdr.car
    test_cons = sexpr.cdr.cdr.car
    body_cons = sexpr.cdr.cdr.cdr

    if _list_length(bindings_cons) < 0:
        return _expand_list(sexpr)
    if not is_cons(test_cons):
        return _expand_list(sexpr)
    if _list_length(test_cons) < 1:
        return _expand_list(sexpr)

    # Walk bindings: each must be (var init) or (var init step).
    vars_list = []
    inits_list = []
    steps_list = []   # entries are expanded step exprs, or None when absent
    cur = bindings_cons
    while is_cons(cur):
        b = cur.car
        blen = _list_length(b)
        if blen != 2 and blen != 3:
            return _expand_list(sexpr)
        var_sexpr = b.car
        if not is_symbol(var_sexpr):
            return _expand_list(sexpr)
        vars_list.append(var_sexpr)
        inits_list.append(expand(b.cdr.car))
        if blen == 3:
            steps_list.append(expand(b.cdr.cdr.car))
        else:
            steps_list.append(None)
        cur = cur.cdr

    # Duplicate-name guard so _analyze_do can raise with a do-specific
    # message (otherwise the desugared named-let would flag it but with
    # a "named let" label).
    seen = set()
    i = 0
    while i < len(vars_list):
        name = as_symbol(vars_list[i])
        if name in seen:
            return _expand_list(sexpr)
        seen.add(name)
        i = i + 1

    test_expr = expand(test_cons.car)

    src = sexpr.src
    loop_name = make_symbol(hygiene_gensym('do-loop'), src)

    # Build the recursive call (loop-name step1 step2 ...).  If a variable
    # has no step expression, pass its current value forward by referring
    # to the variable itself.
    call_items = []
    call_items.append(loop_name)
    i = 0
    while i < len(vars_list):
        if steps_list[i] is not None:
            call_items.append(steps_list[i])
        else:
            call_items.append(vars_list[i])
        i = i + 1
    recur_call = list_from_items(call_items, src)

    # Body form: recur_call alone, or (begin commands... recur_call).
    body_items = []
    cur = body_cons
    while is_cons(cur):
        body_items.append(expand(cur.car))
        cur = cur.cdr
    if not body_items:
        body_form = recur_call
    else:
        begin_sym = make_symbol('begin', src)
        seq = []
        seq.append(begin_sym)
        i = 0
        while i < len(body_items):
            seq.append(body_items[i])
            i = i + 1
        seq.append(recur_call)
        body_form = list_from_items(seq, src)

    # Result form: VOID for empty, bare expr for one, (begin ...) for >=2.
    result_items = []
    cur = test_cons.cdr
    while is_cons(cur):
        result_items.append(expand(cur.car))
        cur = cur.cdr
    if not result_items:
        result_form = VOID_VALUE
    elif len(result_items) == 1:
        result_form = result_items[0]
    else:
        begin_sym = make_symbol('begin', src)
        seq = []
        seq.append(begin_sym)
        i = 0
        while i < len(result_items):
            seq.append(result_items[i])
            i = i + 1
        result_form = list_from_items(seq, src)

    # Build (if test result body).
    if_sym = make_symbol('if', src)
    if_form = list_from_items([if_sym, test_expr, result_form, body_form], src)

    # Build ((var init) ...).
    binding_forms = []
    i = 0
    while i < len(vars_list):
        pair = list_from_items([vars_list[i], inits_list[i]], src)
        binding_forms.append(pair)
        i = i + 1
    let_bindings = list_from_items(binding_forms, src)

    # Build (let <loop-name> <bindings> <if>).  loop-name is a fresh gensym
    # so user code that happens to bind a name like %do-loop% does not
    # shadow our recursive call.
    let_sym = make_symbol('let', src)
    return list_from_items([let_sym, loop_name, let_bindings, if_form], src)


def _case_fold(form):
    """Return a copy of form with every symbol's name lowercased."""
    if is_cons(form):
        return alloc_cons(_case_fold(form.car), _case_fold(form.cdr), form.src)
    if is_symbol(form):
        return make_symbol(as_symbol(form).lower(), src_of(form))
    return form


def _read_and_expand_file(filename, src, fold):
    """Open `filename`, parse its contents, expand each form, return a
    Python list of the expanded forms.  Raises SchemeSyntaxError on
    file-not-found.  When `fold` is true, symbol names are lowercased
    before expansion (include-ci semantics)."""
    from pyscheme.Parser import parse, SchemeSyntaxError
    try:
        f = open(filename, 'r', encoding='utf-8')
    except FileNotFoundError:
        raise SchemeSyntaxError("include: file not found: " + filename, src)
    source = f.read()
    f.close()
    raw = parse(source, filename)
    expanded = []
    i = 0
    while i < len(raw):
        form = raw[i]
        if fold:
            form = _case_fold(form)
        expanded.append(expand(form))
        i = i + 1
    return expanded


# Set by sessionLog_test to the log file's directory so that
# include/include-library-declarations in test expressions resolve relative
# to the test file rather than the process CWD.
_include_fallback_dir = ''


def _include_base_dir(src):
    """Return the directory to resolve a relative include path against.
    When the including form was parsed from a real file, use that file's
    parent.  REPL input (REPL_FILENAME) falls back to _include_fallback_dir,
    which sessionLog_test sets to the test log's directory."""
    if src is None:
        return _include_fallback_dir
    fname = src.filename
    if not fname or fname == REPL_FILENAME:
        return _include_fallback_dir
    return os.path.dirname(fname)


def _expand_include_form(sexpr, fold):
    """Shared body of _expand_include / _expand_include_ci."""
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    src = sexpr.src
    base_dir = _include_base_dir(src)
    all_forms = []
    cur = sexpr.cdr
    while is_cons(cur):
        path_val = cur.car
        if not is_string(path_val):
            return _expand_list(sexpr)
        requested = as_string(path_val)
        # os.path.join returns `requested` unchanged when it is absolute.
        resolved = os.path.join(base_dir, requested) if base_dir else requested
        from_file = _read_and_expand_file(resolved, src, fold)
        j = 0
        while j < len(from_file):
            all_forms.append(from_file[j])
            j = j + 1
        cur = cur.cdr
    # Wrap in (begin <forms>).  Empty include becomes (begin), which the
    # analyzer will reject - punt in that case so _analyze_include
    # produces a useful message.
    if not all_forms:
        return _expand_list(sexpr)
    begin_sym = make_symbol('begin', src)
    items = [begin_sym]
    j = 0
    while j < len(all_forms):
        items.append(all_forms[j])
        j = j + 1
    return list_from_items(items, src)


def _expand_include(sexpr):
    # (include "file1" "file2" ...)
    return _expand_include_form(sexpr, False)


def _expand_include_ci(sexpr):
    # (include-ci "file1" "file2" ...)
    # Case-fold symbol names in the included source (R7RS 5.6.1).
    return _expand_include_form(sexpr, True)


# R7RS 5.6.2 / Appendix B feature identifiers.  `pyscheme` is our
# implementation identifier; `r7rs` indicates R7RS-small compliance.
import sys as _sys
_FEATURES = {
    'r7rs':           True,
    'exact-closed':   True,   # algebraic ops on exact nums stay exact
    'exact-rational': True,   # exact arithmetic always yields exact rationals
    'ratios':         True,   # exact rational numbers (Fraction) supported
    # inexact reals are IEEE 754 doubles (Python float)
    'ieee-float':     True,
    'full-unicode':   True,   # Python strings are full Unicode
    'pyscheme':       True,
}
if _sys.platform == 'win32':
    _FEATURES['windows'] = True
elif _sys.platform.startswith('linux'):
    _FEATURES['posix'] = True
    _FEATURES['linux'] = True
elif _sys.platform == 'darwin':
    _FEATURES['posix'] = True
    _FEATURES['darwin'] = True
del _sys


def _feature_req_matches(req):
    """Evaluate an R7RS cond-expand feature requirement statically."""
    if is_symbol(req):
        name = as_symbol(req)
        if name == 'else':
            return True
        return name in _FEATURES
    if not is_cons(req):
        return False
    head = req.car
    if not is_symbol(head):
        return False
    op = as_symbol(head)
    if op == 'and':
        cur = req.cdr
        while is_cons(cur):
            if not _feature_req_matches(cur.car):
                return False
            cur = cur.cdr
        return True
    if op == 'or':
        cur = req.cdr
        while is_cons(cur):
            if _feature_req_matches(cur.car):
                return True
            cur = cur.cdr
        return False
    if op == 'not':
        return not _feature_req_matches(req.cdr.car)
    if op == 'library':
        from pyscheme.library import library_name_to_key, library_registered_p
        if not is_cons(req.cdr):
            return False
        try:
            key = library_name_to_key(req.cdr.car)
        except ValueError:
            return False
        return library_registered_p(key)
    return False


def _qq_make_cons(car_form, cdr_form, src):
    """Emit (cons car_form cdr_form) - a runtime cons-cell construction."""
    return list_from_items(
        [make_symbol('cons', src), car_form, cdr_form], src)


def _qq_make_append(first, second, src):
    """Emit (append first second) - runtime list concatenation."""
    return list_from_items(
        [make_symbol('append', src), first, second], src)


def _qq_make_list(a, b, src):
    """Emit (list a b)."""
    return list_from_items(
        [make_symbol('list', src), a, b], src)


def _qq_quote(x, src):
    """Emit (quote x) - preserves x as a literal datum."""
    return list_from_items([make_symbol('quote', src), x], src)


def _qq_walk(x, level, default_src):
    """Transform a quasiquoted template into an expression that, when
    evaluated, reconstructs the template with unquoted holes filled in.

    `level` counts nesting: 1 is the outermost quasiquote, and nested
    quasiquotes push the level higher.  Unquote at level 1 produces a
    runtime value; at level >1 it's preserved as literal syntax with
    its argument recursively walked at level-1."""
    from pyscheme.Parser import SchemeSyntaxError

    if is_cons(x):
        head = x.car
        src = x.src if x.src is not None else default_src
        if is_symbol(head):
            name = as_symbol(head)
            if name == 'unquote':
                if not is_cons(x.cdr) or not is_nil(x.cdr.cdr):
                    raise SchemeSyntaxError(
                        'unquote requires exactly one argument', src)
                e = x.cdr.car
                if level == 1:
                    return expand(e)
                return _qq_make_list(
                    _qq_quote(make_symbol('unquote', src), src),
                    _qq_walk(e, level - 1, src),
                    src)
            if name == 'quasiquote':
                if not is_cons(x.cdr) or not is_nil(x.cdr.cdr):
                    raise SchemeSyntaxError(
                        'quasiquote requires exactly one argument', src)
                e = x.cdr.car
                return _qq_make_list(
                    _qq_quote(make_symbol('quasiquote', src), src),
                    _qq_walk(e, level + 1, src),
                    src)
            if name == 'unquote-splicing':
                # unquote-splicing at the top of a template is unspecified in R7RS
                # (cf. §4.2.8 footnote); we evaluate the argument and return its
                # value as-is, matching the "whole list" interpretation used by
                # several Schemes (e.g., Chez, Gauche).
                if not is_cons(x.cdr) or not is_nil(x.cdr.cdr):
                    raise SchemeSyntaxError(
                        'unquote-splicing requires exactly one argument', src)
                if level == 1:
                    return expand(x.cdr.car)
                return _qq_make_list(
                    _qq_quote(make_symbol('unquote-splicing', src), src),
                    _qq_walk(x.cdr.car, level - 1, src),
                    src)
        # Splicing in element position: x.car itself is (unquote-splicing <e>).
        if is_cons(head) and is_symbol(head.car):
            hname = as_symbol(head.car)
            if hname == 'unquote-splicing':
                if not is_cons(head.cdr) or not is_nil(head.cdr.cdr):
                    raise SchemeSyntaxError(
                        'unquote-splicing requires exactly one argument',
                        head.src if head.src is not None else src)
                e = head.cdr.car
                tail = _qq_walk(x.cdr, level, src)
                if level == 1:
                    return _qq_make_append(expand(e), tail, src)
                spliced = _qq_make_list(
                    _qq_quote(make_symbol('unquote-splicing', src), src),
                    _qq_walk(e, level - 1, src),
                    src)
                return _qq_make_cons(spliced, tail, src)
        # Ordinary cons: (cons walk(car) walk(cdr)).
        return _qq_make_cons(
            _qq_walk(x.car, level, src),
            _qq_walk(x.cdr, level, src),
            src)
    # Vector template: process elements as a list template, then convert.
    if is_vector(x):
        items = as_vector_items(x)
        list_chain = list_from_items(items, default_src)
        list_expr = _qq_walk(list_chain, level, default_src)
        return list_from_items(
            [make_symbol('list->vector', default_src), list_expr], default_src)
    # Atom, NIL, or any other value - emit as literal.
    return _qq_quote(x, default_src)


def _expand_quasiquote(sexpr):
    # (quasiquote <template>)  -- single argument
    if not is_cons(sexpr.cdr) or not is_nil(sexpr.cdr.cdr):
        return _expand_list(sexpr)
    template = sexpr.cdr.car
    return _qq_walk(template, 1, sexpr.src)


# ---- multi-value binding forms (R7RS 4.2.2, 5.3.3) ----
# let-values, let*-values, define-values all desugar to call-with-values.
# Synthesized temp names go through hygiene_gensym so user identifiers
# can never collide with our internal bindings.

def _mv_collect_formals(formals_sexpr):
    """Walk a formals s-expression into (fixed_names, rest_name).  Supports
    lambda-style shapes: proper list, dotted tail, or a single identifier
    for pure-variadic.  Returns (None, None) on shape error so the caller
    can punt to the analyzer for a positioned diagnostic."""
    fixed = []
    if is_symbol(formals_sexpr):
        return (fixed, as_symbol(formals_sexpr))
    cur = formals_sexpr
    while is_cons(cur):
        if not is_symbol(cur.car):
            return (None, None)
        fixed.append(as_symbol(cur.car))
        cur = cur.cdr
    if is_nil(cur):
        return (fixed, None)
    if is_symbol(cur):
        return (fixed, as_symbol(cur))
    return (None, None)


def _mv_thunk(expanded_init, src):
    """Emit (lambda () <init>)."""
    return list_from_items(
        [make_symbol('lambda', src), NIL_VALUE, expanded_init], src)


def _mv_lambda(formals_sexpr, body_items, src):
    """Emit (lambda <formals> <body>...).  formals_sexpr is passed through
    unchanged so dotted tails / pure-variadic shapes are preserved."""
    items = [make_symbol('lambda', src), formals_sexpr]
    i = 0
    while i < len(body_items):
        items.append(body_items[i])
        i = i + 1
    return list_from_items(items, src)


def _mv_cwv(producer, consumer, src):
    """Emit (call-with-values <producer> <consumer>)."""
    return list_from_items(
        [make_symbol('call-with-values', src), producer, consumer], src)


def _mv_car(expr, src):
    return list_from_items([make_symbol('car', src), expr], src)


def _mv_cdr(expr, src):
    return list_from_items([make_symbol('cdr', src), expr], src)


def _mv_nth_ref(tmp_sym, index, src):
    """Build (car (cdr (cdr ... tmp))) that selects element `index`."""
    expr = tmp_sym
    i = 0
    while i < index:
        expr = _mv_cdr(expr, src)
        i = i + 1
    return _mv_car(expr, src)


def _mv_tail_ref(tmp_sym, skip, src):
    """Build (cdr (cdr ... tmp)) skipping the first `skip` elements; used
    to bind the rest parameter in let-values to the tail of the temp list."""
    expr = tmp_sym
    i = 0
    while i < skip:
        expr = _mv_cdr(expr, src)
        i = i + 1
    return expr


def _expand_let_star_values(sexpr):
    # (let*-values ((formals init)...) body...)
    # Nested call-with-values: each init sees the bindings introduced by
    # earlier clauses (sequential scoping).
    if _list_length(sexpr) < 3:
        return _expand_list(sexpr)
    bindings_cons = sexpr.cdr.car
    body_cons = sexpr.cdr.cdr
    if _list_length(bindings_cons) < 0:
        return _expand_list(sexpr)
    src = sexpr.src
    clauses = []
    cur = bindings_cons
    while is_cons(cur):
        b = cur.car
        if _list_length(b) != 2:
            return _expand_list(sexpr)
        clauses.append((b.car, expand(b.cdr.car)))
        cur = cur.cdr
    body_items = []
    cur = _expand_body(body_cons, src)
    while is_cons(cur):
        body_items.append(cur.car)
        cur = cur.cdr
    if not body_items:
        return _expand_list(sexpr)
    # With no clauses, let*-values degenerates to (begin body...).
    if not clauses:
        begin_sym = make_symbol('begin', src)
        items = [begin_sym]
        i = 0
        while i < len(body_items):
            items.append(body_items[i])
            i = i + 1
        return list_from_items(items, src)
    # Build from innermost out.  Single-variable formals use (let ((v e)) ...)
    # directly — eliminates two lambda applications per such clause and gives
    # the evaluator's native let* path a chance to handle tail calls cheaply.
    # Multi-variable (or rest) formals still require call-with-values.

    def _single_var(formals):
        return is_cons(formals) and is_symbol(formals.car) and is_nil(formals.cdr)
    current_items = body_items
    i = len(clauses) - 1
    while i >= 0:
        formals, init = clauses[i]
        if _single_var(formals):
            binding = list_from_items([formals.car, init], src)
            bindings = list_from_items([binding], src)
            items = [make_symbol('let', src), bindings]
            j = 0
            while j < len(current_items):
                items.append(current_items[j])
                j = j + 1
            current_items = [list_from_items(items, src)]
        else:
            consumer = _mv_lambda(formals, current_items, src)
            current_items = [_mv_cwv(_mv_thunk(init, src), consumer, src)]
        i = i - 1
    return current_items[0]


def _expand_let_values(sexpr):
    # (let-values ((formals init)...) body...)
    # Parallel semantics: every init evaluates in the outer env, then all
    # variables bind simultaneously.  Collect each init's values into a
    # temp list via (call-with-values (lambda () init) list), then use a
    # plain let to extract each variable via car/cdr chains.
    if _list_length(sexpr) < 3:
        return _expand_list(sexpr)
    bindings_cons = sexpr.cdr.car
    body_cons = sexpr.cdr.cdr
    if _list_length(bindings_cons) < 0:
        return _expand_list(sexpr)
    src = sexpr.src
    clauses = []
    cur = bindings_cons
    while is_cons(cur):
        b = cur.car
        if _list_length(b) != 2:
            return _expand_list(sexpr)
        clauses.append((b.car, expand(b.cdr.car)))
        cur = cur.cdr
    body_items = []
    cur = _expand_body(body_cons, src)
    while is_cons(cur):
        body_items.append(cur.car)
        cur = cur.cdr
    if not body_items:
        return _expand_list(sexpr)
    # Degenerate: no clauses -> (begin body...).
    if not clauses:
        begin_sym = make_symbol('begin', src)
        items = [begin_sym]
        i = 0
        while i < len(body_items):
            items.append(body_items[i])
            i = i + 1
        return list_from_items(items, src)
    # Outer let: bind a fresh gensym to the value-list for each clause.
    outer_bindings = []
    tmp_syms = []
    i = 0
    while i < len(clauses):
        tmp_name = hygiene_gensym('mv')
        tmp_sym = make_symbol(tmp_name, src)
        tmp_syms.append(tmp_sym)
        producer = _mv_thunk(clauses[i][1], src)
        list_sym = make_symbol('list', src)
        cwv = _mv_cwv(producer, list_sym, src)
        pair = list_from_items([tmp_sym, cwv], src)
        outer_bindings.append(pair)
        i = i + 1
    outer_bindings_cons = list_from_items(outer_bindings, src)
    # Inner let: bind each formal name from the corresponding temp list.
    inner_bindings = []
    i = 0
    while i < len(clauses):
        formals = clauses[i][0]
        _mv = _mv_collect_formals(formals)
        fixed = _mv[0]
        rest = _mv[1]
        if fixed is None:
            return _expand_list(sexpr)
        tmp_sym = tmp_syms[i]
        j = 0
        while j < len(fixed):
            var_sym = make_symbol(fixed[j], src)
            ref = _mv_nth_ref(tmp_sym, j, src)
            inner_bindings.append(list_from_items([var_sym, ref], src))
            j = j + 1
        if rest is not None:
            var_sym = make_symbol(rest, src)
            ref = _mv_tail_ref(tmp_sym, len(fixed), src)
            inner_bindings.append(list_from_items([var_sym, ref], src))
        i = i + 1
    let_sym = make_symbol('let', src)
    if inner_bindings:
        inner_bindings_cons = list_from_items(inner_bindings, src)
    else:
        inner_bindings_cons = NIL_VALUE
    inner_items = [let_sym, inner_bindings_cons]
    j = 0
    while j < len(body_items):
        inner_items.append(body_items[j])
        j = j + 1
    inner_let = list_from_items(inner_items, src)
    return list_from_items([let_sym, outer_bindings_cons, inner_let], src)


def _expand_guard(sexpr):
    # (guard (<var> <clause>...) <body>...)
    # -> (with-exception-handler
    #      (lambda (<var>) (cond <clause>... [else (raise <var>)]))
    #      (lambda () <body>...))
    # The implicit (else (raise var)) is omitted if the user supplied an
    # else clause (otherwise we would unreachably replace it).
    if _list_length(sexpr) < 3:
        return _expand_list(sexpr)
    guard_spec = sexpr.cdr.car
    body_cons = sexpr.cdr.cdr
    if _list_length(guard_spec) < 1:
        return _expand_list(sexpr)
    var_sexpr = guard_spec.car
    if not is_symbol(var_sexpr):
        return _expand_list(sexpr)
    clauses_cons = guard_spec.cdr
    if _list_length(clauses_cons) < 0:
        return _expand_list(sexpr)
    # Walk clauses; each must be (<test> <body>...) or start with 'else'.
    # We don't validate clause bodies here; cond's own analyzer does that
    # after expansion.  We only check whether the last clause is 'else'.
    has_else = False
    cur = clauses_cons
    last = None
    while is_cons(cur):
        last = cur.car
        cur = cur.cdr
    if last is not None and is_cons(last) and is_symbol(last.car) and as_symbol(last.car) == 'else':
        has_else = True

    src = sexpr.src
    # Expand the body into a Python list, with internal-define hoisting.
    body_items = []
    cur = _expand_body(body_cons, src)
    while is_cons(cur):
        body_items.append(cur.car)
        cur = cur.cdr
    if not body_items:
        return _expand_list(sexpr)
    # Build the cond tail (list of expanded clauses).
    cond_items = [make_symbol('cond', src)]
    cur = clauses_cons
    while is_cons(cur):
        # Expand each clause's body forms in place but preserve the
        # clause shape.  _expand_list handles it.
        cond_items.append(_expand_list(cur.car))
        cur = cur.cdr
    if not has_else:
        raise_call = list_from_items(
            [make_symbol('raise', src), var_sexpr], src)
        else_clause = list_from_items(
            [make_symbol('else', src), raise_call], src)
        cond_items.append(else_clause)
    cond_form = list_from_items(cond_items, src)
    # Use %guard-eval: a dedicated guard primitive that avoids call/cc.
    # This allows guard to be properly tail-recursive (O(1) stack space
    # for tail-recursive guard loops) without creating continuation objects.
    # Expansion:
    #   (%guard-eval (lambda (<var>) (cond ...)) (lambda () <body>...))
    handler_params = alloc_cons(var_sexpr, NIL_VALUE, src)
    handler = list_from_items(
        [make_symbol('lambda', src), handler_params, cond_form], src)
    # Thunk: (lambda () <body>...).
    thunk_items = [make_symbol('lambda', src), NIL_VALUE]
    i = 0
    while i < len(body_items):
        thunk_items.append(body_items[i])
        i = i + 1
    thunk = list_from_items(thunk_items, src)
    return list_from_items(
        [make_symbol('%guard-eval', src), handler, thunk], src)


def _expand_define_library(sexpr):
    # (define-library <name> <decl>...) has its own structure.  We deliberately
    # do NOT expand begin / declaration bodies here: that has to happen at
    # process time so the per-library macro scope is on top of _macro_scopes
    # when define-syntax fires.  _process_define_library calls expand() on
    # each form right before evaluating it.  The library name is a datum
    # (list of symbols/integers), not a form to expand.
    return sexpr


def _expand_import(sexpr):
    # Top-level (import <import-set>...) - leave verbatim so the evaluator
    # resolves at runtime.  Import-sets are data, not forms.
    return sexpr


def _expand_parameterize(sexpr):
    # (parameterize ((<param> <val>) ...) body...)
    # -> (%with-parameters (list <param>...) (list <val>...)
    #                      (lambda () body...))
    if _list_length(sexpr) < 3:
        return _expand_list(sexpr)
    bindings_cons = sexpr.cdr.car
    body_cons = sexpr.cdr.cdr
    if _list_length(bindings_cons) < 0:
        return _expand_list(sexpr)
    src = sexpr.src
    params_exprs = []
    values_exprs = []
    cur = bindings_cons
    while is_cons(cur):
        b = cur.car
        if _list_length(b) != 2:
            return _expand_list(sexpr)
        params_exprs.append(expand(b.car))
        values_exprs.append(expand(b.cdr.car))
        cur = cur.cdr
    body_items = []
    cur = _expand_body(body_cons, src)
    while is_cons(cur):
        body_items.append(cur.car)
        cur = cur.cdr
    if not body_items:
        return _expand_list(sexpr)

    list_sym = make_symbol('list', src)
    # (list p1 p2 ...)
    params_list_items = [list_sym]
    i = 0
    while i < len(params_exprs):
        params_list_items.append(params_exprs[i])
        i = i + 1
    params_list_form = list_from_items(params_list_items, src)
    # (list v1 v2 ...)
    values_list_items = [list_sym]
    i = 0
    while i < len(values_exprs):
        values_list_items.append(values_exprs[i])
        i = i + 1
    values_list_form = list_from_items(values_list_items, src)
    # (lambda () body...)
    lambda_items = [make_symbol('lambda', src), NIL_VALUE]
    i = 0
    while i < len(body_items):
        lambda_items.append(body_items[i])
        i = i + 1
    thunk = list_from_items(lambda_items, src)
    return list_from_items(
        [make_symbol('%with-parameters', src),
         params_list_form, values_list_form, thunk], src)


def _expand_define_record_type(sexpr):
    # (define-record-type <name>
    #    (<ctor-name> <ctor-field>...)
    #    <predicate-name>
    #    (<field-name> <accessor-name> [<mutator-name>])...)
    # Desugar to a (begin) of defines using the internal %record primitives.
    if _list_length(sexpr) < 4:
        return _expand_list(sexpr)
    type_name_sexpr = sexpr.cdr.car
    ctor_spec = sexpr.cdr.cdr.car
    pred_sexpr = sexpr.cdr.cdr.cdr.car
    field_specs = sexpr.cdr.cdr.cdr.cdr
    if not is_symbol(type_name_sexpr):
        return _expand_list(sexpr)
    if not is_symbol(pred_sexpr):
        return _expand_list(sexpr)
    if _list_length(ctor_spec) < 1:
        return _expand_list(sexpr)
    ctor_name = ctor_spec.car
    if not is_symbol(ctor_name):
        return _expand_list(sexpr)
    # Walk ctor field list and field specs.
    ctor_fields = []
    cur = ctor_spec.cdr
    while is_cons(cur):
        if not is_symbol(cur.car):
            return _expand_list(sexpr)
        ctor_fields.append(cur.car)
        cur = cur.cdr
    if not is_nil(cur):
        return _expand_list(sexpr)
    # Each field-spec: (field accessor) or (field accessor mutator).
    field_entries = []
    cur = field_specs
    while is_cons(cur):
        spec = cur.car
        slen = _list_length(spec)
        if slen != 2 and slen != 3:
            return _expand_list(sexpr)
        fname = spec.car
        accessor = spec.cdr.car
        if not is_symbol(fname) or not is_symbol(accessor):
            return _expand_list(sexpr)
        if slen == 3:
            mutator = spec.cdr.cdr.car
            if not is_symbol(mutator):
                return _expand_list(sexpr)
            field_entries.append((fname, accessor, mutator))
        else:
            field_entries.append((fname, accessor, None))
        cur = cur.cdr
    if not is_nil(cur):
        return _expand_list(sexpr)

    src = sexpr.src
    type_name = as_symbol(type_name_sexpr)
    # All field names known to the record-type descriptor, in user-declared
    # order (taken from the <field-name> slot of each field spec).
    all_field_names = []
    i = 0
    while i < len(field_entries):
        all_field_names.append(field_entries[i][0])
        i = i + 1

    # Hidden global holding the record type descriptor.  Use a gensym so
    # two define-record-type forms with the same user-visible type name
    # produce distinct hidden descriptors and never collide with user
    # identifiers.
    rt_sym_name = hygiene_gensym('record-type-' + type_name)
    rt_sym = make_symbol(rt_sym_name, src)

    # Helpers for emitting pieces.
    def mk_sym(n):
        return make_symbol(n, src)

    def mk_define(name_sexpr, value_sexpr):
        return list_from_items([mk_sym('define'), name_sexpr, value_sexpr], src)

    def mk_define_proc(name_sexpr, params_sexpr, body_sexpr):
        # Emit (define <name> (lambda <params> <body>)) already in core form;
        # the Expander does not re-walk our output, so (define (f ...) ...)
        # sugar would not be re-expanded here.
        lam = list_from_items(
            [mk_sym('lambda'), params_sexpr, body_sexpr], src)
        return list_from_items([mk_sym('define'), name_sexpr, lam], src)

    def mk_quote(datum):
        return list_from_items([mk_sym('quote'), datum], src)

    def mk_list_of_syms(sym_list):
        # (list 'a 'b 'c) built as a quoted list datum instead: '(a b c).
        return mk_quote(list_from_items(sym_list, src))

    forms = [mk_sym('begin')]

    # %record-type:<name> = (%make-record-type 'name '(f1 f2 ...))
    rt_init = list_from_items(
        [mk_sym('%make-record-type'),
         mk_quote(type_name_sexpr),
         mk_list_of_syms(all_field_names)],
        src)
    forms.append(mk_define(rt_sym, rt_init))

    # (define (ctor ctor-fields...) (%make-record %rt (list ctor-fields...)))
    # When a ctor-field is omitted from the declared field list, its slot
    # should still be filled - R7RS leaves uninitialized slots unspecified;
    # we fill with #<void>.
    ctor_field_names_set = set()
    i = 0
    while i < len(ctor_fields):
        ctor_field_names_set.add(as_symbol(ctor_fields[i]))
        i = i + 1
    # Build the (list ...) argument to %make-record, in declared field order.
    list_items = [mk_sym('list')]
    i = 0
    while i < len(all_field_names):
        fname = as_symbol(all_field_names[i])
        if fname in ctor_field_names_set:
            list_items.append(all_field_names[i])
        else:
            list_items.append(VOID_VALUE)
        i = i + 1
    ctor_body = list_from_items(
        [mk_sym('%make-record'), rt_sym, list_from_items(list_items, src)],
        src)
    params = list_from_items(ctor_fields, src)
    forms.append(mk_define_proc(ctor_name, params, ctor_body))

    # (define (predicate obj) (%record-of-type? obj %rt))
    obj_sym = mk_sym('obj')
    pred_body = list_from_items(
        [mk_sym('%record-of-type?'), obj_sym, rt_sym], src)
    pred_params = alloc_cons(obj_sym, NIL_VALUE, src)
    forms.append(mk_define_proc(pred_sexpr, pred_params, pred_body))

    # For each field: accessor and optional mutator.  Emit each as a
    # call to %make-record-accessor / %make-record-mutator, which produce
    # special RECORD_ACCESSOR / RECORD_MUTATOR values; the Evaluator
    # dispatches them inline so type-error positions are the call site,
    # not the define-record-type form.
    from pyscheme.AST import make_integer
    i = 0
    while i < len(field_entries):
        fname = field_entries[i][0]
        accessor = field_entries[i][1]
        mutator = field_entries[i][2]
        idx_val = make_integer(i, src)
        acc_init = list_from_items(
            [mk_sym('%make-record-accessor'), rt_sym, idx_val,
             mk_quote(accessor)], src)
        forms.append(mk_define(accessor, acc_init))
        if mutator is not None:
            mut_init = list_from_items(
                [mk_sym('%make-record-mutator'), rt_sym, idx_val,
                 mk_quote(mutator)], src)
            forms.append(mk_define(mutator, mut_init))
        i = i + 1

    return list_from_items(forms, src)


def _dv_build_setter(fixed, rest, expanded_expr, src):
    """Build the (call-with-values <producer> <consumer>) form that drives
    define-values's multi-value assignment.  fixed/rest are the user's
    formals already collected; expanded_expr is the producer body, already
    expanded.  Returns one cons form."""
    set_sym = make_symbol('set!', src)
    tmp_fixed = []
    i = 0
    while i < len(fixed):
        tmp_fixed.append(make_symbol(hygiene_gensym('mv-tmp'), src))
        i = i + 1
    tmp_rest = None
    if rest is not None:
        tmp_rest = make_symbol(hygiene_gensym('mv-tmp-rest'), src)
    body_items = []
    i = 0
    while i < len(fixed):
        body_items.append(list_from_items(
            [set_sym, make_symbol(fixed[i], src), tmp_fixed[i]], src))
        i = i + 1
    if rest is not None:
        body_items.append(list_from_items(
            [set_sym, make_symbol(rest, src), tmp_rest], src))
    if not body_items:
        # (define-values () <expr>): no set!s, but the consumer lambda still
        # needs a body expression.  Yield the unspecified value.
        body_items.append(VOID_VALUE)
    if tmp_rest is None:
        consumer_formals = list_from_items(tmp_fixed, src)
    elif not tmp_fixed:
        consumer_formals = tmp_rest
    else:
        consumer_formals = tmp_rest
        j = len(tmp_fixed) - 1
        while j >= 0:
            consumer_formals = alloc_cons(tmp_fixed[j], consumer_formals, src)
            j = j - 1
    consumer = _mv_lambda(consumer_formals, body_items, src)
    producer = _mv_thunk(expanded_expr, src)
    return _mv_cwv(producer, consumer, src)


def _expand_define_values(sexpr):
    # (define-values <formals> <expr>)
    # Top-level desugaring: emit a begin that pre-defines each user-visible
    # binding to VOID, then runs the call-with-values setter.  Inside body
    # context, _expand_body intercepts before this handler runs and hoists
    # the bindings into a letrec* directly.
    if _list_length(sexpr) != 3:
        return _expand_list(sexpr)
    formals_sexpr = sexpr.cdr.car
    expr = sexpr.cdr.cdr.car
    _mv = _mv_collect_formals(formals_sexpr)
    fixed = _mv[0]
    rest = _mv[1]
    if fixed is None:
        return _expand_list(sexpr)
    src = sexpr.src
    define_sym = make_symbol('define', src)
    items = [make_symbol('begin', src)]
    i = 0
    while i < len(fixed):
        items.append(list_from_items(
            [define_sym, make_symbol(fixed[i], src), VOID_VALUE], src))
        i = i + 1
    if rest is not None:
        items.append(list_from_items(
            [define_sym, make_symbol(rest, src), VOID_VALUE], src))
    items.append(_dv_build_setter(fixed, rest, expand(expr), src))
    return list_from_items(items, src)


def _expand_cond_expand(sexpr):
    # (cond-expand <clause>...)
    # Each clause: (<feature-req> <body>...)  or  (else <body>...)
    # Pick the first clause whose req matches; emit its body as
    # (begin <expanded body>...).  If nothing matches, punt so the
    # analyzer can complain with position.
    if not is_cons(sexpr.cdr):
        return _expand_list(sexpr)
    src = sexpr.src
    cur = sexpr.cdr
    while is_cons(cur):
        clause = cur.car
        if not is_cons(clause):
            return _expand_list(sexpr)
        req = clause.car
        body = clause.cdr
        if _feature_req_matches(req):
            # Emit (begin <expanded body>...).
            if is_nil(body):
                return VOID_VALUE
            begin_sym = make_symbol('begin', src)
            items = [begin_sym]
            bcur = body
            while is_cons(bcur):
                items.append(expand(bcur.car))
                bcur = bcur.cdr
            return list_from_items(items, src)
        cur = cur.cdr
    # No clause matched.  Punt for a positioned diagnostic.
    return _expand_list(sexpr)


_SUGAR_HANDLERS = {
    'define':             _expand_define,
    'lambda':             _expand_lambda,
    'let':                _expand_let,
    'let*':               _expand_let_star,
    'letrec':             _expand_letrec,
    'letrec*':            _expand_letrec_star,
    'when':               _expand_when,
    'unless':             _expand_unless,
    'case-lambda':        _expand_case_lambda,
    'if':                 _expand_if,
    'quote':              _expand_quote,
    'do':                 _expand_do,
    'include':            _expand_include,
    'include-ci':         _expand_include_ci,
    'cond-expand':        _expand_cond_expand,
    'quasiquote':         _expand_quasiquote,
    'let-values':         _expand_let_values,
    'let*-values':        _expand_let_star_values,
    'define-values':      _expand_define_values,
    'define-record-type': _expand_define_record_type,
    'parameterize':       _expand_parameterize,
    'guard':              _expand_guard,
    'define-library':     _expand_define_library,
    'import':             _expand_import,
}


# -------- Self-test --------

if __name__ == '__main__':
    from pyscheme.Parser import parse_one, _strip_src
    from pyscheme.AST import INTEGER, BOOLEAN

    samples = [
        ('(define (f x) x)',
         [(SYMBOL, 'define'),
          (SYMBOL, 'f'),
          [(SYMBOL, 'lambda'),
          [(SYMBOL, 'x')],
          (SYMBOL, 'x')]]),
        ('(define (g x y) x y)',
            [(SYMBOL, 'define'),
             (SYMBOL, 'g'),
                [(SYMBOL, 'lambda'),
                 [(SYMBOL, 'x'), (SYMBOL, 'y')],
                 (SYMBOL, 'x'),
                 (SYMBOL, 'y')]]),
        ('(define (thunk) 42)',
            [(SYMBOL, 'define'),
             (SYMBOL, 'thunk'),
                [(SYMBOL, 'lambda'), [], (INTEGER, 42)]]),

        ('(if #t 1)',
            [(SYMBOL, 'if'),
             (BOOLEAN, True),
                (INTEGER, 1),
                (VOID,)]),
        ('(if #t 1 2)',
            [(SYMBOL, 'if'),
             (BOOLEAN, True),
                (INTEGER, 1),
                (INTEGER, 2)]),

        # plain define passes through
        ('(define x 5)',
            [(SYMBOL, 'define'),
             (SYMBOL, 'x'),
                (INTEGER, 5)]),

        # nested sugar
        ('(define (g) (if #t 1))',
            [(SYMBOL, 'define'),
             (SYMBOL, 'g'),
                [(SYMBOL, 'lambda'), [],
                 [(SYMBOL, 'if'),
                 (BOOLEAN, True),
                    (INTEGER, 1),
                    (VOID,)]]]),

        # quoted data preserved verbatim
        ("'(define (f x) x)",
            [(SYMBOL, 'quote'),
             [(SYMBOL, 'define'),
                [(SYMBOL, 'f'), (SYMBOL, 'x')],
                (SYMBOL, 'x')]]),

        # variadic define: (define (f . rest) ...) -> (define f (lambda rest ...))
        ('(define (f . rest) rest)',
            [(SYMBOL, 'define'),
             (SYMBOL, 'f'),
                [(SYMBOL, 'lambda'),
                 (SYMBOL, 'rest'),
                 (SYMBOL, 'rest')]]),

        # fixed + rest: (define (f a b . rest) ...) -> (define f (lambda (a b . rest) ...))
        ('(define (f a b . rest) rest)',
            [(SYMBOL, 'define'),
             (SYMBOL, 'f'),
                [(SYMBOL, 'lambda'),
                 ([(SYMBOL, 'a'), (SYMBOL, 'b')], (SYMBOL, 'rest')),
                 (SYMBOL, 'rest')]]),
    ]

    n_pass = 0
    n_fail = 0
    i = 0
    while i < len(samples):
        source = samples[i][0]
        expected = samples[i][1]
        got = expand(parse_one(source))
        stripped = _strip_src(got)
        if stripped == expected:
            print("[ OK ] %r" % source)
            n_pass = n_pass + 1
        else:
            print("[FAIL] %r" % source)
            print("        expected: %r" % (expected,))
            print("        got:      %r" % (stripped,))
            n_fail = n_fail + 1
        i = i + 1

    print()
    print("%d passed, %d failed" % (n_pass, n_fail))
