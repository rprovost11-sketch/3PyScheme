"""R7RS syntax-rules pattern matcher and template instantiator (R7RS 4.3).

Called from the Expander when a form's head is a bound syntax transformer.
Public API:

    parse_syntax_rules(tail, def_env, name)
        Parse a (syntax-rules [ellipsis] (literals) rules...) body into
        a SyntaxTransformer.

    apply_syntax_transformer(t, form)
        Run transformer t against form; return the expanded s-expression.
        Raises SchemeSyntaxError if no pattern matches.

Hygiene model: alpha-renaming at binding sites.

  free_id_map   - dict name->gensym_alias built at parse time.  Every free
                  template identifier that was bound in the definition
                  environment gets a gensym alias; that alias is bound in
                  the runtime env to the def-time value.  Template expansion
                  emits the alias so the reference resolves at the def site.

  intro_names   - set of template identifiers not found in the definition
                  environment (introduced binding sites like `t`, `tmp`).
                  Emitted as-is; the Expander alpha-renames them when it
                  processes the expanded form's binding sites.

Literal matching uses plain string equality: user-site identifiers that
share a literal's name but were introduced by a binding form arrive with
a gensym suffix (e.g., `foo#3`) so they fail the plain-name comparison.
"""

from pyscheme.AST import (
    is_cons, is_nil, is_symbol, is_string, is_integer, is_real, is_boolean,
    is_character, is_vector,
    as_symbol, as_string, as_integer, as_real, as_boolean, as_character,
    as_vector_items,
    alloc_cons, make_symbol, make_vector, list_from_items, src_of, eqv_atom,
    make_syntax_transformer, is_syntax_transformer,
    is_closure, is_primitive,
    paint_mark, strip_marks, gensym_display_name,
    NIL_VALUE, SYMBOL, GENSYM_PREFIX,
)


# Syntactic keywords recognized directly by the Evaluator / Expander.
# These must not be renamed when they appear in macro templates.
_SYNTACTIC_KEYWORDS = {
    'if', 'lambda', 'begin', 'define', 'set!', 'quote',
    'let', 'let*', 'letrec', 'letrec*',
    'and', 'or', 'case', 'cond', 'when', 'unless', 'do', 'guard',
    'parameterize', 'define-syntax', 'let-syntax', 'letrec-syntax',
    'define-record-type', 'syntax-rules', 'define-library',
    'import', 'export', 'case-lambda',
    'quasiquote', 'unquote', 'unquote-splicing',
    'define-values', 'let-values', 'let*-values',
    'include', 'include-ci', 'cond-expand',
    'delay', 'delay-force',
    'else', '=>', 'library',
    '_', '...',
}


# Module-level counter for hygiene gensyms.  Shared by all transformers.
_GENSYM_COUNTER = 0

# Per-expansion hygiene MARK (A1/A3 prototype).  apply_syntax_transformer mints a
# fresh mark and sets _current_mark around its instantiation; _instantiate paints
# template-introduced identifiers with it (see AST.paint_mark).  Instantiation is
# not re-entrant within a single application (nested macros expand later, via the
# Expander, each with its own mark), so a module global suffices; it is
# save/restored defensively.  None => no painting (direct _instantiate calls).
_MARK_COUNTER = 0
_current_mark = None


# ── Expander invariant auditing (#4a) ───────────────────────────────────────
# Off by default.  When PYSCHEME_EXPANDER_AUDIT is set, the template
# instantiator checks hygiene/expansion invariants and reports violations to
# stderr *without changing expansion behavior*, so a full test run becomes a
# white-box invariant sweep complementing the black-box cross-port harness.
# Mirrored in cppScheme2 (CPPSCHEME2_EXPANDER_AUDIT).  See
# scheme-tests/cross-port-tests/README.md.
import os as _os
import sys as _sys

_EXPANDER_AUDIT = bool(_os.environ.get('PYSCHEME_EXPANDER_AUDIT'))
_audit_violations = []


def expander_audit_enabled():
    return _EXPANDER_AUDIT


def expander_audit_violations():
    """The list of (kind, message) invariant violations seen so far."""
    return list(_audit_violations)


def _audit(kind, msg):
    _audit_violations.append((kind, msg))
    _sys.stderr.write('[EXPANDER-AUDIT] %s: %s\n' % (kind, msg))


def hygiene_gensym(base, force=False):
    """Generate a fresh symbol name unlikely to collide with user code.
    Uses a non-printable marker byte (AST.GENSYM_PREFIX) so the display paths
    can strip it.  If base is already a gensym (starts with the prefix), return
    it unchanged to prevent double-gensymming when the Expander processes macro
    output -- UNLESS force=True, which always produces a fresh gensym from the
    base's display name.  force is used for per-application template binders that
    may arrive already gensym'd (a binder name threaded in from an enclosing
    binding); they must be freshened per application so they don't capture a
    same-named use-site reference (A1f).  The inverse decoder is
    AST.gensym_display_name."""
    global _GENSYM_COUNTER
    if force:
        base = gensym_display_name(base)
        _GENSYM_COUNTER = _GENSYM_COUNTER + 1
        return GENSYM_PREFIX + base + '.' + str(_GENSYM_COUNTER)
    if base.startswith(GENSYM_PREFIX):
        return base
    # Strip any hygiene marks so the mark byte is never embedded in a gensym
    # name (which strip_marks would later mis-cut).  Callers that need to tell
    # two same-base marked names apart key on the full marked name in their
    # rename table; the gensym value just needs to be unique and mark-free.
    base = strip_marks(base)
    _GENSYM_COUNTER = _GENSYM_COUNTER + 1
    return GENSYM_PREFIX + base + '.' + str(_GENSYM_COUNTER)


