"""Equivalence primitives: eq?, eqv?, equal?.

Semantics (loose R7RS alignment):
    eq?    - Identity for pairs/strings/closures; tag+payload equality for
             symbols, booleans, and nil.  Ignores trailing src on atoms.
    eqv?   - Like eq? plus numeric value-and-exactness equality:
             (eqv? 2 2) -> #t; (eqv? 2 2.0) -> #f.
    equal? - Recursive structural equality; walks ConsCell chains and
             compares atoms by tag+payload (ignoring src).
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
    is_cons, is_nil, is_void,
    is_symbol, is_boolean, is_string, is_character,
    is_integer, is_real, is_rational, is_complex, is_exact_complex,
    is_vector, is_bytevector,
    as_symbol, as_boolean, as_string, as_character,
    as_integer, as_real,
    as_rational_num, as_rational_den,
    as_complex_real, as_complex_imag,
    as_exact_complex_real, as_exact_complex_imag,
    as_vector_items, as_bytevector_items,
    make_boolean, eqv_atom,
)


CATEGORY = 'equivalence'


def _prim_eq_p(ctx, env, args, app_node):
    a = args[0]
    b = args[1]
    if a is b:
        return make_boolean(True)
    if is_symbol(a) and is_symbol(b):
        return make_boolean(as_symbol(a) == as_symbol(b))
    if is_boolean(a) and is_boolean(b):
        return make_boolean(as_boolean(a) is as_boolean(b))
    if is_nil(a) and is_nil(b):
        return make_boolean(True)
    return make_boolean(False)


def _prim_eqv_p(ctx, env, args, app_node):
    return make_boolean(eqv_atom(args[0], args[1]))


def _equal_leaf(a, b, seen, work):
    """Compare two NON-cons values for equal?.  Compound leaves (vectors,
    exact-complex) enqueue their children onto `work` and return True; atomic
    leaves compare directly.  Returns False on a definite mismatch.  Caller
    guarantees neither a nor b is a cons (those are handled by the cdr-spine
    loop in _value_equal)."""
    if is_vector(a):
        if not is_vector(b):
            return False
        va = as_vector_items(a)
        vb = as_vector_items(b)
        if len(va) != len(vb):
            return False
        key = (id(a), id(b))
        if key in seen:
            return True
        seen.add(key)
        i = 0
        while i < len(va):
            work.append((va[i], vb[i]))
            i = i + 1
        return True
    if is_vector(b):
        return False
    if is_bytevector(a):
        if not is_bytevector(b):
            return False
        return as_bytevector_items(a) == as_bytevector_items(b)
    if is_bytevector(b):
        return False
    if a is b:
        return True
    if is_nil(a) and is_nil(b):
        return True
    if is_void(a) and is_void(b):
        return True
    if is_symbol(a) and is_symbol(b):
        return as_symbol(a) == as_symbol(b)
    if is_integer(a) and is_integer(b):
        return as_integer(a) == as_integer(b)
    if is_real(a) and is_real(b):
        return as_real(a) == as_real(b)
    if is_rational(a) and is_rational(b):
        return (as_rational_num(a) == as_rational_num(b)
                and as_rational_den(a) == as_rational_den(b))
    if is_complex(a) and is_complex(b):
        return (as_complex_real(a) == as_complex_real(b)
                and as_complex_imag(a) == as_complex_imag(b))
    if is_exact_complex(a) and is_exact_complex(b):
        work.append((as_exact_complex_real(a), as_exact_complex_real(b)))
        work.append((as_exact_complex_imag(a), as_exact_complex_imag(b)))
        return True
    if is_boolean(a) and is_boolean(b):
        return as_boolean(a) is as_boolean(b)
    if is_string(a) and is_string(b):
        return as_string(a) == as_string(b)
    if is_character(a) and is_character(b):
        return as_character(a) == as_character(b)
    return False


def _value_equal(a, b, seen=None):
    """Structural equality ignoring trailing src on atoms.  Walks ConsCell
    chains and vectors via an explicit heap worklist instead of recursion:
    the cdr-spine is iterated in a loop (so a long flat list costs heap, not
    Python stack), with each car (and vector items / exact-complex parts)
    pushed for later comparison.  Depth is heap-bounded, so deeply nested or
    very long structures no longer raise RecursionError.  The 'seen' set tracks
    (id(a), id(b)) pairs for mutable structures to bound the walk on cycles.
    equal? is a conjunction over all node-pairs, so comparison order does not
    affect the result -- we just bail on the first mismatch."""
    if seen is None:
        seen = set()
    work = [(a, b)]
    while work:
        x, y = work.pop()
        # cons cdr-spine: descend cdr in a loop, pushing each car; break early
        # if this pair was already seen (a cycle -> treat as equal).
        cycle = False
        while is_cons(x):
            if not is_cons(y):
                return False
            key = (id(x), id(y))
            if key in seen:
                cycle = True
                break
            seen.add(key)
            work.append((x.car, y.car))
            x = x.cdr
            y = y.cdr
        if cycle:
            continue
        if is_cons(y):
            return False
        # x and y are both non-cons here.
        if not _equal_leaf(x, y, seen, work):
            return False
    return True


def _prim_equal_p(ctx, env, args, app_node):
    return make_boolean(_value_equal(args[0], args[1]))


def _prim_boolean_eq_p(ctx, env, args, app_node):
    from pyscheme.Environment import SchemeTypeError
    from pyscheme.AST import src_of
    first = args[0]
    if not is_boolean(first):
        raise SchemeTypeError(
            'boolean=?: arguments must be booleans', src_of(app_node))
    first_val = as_boolean(first)
    i = 1
    while i < len(args):
        v = args[i]
        if not is_boolean(v):
            raise SchemeTypeError(
                'boolean=?: arguments must be booleans', src_of(app_node))
        if as_boolean(v) is not first_val:
            return make_boolean(False)
        i = i + 1
    return make_boolean(True)


def _prim_symbol_eq_p(ctx, env, args, app_node):
    from pyscheme.Environment import SchemeTypeError
    from pyscheme.AST import src_of
    first = args[0]
    if not is_symbol(first):
        raise SchemeTypeError(
            'symbol=?: arguments must be symbols', src_of(app_node))
    first_name = as_symbol(first)
    i = 1
    while i < len(args):
        v = args[i]
        if not is_symbol(v):
            raise SchemeTypeError(
                'symbol=?: arguments must be symbols', src_of(app_node))
        if as_symbol(v) != first_name:
            return make_boolean(False)
        i = i + 1
    return make_boolean(True)


def register():
    register_primitive('eq?', (2, 2), _prim_eq_p,
                       doc=(
        "Return #t if a and b are the same object.  Symbols with the same\n"
        "name, booleans with the same truth value, and empty lists are always\n"
        "eq?.  For pairs, strings, and vectors eq? tests allocation identity."),
        category=CATEGORY)
    register_primitive('eqv?', (2, 2), _prim_eqv_p,
                       doc=(
        "Like eq?, but also returns #t for numbers with the same value and\n"
        "exactness.  (eqv? 2 2) is #t; (eqv? 2 2.0) is #f."),
        category=CATEGORY)
    register_primitive('equal?', (2, 2), _prim_equal_p,
                       doc=(
        "Recursive structural equality.  Two pairs are equal? if their cars\n"
        "and cdrs are equal?; two atoms are equal? if their tag+payload match\n"
        "(ignoring source position)."),
        category=CATEGORY)
    register_primitive('boolean=?', (2, None), _prim_boolean_eq_p,
                       doc=('(boolean=? a b ...) returns #t when all arguments are booleans '
                            'with the same truth value.  R7RS 6.3.'),
                       category=CATEGORY)
    register_primitive('symbol=?', (2, None), _prim_symbol_eq_p,
                       doc=('(symbol=? a b ...) returns #t when all arguments are symbols '
                            'with the same name.  R7RS 6.5.'),
                       category=CATEGORY)