# ── SyntaxMatch: bindings produced by pattern matching ─────────────────────

class _SyntaxMatch:
    """Bindings from one pattern-match attempt.
       scalars   - dict: name -> Scheme value (depth 0 pattern vars)
       ellipsis  - dict: name -> Python list of values (depth >= 1 pvars)
       ell_depth - dict: name -> int, the ellipsis depth"""

    def __init__(self):
        self.scalars = {}
        self.ellipsis = {}
        self.ell_depth = {}


def _is_ellipsis(form, ellipsis_sym):
    return is_symbol(form) and as_symbol(form) == ellipsis_sym


# ── Pattern-variable collectors ────────────────────────────────────────────

def collect_pvars(pat, literals, ellipsis_sym, out):
    """Add to the set `out` every pattern variable in `pat`.  Non-pvars:
    literals, the underscore, the ellipsis symbol."""
    if is_symbol(pat):
        s = as_symbol(pat)
        if s == '_' or s == ellipsis_sym:
            return
        i = 0
        while i < len(literals):
            if s == literals[i]:
                return
            i = i + 1
        out.add(s)
        return
    if is_cons(pat):
        collect_pvars(pat.car, literals, ellipsis_sym, out)
        collect_pvars(pat.cdr, literals, ellipsis_sym, out)
    if is_vector(pat):
        for item in as_vector_items(pat):
            collect_pvars(item, literals, ellipsis_sym, out)


def collect_pvars_with_depth(pat, literals, ellipsis_sym, out, depth):
    """Record each pvar's ellipsis depth in `out` (a dict name -> int)."""
    if is_symbol(pat):
        s = as_symbol(pat)
        if s == '_' or s == ellipsis_sym:
            return
        i = 0
        while i < len(literals):
            if s == literals[i]:
                return
            i = i + 1
        out[s] = depth
        return
    if is_cons(pat):
        cur = pat
        while is_cons(cur):
            elem = cur.car
            rest = cur.cdr
            has_ell = is_cons(rest) and _is_ellipsis(rest.car, ellipsis_sym)
            if has_ell:
                collect_pvars_with_depth(
                    elem, literals, ellipsis_sym, out, depth + 1)
                cur = rest.cdr
            else:
                collect_pvars_with_depth(
                    elem, literals, ellipsis_sym, out, depth)
                cur = rest
        if not is_nil(cur):
            collect_pvars_with_depth(cur, literals, ellipsis_sym, out, depth)
    if is_vector(pat):
        items = as_vector_items(pat)
        i = 0
        n = len(items)
        while i < n:
            has_ell = (i + 1 < n
                       and is_symbol(items[i + 1])
                       and as_symbol(items[i + 1]) == ellipsis_sym)
            if has_ell:
                collect_pvars_with_depth(
                    items[i], literals, ellipsis_sym, out, depth + 1)
                i = i + 2
            else:
                collect_pvars_with_depth(
                    items[i], literals, ellipsis_sym, out, depth)
                i = i + 1


# ── Free-identifier collector ──────────────────────────────────────────────

def collect_free_ids(tmpl, pvars, literals, ellipsis_sym, out):
    """Collect template identifiers that are not pvars / literals / ellipsis /
    underscore / syntactic keywords; skips inside (quote ...).  `out` is a set."""
    if is_symbol(tmpl):
        s = as_symbol(tmpl)
        if s == ellipsis_sym or s == '_':
            return
        if s in pvars:
            return
        i = 0
        while i < len(literals):
            if s == literals[i]:
                return
            i = i + 1
        if s in _SYNTACTIC_KEYWORDS:
            return
        out.add(s)
        return
    if is_cons(tmpl):
        if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'quote':
            return
        collect_free_ids(tmpl.car, pvars, literals, ellipsis_sym, out)
        collect_free_ids(tmpl.cdr, pvars, literals, ellipsis_sym, out)
    if is_vector(tmpl):
        for item in as_vector_items(tmpl):
            collect_free_ids(item, pvars, literals, ellipsis_sym, out)


# ── Binding forms: shared roster for the two hygiene walkers ───────────────
# Two walkers must know which special-form heads introduce *bindings*, and they
# must not silently drift apart:
#   * collect_binding_intros (below) walks RAW syntax-rules TEMPLATES, adding
#     binder-position names to the per-expansion gensym set.
#   * Expander._rename_refs_in_form walks ALREADY-EXPANDED forms, masking
#     re-bound names while it renames free references.
# They deliberately cover DIFFERENT subsets because they see different input
# languages: a template can contain `define` (internal defines are lowered to
# letrec* before the expanded-form walker ever runs, so it never sees one),
# while case-lambda / let-syntax / letrec-syntax survive into expanded code (so
# the expanded-form walker masks them).  Both share the binder extractors
# formals_bound_names / let_binding_names.  Adding a binding form means deciding
# which walker(s) it reaches and updating the matching roster below; this is the
# single place that records the full set.
TEMPLATE_BINDING_HEADS = frozenset([
    'lambda', 'let', 'let*', 'letrec', 'letrec*', 'define',
])
EXPANDED_BINDING_HEADS = frozenset([
    'lambda', 'let', 'let*', 'letrec', 'letrec*',
    'case-lambda', 'let-syntax', 'letrec-syntax',
])
BINDING_FORM_HEADS = TEMPLATE_BINDING_HEADS | EXPANDED_BINDING_HEADS


# ── Binding-site intro-name collector ─────────────────────────────────────

def collect_binding_intros(tmpl, pvars, out):
    """Collect non-pvar template symbols that appear in binding positions
    (lambda formals, let/letrec binding names, named-let name, define name).
    These are the intro_names that must be gensymmed per application so they
    don't accidentally capture same-named use-site variables.

    Handles TEMPLATE_BINDING_HEADS (this walker's roster -- see that set's
    comment for why it differs from _rename_refs_in_form's set)."""
    if not is_cons(tmpl):
        return
    if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'quote':
        return
    if is_symbol(tmpl.car):
        hname = as_symbol(tmpl.car)
        if hname == 'lambda':
            if is_cons(tmpl.cdr):
                _cbi_formals(tmpl.cdr.car, pvars, out)
                collect_binding_intros(tmpl.cdr.cdr, pvars, out)
            return
        if hname in ('let', 'let*', 'letrec', 'letrec*'):
            if is_cons(tmpl.cdr):
                second = tmpl.cdr.car
                body_start = tmpl.cdr.cdr
                bindings = second
                if hname == 'let' and is_symbol(second):
                    name = as_symbol(second)
                    if name not in pvars:
                        out.add(name)
                    if is_cons(body_start):
                        bindings = body_start.car
                        body_start = body_start.cdr
                    else:
                        return
                _cbi_let_bindings(bindings, pvars, out)
                collect_binding_intros(body_start, pvars, out)
            return
        if hname == 'define':
            if is_cons(tmpl.cdr):
                nameform = tmpl.cdr.car
                if is_symbol(nameform):
                    n = as_symbol(nameform)
                    if n not in pvars:
                        out.add(n)
                elif is_cons(nameform) and is_symbol(nameform.car):
                    n = as_symbol(nameform.car)
                    if n not in pvars:
                        out.add(n)
                    _cbi_formals(nameform.cdr, pvars, out)
                collect_binding_intros(tmpl.cdr.cdr, pvars, out)
            return
    collect_binding_intros(tmpl.car, pvars, out)
    collect_binding_intros(tmpl.cdr, pvars, out)


def formals_bound_names(formals):
    """Names bound by a lambda formals list (proper or dotted/rest arg), in
    order.  The single binder-extractor for formals, shared by both hygiene
    walkers (collect_binding_intros here and Expander._rename_refs_in_form)."""
    names = []
    cur = formals
    while is_cons(cur):
        if is_symbol(cur.car):
            names.append(as_symbol(cur.car))
        cur = cur.cdr
    if is_symbol(cur):
        names.append(as_symbol(cur))
    return names


def let_binding_names(bindings):
    """Names bound by a let/letrec binding list ((name init)...), in order.
    The single binder-extractor for let-bindings, shared by both hygiene
    walkers (collect_binding_intros here and Expander._rename_refs_in_form)."""
    names = []
    cur = bindings
    while is_cons(cur):
        b = cur.car
        if is_cons(b) and is_symbol(b.car):
            names.append(as_symbol(b.car))
        cur = cur.cdr
    return names


def _cbi_formals(formals, pvars, out):
    """Collect binding-intro names from a lambda formals list."""
    for n in formals_bound_names(formals):
        if n not in pvars:
            out.add(n)


def _cbi_let_bindings(bindings, pvars, out):
    """Collect binding-intro names from a let/letrec binding list."""
    for n in let_binding_names(bindings):
        if n not in pvars:
            out.add(n)


def collect_self_call_operand_intros(tmpl, macro_name, pvars, out):
    """Collect non-pvar symbols passed as a direct operand to a recursive
    self-invocation of the macro anywhere in a template.

    Such an operand can land in a binding position in another rule of the same
    macro -- miniKanren's `run` clause 1 passes an introduced `q` to a
    recursive `run` call that clause 2 binds with `let` -- so it must be
    gensymmed per application or it captures, or is captured by, a same-named
    use-site identifier.  This is deliberately narrow: an introduced free
    *reference* that is NOT threaded through a self-call -- e.g. the `y` in
    `(define-syntax m (syntax-rules () ((_ n) (+ n y))))` -- is left as-is so
    it still resolves at the use site (referential transparency).

    NOTE: a heuristic, not full hygiene.  It does not catch a binder threaded
    through *mutual* macro recursion; the principled fix is mark-based
    (sets-of-scopes) resolution."""
    if not is_cons(tmpl):
        if is_vector(tmpl):
            for item in as_vector_items(tmpl):
                collect_self_call_operand_intros(item, macro_name, pvars, out)
        return
    if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'quote':
        return
    if is_symbol(tmpl.car) and as_symbol(tmpl.car) == macro_name:
        # direct operands of a recursive self-call
        cur = tmpl.cdr
        while is_cons(cur):
            if is_symbol(cur.car):
                s = as_symbol(cur.car)
                if s not in pvars:
                    out.add(s)
            cur = cur.cdr
    # recurse everywhere to find nested self-calls
    collect_self_call_operand_intros(tmpl.car, macro_name, pvars, out)
    cur = tmpl.cdr
    while is_cons(cur):
        collect_self_call_operand_intros(cur.car, macro_name, pvars, out)
        cur = cur.cdr


# ── Pattern matching ────────────────────────────────────────────────────────

def _list_length_approx(lst):
    n = 0
    cur = lst
    while is_cons(cur):
        n = n + 1
        cur = cur.cdr
    return n


def _datum_equal(a, b):
    """Structural equality for literal datums appearing in patterns."""
    if eqv_atom(a, b):
        return True
    if is_string(a) and is_string(b):
        return as_string(a) == as_string(b)
    if is_cons(a) and is_cons(b):
        if not _datum_equal(a.car, b.car):
            return False
        return _datum_equal(a.cdr, b.cdr)
    if is_nil(a) and is_nil(b):
        return True
    if is_vector(a) and is_vector(b):
        ia = as_vector_items(a)
        ib = as_vector_items(b)
        if len(ia) != len(ib):
            return False
        i = 0
        while i < len(ia):
            if not _datum_equal(ia[i], ib[i]):
                return False
            i = i + 1
        return True
    return False


def _match_pattern(pat, form, literals, ellipsis_sym, out):
    """Match one pattern node against one form node.  Returns True and fills
    `out`, or False.  Literal matching uses plain string equality: a
    use-site identifier that has been alpha-renamed will have a gensym name
    and will not match the literal's plain name.

    Literals are checked before the `_` wildcard so that `_` declared in the
    literals list matches literally (R7RS 4.3.2 literal-priority rule)."""
    if is_symbol(pat):
        s = as_symbol(pat)
        i = 0
        while i < len(literals):
            if s == literals[i]:
                if not (is_symbol(form) and as_symbol(form) == s):
                    return False
                return True
            i = i + 1
        if s == '_':
            return True
        out.scalars[s] = form
        return True
    if is_cons(pat):
        return _match_list_pattern(pat, form, literals, ellipsis_sym, out)
    if is_vector(pat):
        if not is_vector(form):
            return False
        return _match_vector_pattern(as_vector_items(pat), as_vector_items(form),
                                     literals, ellipsis_sym, out)
    if is_nil(pat):
        return is_nil(form)
    return _datum_equal(pat, form)


def _match_list_pattern(pat_list, form_list, literals, ellipsis_sym, out):
    """Match a list-shaped pattern against a list-shaped form."""
    while is_cons(pat_list):
        pat_elem = pat_list.car
        pat_rest = pat_list.cdr
        has_ell = (is_cons(pat_rest)
                   and _is_ellipsis(pat_rest.car, ellipsis_sym))
        if has_ell:
            suffix_pat = pat_rest.cdr
            suffix_need = _list_length_approx(suffix_pat)
            form_vec = []
            form_tail = form_list
            while is_cons(form_tail):
                form_vec.append(form_tail.car)
                form_tail = form_tail.cdr
            total = len(form_vec)
            if total < suffix_need:
                return False
            n_ellipsis = total - suffix_need
            pvar_depths = {}
            collect_pvars_with_depth(
                pat_elem, literals, ellipsis_sym, pvar_depths, 0)
            for pv in pvar_depths:
                out.ellipsis[pv] = []
                out.ell_depth[pv] = pvar_depths[pv] + 1
            i = 0
            while i < n_ellipsis:
                sub = _SyntaxMatch()
                if not _match_pattern(pat_elem, form_vec[i], literals,
                                      ellipsis_sym, sub):
                    return False
                for k in sub.scalars:
                    out.ellipsis[k].append(sub.scalars[k])
                for k in sub.ellipsis:
                    out.ellipsis[k].append(sub.ellipsis[k])
                i = i + 1
            # Preserve any improper (dotted) tail so a trailing pattern var
            # like `rest` in (a ... . rest) binds to it (R7RS 4.3.2).
            suffix_form = form_tail
            j = total - 1
            while j >= n_ellipsis:
                suffix_form = alloc_cons(form_vec[j], suffix_form)
                j = j - 1
            return _match_list_pattern(suffix_pat, suffix_form, literals,
                                       ellipsis_sym, out)
        if not is_cons(form_list):
            return False
        if not _match_pattern(pat_elem, form_list.car, literals,
                              ellipsis_sym, out):
            return False
        pat_list = pat_rest
        form_list = form_list.cdr
    if is_nil(pat_list):
        return is_nil(form_list)
    if is_symbol(pat_list):
        s = as_symbol(pat_list)
        i = 0
        while i < len(literals):
            if s == literals[i]:
                return False
            i = i + 1
        out.scalars[s] = form_list
        return True
    return False


def _match_vector_pattern(pat_items, form_items, literals, ellipsis_sym, out):
    """Match a vector pattern (Python list) against a vector form (Python list)."""
    i = 0
    j = 0
    n_pat = len(pat_items)
    n_form = len(form_items)
    while i < n_pat:
        pat_elem = pat_items[i]
        has_ell = (i + 1 < n_pat
                   and _is_ellipsis(pat_items[i + 1], ellipsis_sym))
        if has_ell:
            suffix_count = n_pat - (i + 2)
            available = n_form - j
            if available < suffix_count:
                return False
            n_ellipsis = available - suffix_count
            pvar_depths = {}
            collect_pvars_with_depth(
                pat_elem, literals, ellipsis_sym, pvar_depths, 0)
            for pv in pvar_depths:
                out.ellipsis[pv] = []
                out.ell_depth[pv] = pvar_depths[pv] + 1
            k = 0
            while k < n_ellipsis:
                sub = _SyntaxMatch()
                if not _match_pattern(pat_elem, form_items[j + k], literals,
                                      ellipsis_sym, sub):
                    return False
                for key in sub.scalars:
                    out.ellipsis[key].append(sub.scalars[key])
                for key in sub.ellipsis:
                    out.ellipsis[key].append(sub.ellipsis[key])
                k = k + 1
            j = j + n_ellipsis
            i = i + 2
            continue
        if j >= n_form:
            return False
        if not _match_pattern(pat_elem, form_items[j], literals,
                              ellipsis_sym, out):
            return False
        i = i + 1
        j = j + 1
    return j == n_form


# ── Template instantiation ──────────────────────────────────────────────────

def _collect_ell_refs(tmpl, match, out):
    """Find all ellipsis-bound pvars referenced in a template sub-element."""
    if is_symbol(tmpl):
        s = as_symbol(tmpl)
        if s in match.ellipsis:
            out.append(s)
        return
    if is_cons(tmpl):
        _collect_ell_refs(tmpl.car, match, out)
        _collect_ell_refs(tmpl.cdr, match, out)
    if is_vector(tmpl):
        for item in as_vector_items(tmpl):
            _collect_ell_refs(item, match, out)


def _instantiate(tmpl, match, ellipsis_sym, use_src, free_id_map):
    """Expand a template sub-expression against match bindings.

    free_id_map - per-transformer map: free_id -> gensym_alias (bound at def time)

    Template symbols not in match or free_id_map are emitted as-is: binding-site
    symbols (like `t` in `(let ((t e)) ...)`) are renamed by the Expander when it
    processes the expanded form; call-position symbols (like recursive macro refs)
    resolve in the use-site environment as expected.

    use_src is carried to synthesized cons cells."""
    global _current_mark
    if is_symbol(tmpl):
        s = as_symbol(tmpl)
        if s in match.scalars:
            return match.scalars[s]
        if s in free_id_map:
            return make_symbol(free_id_map[s], src_of(tmpl))
        # A template-introduced identifier (not a pattern var, not a def-site
        # free ref).  Paint it with this expansion's mark so two same-named
        # identifiers from different expansion sites stay distinct for literal
        # matching / bound-identifier=? (A3).  Syntactic keywords are left bare
        # so they stay recognizable; the mark resolves away (strip) for variable
        # references and is removed wholesale before analysis.
        if _current_mark is None or s in _SYNTACTIC_KEYWORDS:
            return tmpl
        return make_symbol(paint_mark(s, _current_mark), src_of(tmpl))
    if is_cons(tmpl):
        # R7RS §4.3.2 escape: (ellipsis inner) disables ellipsis inside inner.
        # This takes priority over quote so '(... ...) in a template yields '...
        if (_is_ellipsis(tmpl.car, ellipsis_sym)
                and is_cons(tmpl.cdr) and is_nil(tmpl.cdr.cdr)):
            return _instantiate(tmpl.cdr.car, match,
                                '\x00no-ellipsis\x00', use_src, free_id_map)
        # Inside (quote ...) introduced identifiers are literal data, not code:
        # suppress painting so the quoted datum stays mark-free (and the strip
        # pass can skip quoted data wholesale, never traversing deep/circular
        # quoted lists).  Pattern-variable substitutions are unaffected.
        if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'quote':
            saved_q = _current_mark
            _current_mark = None
            try:
                return _instantiate_list(tmpl, match, ellipsis_sym, use_src,
                                         free_id_map)
            finally:
                _current_mark = saved_q
        if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'syntax-error':
            _raise_syntax_error(
                tmpl.cdr, match, ellipsis_sym, use_src, free_id_map)
        return _instantiate_list(tmpl, match, ellipsis_sym, use_src, free_id_map)
    if is_vector(tmpl):
        return _instantiate_vector(as_vector_items(tmpl), match,
                                   ellipsis_sym, use_src, free_id_map)
    return tmpl


def _raise_syntax_error(args_tail, match, ellipsis_sym, use_src, free_id_map):
    from pyscheme.Parser import SchemeSyntaxError
    from pyscheme.PrettyPrinter import pretty_print
    args = []
    cur = args_tail
    while is_cons(cur):
        args.append(_instantiate(cur.car, match,
                    ellipsis_sym, use_src, free_id_map))
        cur = cur.cdr
    if args and is_string(args[0]):
        msg = as_string(args[0])
        datums = []
        _di = 1
        while _di < len(args):
            datums.append(args[_di])
            _di = _di + 1
    else:
        msg = 'syntax-error'
        datums = args
    if datums:
        parts = []
        i = 0
        while i < len(datums):
            parts.append(pretty_print(datums[i]))
            i = i + 1
        msg = msg + ': ' + ' '.join(parts)
    raise SchemeSyntaxError(msg, use_src)


def _expand_ellipsis_run(elem, match, num_ell, ellipsis_sym, use_src,
                         free_id_map, output):
    """Expand a subtemplate `elem` followed by num_ell (>= 1) ellipses, appending
    to `output`.  num_ell == 1 is the ordinary case; num_ell >= 2 flattens that
    many nested levels, e.g. (x ... ...) collapses ((1 2) (3) (4 5 6)) to
    1 2 3 4 5 6 (R7RS 4.3.2)."""
    ell_syms = []
    _collect_ell_refs(elem, match, ell_syms)
    if not ell_syms:
        return
    count = len(match.ellipsis[ell_syms[0]])
    if _EXPANDER_AUDIT:
        # Invariant: every ellipsis var combined in one run has the SAME match
        # count (the loop indexes them all by k).  A violation is exactly the
        # F1 bug -- it makes a later, shorter var get indexed out of bounds.
        for sv in ell_syms:
            n = len(match.ellipsis[sv])
            if n != count:
                _audit('ellipsis-length-mismatch',
                       'run keyed on %r (len %d) but %r has len %d'
                       % (gensym_display_name(ell_syms[0]), count,
                          gensym_display_name(sv), n))
            d = match.ell_depth.get(sv, 0)
            if d < 1:
                _audit('ellipsis-depth',
                       '%r collected as an ellipsis ref but has depth %d'
                       % (gensym_display_name(sv), d))
    k = 0
    while k < count:
        sub = _SyntaxMatch()
        for key in match.scalars:
            sub.scalars[key] = match.scalars[key]
        for key in match.ellipsis:
            sub.ellipsis[key] = match.ellipsis[key]
            sub.ell_depth[key] = match.ell_depth.get(key, 0)
        j = 0
        while j < len(ell_syms):
            sv = ell_syms[j]
            d = match.ell_depth[sv]
            peeled = match.ellipsis[sv][k]
            if d == 1:
                sub.scalars[sv] = peeled
                if sv in sub.ellipsis:
                    del sub.ellipsis[sv]
                if sv in sub.ell_depth:
                    del sub.ell_depth[sv]
            else:
                sub.ellipsis[sv] = peeled
                sub.ell_depth[sv] = d - 1
            j = j + 1
        if num_ell == 1:
            output.append(_instantiate(
                elem, sub, ellipsis_sym, use_src, free_id_map))
        else:
            _expand_ellipsis_run(elem, sub, num_ell - 1, ellipsis_sym, use_src,
                                 free_id_map, output)
        k = k + 1


def _instantiate_vector(tmpl_items, match, ellipsis_sym, use_src, free_id_map):
    output = []
    i = 0
    n = len(tmpl_items)
    while i < n:
        elem = tmpl_items[i]
        has_ell = (i + 1 < n
                   and _is_ellipsis(tmpl_items[i + 1], ellipsis_sym))
        if has_ell:
            # Count the run of consecutive ellipses (x ... ... flattens levels).
            num_ell = 0
            j2 = i + 1
            while j2 < n and _is_ellipsis(tmpl_items[j2], ellipsis_sym):
                num_ell = num_ell + 1
                j2 = j2 + 1
            _expand_ellipsis_run(elem, match, num_ell, ellipsis_sym, use_src,
                                 free_id_map, output)
            i = j2
            continue
        output.append(_instantiate(
            elem, match, ellipsis_sym, use_src, free_id_map))
        i = i + 1
    return make_vector(output)


def _instantiate_list(tmpl_list, match, ellipsis_sym, use_src, free_id_map):
    output = []
    cur = tmpl_list
    while is_cons(cur):
        elem = cur.car
        rest = cur.cdr
        has_ell = is_cons(rest) and _is_ellipsis(rest.car, ellipsis_sym)
        if has_ell:
            # Count the run of consecutive ellipses (x ... ... flattens levels).
            num_ell = 0
            e = rest
            while is_cons(e) and _is_ellipsis(e.car, ellipsis_sym):
                num_ell = num_ell + 1
                e = e.cdr
            _expand_ellipsis_run(elem, match, num_ell, ellipsis_sym, use_src,
                                 free_id_map, output)
            cur = e
            continue
        output.append(_instantiate(
            elem, match, ellipsis_sym, use_src, free_id_map))
        cur = rest
    if is_nil(cur):
        tail = NIL_VALUE
    else:
        tail = _instantiate(cur, match, ellipsis_sym, use_src, free_id_map)
    result = tail
    i = len(output) - 1
    while i >= 0:
        result = alloc_cons(output[i], result, use_src)
        i = i - 1
    return result


# ── Transformer application ─────────────────────────────────────────────────

def apply_syntax_transformer(t, form):
    """Try each rule in order; on match, instantiate the template.
    Raises SchemeSyntaxError if no pattern matches."""
    from pyscheme.Parser import SchemeSyntaxError
    literals = t.literals
    ellipsis_sym = t.ellipsis
    base_map = t.free_id_map if t.free_id_map is not None else {}
    use_src = src_of(form)
    # Per-application gensym for introduced hygienic temporaries (operand- and
    # binding-position intro names): ensures macro-introduced identifiers don't
    # capture, or get captured by, same-named use-site variables -- including
    # ones that only become binders after a later recursive expansion.
    hygienic_intros = getattr(t, 'hygienic_intro_names', None)
    if hygienic_intros:
        free_id_map = dict(base_map)
        for iname in hygienic_intros:
            if iname not in free_id_map:
                # force=True when iname is already a gensym (a binder threaded in
                # from an enclosing binding): it must be freshened per application
                # so the generated binder doesn't capture a same-named use-site
                # reference (A1f).  hygiene_gensym otherwise passes gensyms through.
                free_id_map[iname] = hygiene_gensym(
                    iname, force=iname.startswith(GENSYM_PREFIX))
    else:
        free_id_map = base_map
    form_tail = form.cdr if is_cons(form) else NIL_VALUE
    # Mint a fresh per-application mark; _instantiate paints introduced
    # identifiers with it.  Save/restore defensively in case instantiation ever
    # re-enters (it should not within one application).
    global _MARK_COUNTER, _current_mark
    _MARK_COUNTER = _MARK_COUNTER + 1
    this_mark = _MARK_COUNTER
    i = 0
    while i < len(t.rules):
        pattern = t.rules[i][0]
        template = t.rules[i][1]
        if is_cons(pattern):
            match = _SyntaxMatch()
            if _match_list_pattern(pattern.cdr, form_tail, literals,
                                   ellipsis_sym, match):
                saved_mark = _current_mark
                _current_mark = this_mark
                try:
                    return _instantiate(template, match, ellipsis_sym, use_src,
                                        free_id_map)
                finally:
                    _current_mark = saved_mark
        i = i + 1
    raise SchemeSyntaxError(
        "syntax-rules: no matching pattern for '" + t.name + "'", use_src)


# ── Parse (syntax-rules [ellipsis] (literals) rules...) ─────────────────────

def parse_syntax_rules(tail, def_env, name, form_src=None):
    """Parse a syntax-rules body into a SyntaxTransformer.

    form_src is the source position of the whole (syntax-rules ...) form,
    used for the 'malformed' diagnostic since tail (the form's cdr) may be
    a bare NIL with no position.

    def_env is a flat dict snapshot of the current runtime env (name->value).
    For each free template identifier:
      - If found in def_env: create a gensym alias, bind alias->def_value in
        the runtime env, store in free_id_map.
      - If not found: add to intro_names (introduced binding sites)."""
    from pyscheme.Parser import SchemeSyntaxError
    if not is_cons(tail):
        raise SchemeSyntaxError('syntax-rules: malformed',
                                form_src if form_src is not None else src_of(tail))
    ellipsis_sym = '...'
    first = tail.car
    rest = tail.cdr
    if is_symbol(first) and is_cons(rest):
        second = rest.car
        if is_nil(second) or is_cons(second):
            ellipsis_sym = as_symbol(first)
            lit_list = second
            rules_list = rest.cdr
        else:
            lit_list = first
            rules_list = rest
    else:
        lit_list = first
        rules_list = rest
    literals = []
    cur = lit_list
    while is_cons(cur):
        if not is_symbol(cur.car):
            raise SchemeSyntaxError(
                'syntax-rules: literal must be a symbol', src_of(cur.car))
        lit_name = as_symbol(cur.car)
        literals.append(lit_name)
        cur = cur.cdr
    # R7RS 4.3.2 literal-priority: an identifier in <literals> is always a
    # literal, overriding any role as `_` (wildcard) or as the ellipsis.  If the
    # ellipsis symbol is itself declared a literal, ellipsis matching is disabled
    # (the elli-lit case); swap in a sentinel that no real symbol can equal so
    # every ellipsis test below sees "no active ellipsis" while the literal
    # itself still matches/instantiates verbatim via the literals list.
    if ellipsis_sym in literals:
        ellipsis_sym = '\x00no-ellipsis\x00'
    rules = []
    pvars_union = set()
    templates = []
    cur = rules_list
    while is_cons(cur):
        rule = cur.car
        if not is_cons(rule) or not is_cons(rule.cdr):
            raise SchemeSyntaxError(
                'syntax-rules: each rule must be (pattern template)',
                src_of(rule))
        pattern = rule.car
        template = rule.cdr.car
        pvars = set()
        if is_cons(pattern):
            collect_pvars(pattern.cdr, literals, ellipsis_sym, pvars)
        pvars_union = pvars_union | pvars
        rules.append((pattern, template))
        templates.append((template, pvars))
        cur = cur.cdr
    # Collect all free identifiers across all templates.
    free_ids = set()
    for tmpl, pvars in templates:
        collect_free_ids(tmpl, pvars, literals, ellipsis_sym, free_ids)
    # Collect binding-position intro names (need per-application gensym).
    binding_intros = set()
    for tmpl, pvars in templates:
        collect_binding_intros(tmpl, pvars, binding_intros)
    # Collect introduced names threaded as operands through a recursive
    # self-call: these may become binders in another rule of the macro, so they
    # need a per-application gensym (see collect_self_call_operand_intros).
    self_call_intros = set()
    for tmpl, pvars in templates:
        collect_self_call_operand_intros(tmpl, name, pvars, self_call_intros)
    # Build free_id_map and intro_names from def_env.
    free_id_map = {}
    intro_names = set()
    if def_env is not None:
        for fid in free_ids:
            if fid in def_env:
                gs = hygiene_gensym(fid)
                free_id_map[fid] = gs
            else:
                intro_names.add(fid)
    else:
        for fid in free_ids:
            intro_names.add(fid)
    # Hygienic temporaries needing a per-application gensym: introduced names
    # that appear in any value position -- operands (intro_names - head_names)
    # or binders (intro_names & binding_intros).  A name used purely as an
    # operator head (a macro / procedure / special-form reference, e.g.
    # `fresh`, `run`, `==`, including forward and self references) is left
    # as-is so it resolves at the use site.  The binding-position union also
    # re-admits a name used both as a head and a binder, e.g. `f` in
    # `(lambda (f) (f x))`.  This catches operand-position temporaries that
    # only become binders after a later (recursive) expansion -- the case
    # miniKanren's `run` macro hits with its introduced query variable `q`.
    # A1f spike: a binding-position identifier is ALWAYS a fresh per-application
    # binder, even if its name happens to be bound in the def-env.  A template
    # binder introduces a NEW binding, not a reference, so it must never be
    # aliased to the def-site -- otherwise a generated macro whose binder name
    # collides with an enclosing binding (e.g. foo's `y` = the outer `x`) reuses
    # that binding and captures a same-named use-site reference.  Pull binders
    # out of free_id_map and route them through the fresh-gensym path.
    for _b in binding_intros:
        if _b in free_id_map:
            del free_id_map[_b]
        intro_names.add(_b)
    hygienic_intro_names = (intro_names
                            & (binding_intros | self_call_intros))
    # Wire each free_id alias into the GLOBAL runtime env so it persists past
    # any temporary body-scan child envs and is reachable at eval time.  The
    # alias is an INDIRECTION to the def-site binding (resolved live where that
    # binding still exists -- e.g. a top-level global -- so set! through the
    # macro writes through and later mutations are seen; the A5 hygiene bug),
    # with the def-time VALUE snapshot as a fallback for references whose def env
    # is a transient scan scope at eval time (library-internal helpers).  Either
    # way a same-named use-site binding is bypassed (referential transparency).
    if free_id_map and def_env is not None:
        try:
            from pyscheme.Expander import get_runtime_env
            env = get_runtime_env()
            if env is not None:
                global_env = env.getGlobalEnv()
                for fid, gs in free_id_map.items():
                    # A fid that is itself a hygiene gensym (hygiene_gensym
                    # returned it unchanged, so gs == fid) was already wired up
                    # by the macro that introduced it; re-aliasing it to itself
                    # would build a cyclic _AliasCell.  Leave the existing binding.
                    if fid.startswith(GENSYM_PREFIX):
                        continue
                    global_env.register_alias(gs, fid, env, def_env[fid])
        except ImportError:
            pass
    t = make_syntax_transformer(name, literals, ellipsis_sym, rules,
                                free_id_map, intro_names)
    t.hygienic_intro_names = hygienic_intro_names
    return t


# ── Self-test ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from pyscheme.Parser import parse_one

    n_pass = 0
    n_fail = 0

    def check(label, cond):
        global n_pass, n_fail
        if cond:
            print('[ OK ] ' + label)
            n_pass = n_pass + 1
        else:
            print('[FAIL] ' + label)
            n_fail = n_fail + 1

    # Pattern matching: literal symbol.
    pat = parse_one('(foo bar)')
    form = parse_one('(foo bar)')
    m = _SyntaxMatch()
    ok = _match_list_pattern(pat.cdr, form.cdr, ['bar'], '...', m)
    check('literal matches', ok)

    # Pattern variable capture.
    pat = parse_one('(foo x)')
    form = parse_one('(foo 42)')
    m = _SyntaxMatch()
    ok = _match_list_pattern(pat.cdr, form.cdr, [], '...', m)
    check('pvar matches', ok)
    check('pvar bound', 'x' in m.scalars)

    # Literal mismatch due to alpha-rename: gensym name != literal name.
    pat = parse_one('(foo bar)')
    form_sym = make_symbol('\x01h.bar.99', None)
    from pyscheme.AST import alloc_cons, NIL_VALUE
    renamed_form = alloc_cons(parse_one('foo'), alloc_cons(
        form_sym, NIL_VALUE, None), None)
    m2 = _SyntaxMatch()
    ok2 = _match_list_pattern(pat.cdr, renamed_form.cdr, ['bar'], '...', m2)
    check('renamed literal does not match', not ok2)

    # Ellipsis.
    pat = parse_one('(foo x ...)')
    form = parse_one('(foo 1 2 3)')
    m = _SyntaxMatch()
    ok = _match_list_pattern(pat.cdr, form.cdr, [], '...', m)
    check('ellipsis matches', ok)
    check('ellipsis depth 1', m.ell_depth.get('x') == 1)
    check('ellipsis count 3', len(m.ellipsis.get('x', [])) == 3)

    # parse_syntax_rules.
    body = parse_one('(() ((_ a b) (cons a b)))')
    t = parse_syntax_rules(body, {}, 'mymacro')
    check('parsed no literals', t.literals == [])
    check('parsed one rule', len(t.rules) == 1)
    check('default ellipsis', t.ellipsis == '...')
    check('cons in intro_names', 'cons' in t.intro_names)

    # Apply a simple transformer.
    body2 = parse_one('(() ((_ a b) (list a b)))')
    t2 = parse_syntax_rules(body2, {}, 'pair-up')
    form2 = parse_one('(pair-up 1 2)')
    result = apply_syntax_transformer(t2, form2)
    check('expansion is cons', is_cons(result))
    check('expansion arity 3', _list_length_approx(result) == 3)
    check('first is renamed list symbol', is_symbol(result.car))
    check('second is 1', is_integer(result.cdr.car)
          and as_integer(result.cdr.car) == 1)
    check('third is 2', is_integer(result.cdr.cdr.car)
          and as_integer(result.cdr.cdr.car) == 2)

    print()
    print('%d passed, %d failed' % (n_pass, n_fail))
