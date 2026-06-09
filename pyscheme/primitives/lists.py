"""List and pair primitives: cons, car, cdr, pair?, null?, list,
append, length, reverse, list-tail, list-ref, list-copy, list?,
member / memv / memq, assoc / assv / assq, map, for-each, set-car!,
set-cdr!, make-list, list-set!, plus the caar/cadr/cdar/cddr family."""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
    alloc_cons, NIL_VALUE, VOID_VALUE,
    is_cons, is_nil, is_integer, as_integer,
    src_of, set_car, set_cdr,
    make_boolean, make_integer, eqv_atom,
)
from pyscheme.Environment import SchemeTypeError, SchemeArityError, arity_mismatch_msg


CATEGORY = 'lists'


def _prim_cons(ctx, env, args, app_node):
    return alloc_cons(args[0], args[1], None)


def _prim_car(ctx, env, args, app_node):
    v = args[0]
    if not is_cons(v):
        raise SchemeTypeError('car: expected pair, got non-pair value',
                              src_of(app_node))
    return v.car


def _prim_cdr(ctx, env, args, app_node):
    v = args[0]
    if not is_cons(v):
        raise SchemeTypeError('cdr: expected pair, got non-pair value',
                              src_of(app_node))
    return v.cdr


def _prim_pair_p(ctx, env, args, app_node):
    return make_boolean(is_cons(args[0]))


def _prim_null_p(ctx, env, args, app_node):
    return make_boolean(is_nil(args[0]))


def _prim_list(ctx, env, args, app_node):
    result = NIL_VALUE
    i = len(args) - 1
    while i >= 0:
        result = alloc_cons(args[i], result, None)
        i = i - 1
    return result


def _proper_list_p(v):
    """True if v is a proper list (terminated by NIL).  Uses Floyd's
    cycle-detection so list? terminates in finite time on circular
    structures, returning #f as R7RS §6.4 requires."""
    slow = v
    fast = v
    while True:
        if is_nil(fast):
            return True
        if not is_cons(fast):
            return False
        fast = fast.cdr
        if is_nil(fast):
            return True
        if not is_cons(fast):
            return False
        fast = fast.cdr
        slow = slow.cdr
        if slow is fast:
            return False


def _prim_list_p(ctx, env, args, app_node):
    return make_boolean(_proper_list_p(args[0]))


def _prim_length(ctx, env, args, app_node):
    v = args[0]
    n = 0
    cur = v
    while is_cons(cur):
        n = n + 1
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'length: argument must be a proper list', src_of(app_node))
    return make_integer(n)


def _prim_reverse(ctx, env, args, app_node):
    v = args[0]
    result = NIL_VALUE
    cur = v
    while is_cons(cur):
        result = alloc_cons(cur.car, result, None)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'reverse: argument must be a proper list', src_of(app_node))
    return result


def _prim_list_tail(ctx, env, args, app_node):
    v = args[0]
    k_v = args[1]
    if not is_integer(k_v):
        raise SchemeTypeError(
            'list-tail: index must be an integer', src_of(app_node))
    k = as_integer(k_v)
    if k < 0:
        raise SchemeTypeError(
            'list-tail: index must be non-negative', src_of(app_node))
    cur = v
    i = 0
    while i < k:
        if not is_cons(cur):
            raise SchemeTypeError(
                'list-tail: index out of range', src_of(app_node))
        cur = cur.cdr
        i = i + 1
    return cur


def _prim_list_ref(ctx, env, args, app_node):
    v = args[0]
    k_v = args[1]
    if not is_integer(k_v):
        raise SchemeTypeError(
            'list-ref: index must be an integer', src_of(app_node))
    k = as_integer(k_v)
    if k < 0:
        raise SchemeTypeError(
            'list-ref: index must be non-negative', src_of(app_node))
    cur = v
    i = 0
    while i < k:
        if not is_cons(cur):
            raise SchemeTypeError(
                'list-ref: index out of range', src_of(app_node))
        cur = cur.cdr
        i = i + 1
    if not is_cons(cur):
        raise SchemeTypeError(
            'list-ref: index out of range', src_of(app_node))
    return cur.car


def _prim_list_copy(ctx, env, args, app_node):
    v = args[0]
    if is_nil(v):
        return NIL_VALUE
    if not is_cons(v):
        return v
    collected = []
    cur = v
    while is_cons(cur):
        collected.append(cur.car)
        cur = cur.cdr
    tail = cur
    i = len(collected) - 1
    while i >= 0:
        tail = alloc_cons(collected[i], tail, None)
        i = i - 1
    return tail


def _make_member_search(name, equality_predicate):
    def search(ctx, env, args, app_node):
        from pyscheme.Evaluator import isFalse
        target = args[0]
        cur = args[1]
        if len(args) >= 3:
            from pyscheme.primitives.meta import _apply_scheme_proc
            compare_proc = args[2]
            while is_cons(cur):
                result = _apply_scheme_proc(compare_proc, [target, cur.car],
                                            ctx, None, app_node)
                if not isFalse(result):
                    return cur
                cur = cur.cdr
        else:
            while is_cons(cur):
                if equality_predicate(target, cur.car):
                    return cur
                cur = cur.cdr
        if not is_nil(cur):
            raise SchemeTypeError(
                name + ': second argument must be a proper list',
                src_of(app_node))
        return make_boolean(False)
    search.__name__ = '_prim_' + name
    return search


def _make_assoc_search(name, equality_predicate):
    def search(ctx, env, args, app_node):
        from pyscheme.Evaluator import isFalse
        target = args[0]
        cur = args[1]
        if len(args) >= 3:
            from pyscheme.primitives.meta import _apply_scheme_proc
            compare_proc = args[2]
            while is_cons(cur):
                pair = cur.car
                if not is_cons(pair):
                    raise SchemeTypeError(
                        name + ': alist entries must be pairs', src_of(app_node))
                result = _apply_scheme_proc(compare_proc, [target, pair.car],
                                            ctx, None, app_node)
                if not isFalse(result):
                    return pair
                cur = cur.cdr
        else:
            while is_cons(cur):
                pair = cur.car
                if not is_cons(pair):
                    raise SchemeTypeError(
                        name + ': alist entries must be pairs', src_of(app_node))
                if equality_predicate(target, pair.car):
                    return pair
                cur = cur.cdr
        if not is_nil(cur):
            raise SchemeTypeError(
                name + ': second argument must be a proper list',
                src_of(app_node))
        return make_boolean(False)
    search.__name__ = '_prim_' + name
    return search


def _eq(a, b):
    return a is b or eqv_atom(a, b)


def _eqv(a, b):
    return eqv_atom(a, b) or a is b


def _equal(a, b):
    from pyscheme.primitives.equivalence import _value_equal
    return _value_equal(a, b)


def _prim_map(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    if len(args) < 2:
        raise SchemeArityError(
            arity_mismatch_msg('map', 2, None, len(args)),
            src_of(app_node))
    proc = args[0]
    lists = args[1:]
    collected = []
    while True:
        ready = True
        for lst in lists:
            if not is_cons(lst):
                ready = False
                break
        if not ready:
            break
        arg_row = []
        next_lists = []
        for lst in lists:
            arg_row.append(lst.car)
            next_lists.append(lst.cdr)
        collected.append(_apply_scheme_proc(
            proc, arg_row, ctx, None, app_node))
        lists = next_lists
    # R7RS 6.10: if the lists are of unequal length, map terminates when the
    # shortest runs out, so leftover cons cells in longer lists are fine.  An
    # improper list (a non-nil, non-pair tail) reached during traversal is an
    # error.
    for lst in lists:
        if not is_cons(lst) and not is_nil(lst):
            raise SchemeTypeError(
                'map: list arguments must be proper lists',
                src_of(app_node))
    result = NIL_VALUE
    i = len(collected) - 1
    while i >= 0:
        result = alloc_cons(collected[i], result, None)
        i = i - 1
    return result


def _prim_filter(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    from pyscheme.Evaluator import isFalse
    pred = args[0]
    lst = args[1]
    collected = []
    while is_cons(lst):
        keep = _apply_scheme_proc(pred, [lst.car], ctx, None, app_node)
        if not isFalse(keep):
            collected.append(lst.car)
        lst = lst.cdr
    if not is_nil(lst):
        raise SchemeTypeError(
            'filter: list argument must be a proper list',
            src_of(app_node))
    result = NIL_VALUE
    i = len(collected) - 1
    while i >= 0:
        result = alloc_cons(collected[i], result, None)
        i = i - 1
    return result


def _prim_for_each(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    from pyscheme.AST import VOID_VALUE
    if len(args) < 2:
        raise SchemeArityError(
            arity_mismatch_msg('for-each', 2, None, len(args)),
            src_of(app_node))
    proc = args[0]
    lists = args[1:]
    while True:
        ready = True
        for lst in lists:
            if not is_cons(lst):
                ready = False
                break
        if not ready:
            break
        arg_row = []
        next_lists = []
        for lst in lists:
            arg_row.append(lst.car)
            next_lists.append(lst.cdr)
        _apply_scheme_proc(proc, arg_row, ctx, None, app_node)
        lists = next_lists
    # R7RS 6.10: unequal-length lists terminate at the shortest; only a
    # genuinely improper (non-nil, non-pair) tail is an error.
    for lst in lists:
        if not is_cons(lst) and not is_nil(lst):
            raise SchemeTypeError(
                'for-each: list arguments must be proper lists',
                src_of(app_node))
    return VOID_VALUE


def _make_cxr(name, ops):
    """Build a c..r primitive that walks a chain of car/cdr.  ops is a
    string of 'a' and 'd' characters: caar -> "aa", cdadr -> "dad".
    The string reads left-to-right matching the symbol's letters
    between c and r."""
    def fn(ctx, env, args, app_node):
        v = args[0]
        i = len(ops) - 1
        while i >= 0:
            if not is_cons(v):
                raise SchemeTypeError(
                    '%s: chain hit a non-pair' % name, src_of(app_node))
            if ops[i] == 'a':
                v = v.car
            else:
                v = v.cdr
            i = i - 1
        return v
    fn.__name__ = '_prim_' + name
    return fn


def _prim_set_car(ctx, env, args, app_node):
    v = args[0]
    if not is_cons(v):
        raise SchemeTypeError(
            'set-car!: argument must be a pair', src_of(app_node))
    if v.immutable:
        raise SchemeTypeError(
            'set-car!: argument is an immutable literal', src_of(app_node))
    set_car(v, args[1])
    return VOID_VALUE


def _prim_set_cdr(ctx, env, args, app_node):
    v = args[0]
    if not is_cons(v):
        raise SchemeTypeError(
            'set-cdr!: argument must be a pair', src_of(app_node))
    if v.immutable:
        raise SchemeTypeError(
            'set-cdr!: argument is an immutable literal', src_of(app_node))
    set_cdr(v, args[1])
    return VOID_VALUE


def _prim_make_list(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'make-list: length must be an integer', src_of(app_node))
    k = as_integer(args[0])
    if k < 0:
        raise SchemeTypeError(
            'make-list: length must be non-negative', src_of(app_node))
    fill = VOID_VALUE
    if len(args) >= 2:
        fill = args[1]
    result = NIL_VALUE
    i = 0
    while i < k:
        result = alloc_cons(fill, result, None)
        i = i + 1
    return result


def _prim_list_set(ctx, env, args, app_node):
    v = args[0]
    k_v = args[1]
    if not is_integer(k_v):
        raise SchemeTypeError(
            'list-set!: index must be an integer', src_of(app_node))
    k = as_integer(k_v)
    if k < 0:
        raise SchemeTypeError(
            'list-set!: index must be non-negative', src_of(app_node))
    cur = v
    i = 0
    while i < k:
        if not is_cons(cur):
            raise SchemeTypeError(
                'list-set!: index out of range', src_of(app_node))
        cur = cur.cdr
        i = i + 1
    if not is_cons(cur):
        raise SchemeTypeError(
            'list-set!: index out of range', src_of(app_node))
    set_car(cur, args[2])
    return VOID_VALUE


def _prim_append(ctx, env, args, app_node):
    # (append list1 list2 ... listN) - all but the last must be proper
    # lists; the last may be any value and forms the final cdr.
    if not args:
        return NIL_VALUE
    if len(args) == 1:
        return args[0]
    collected = []
    i = 0
    while i < len(args) - 1:
        cur = args[i]
        while is_cons(cur):
            collected.append(cur.car)
            cur = cur.cdr
        if not is_nil(cur):
            raise SchemeTypeError(
                'append: non-last argument must be a proper list', app_node)
        i = i + 1
    tail = args[len(args) - 1]
    j = len(collected) - 1
    while j >= 0:
        tail = alloc_cons(collected[j], tail, None)
        j = j - 1
    return tail


def register():
    register_primitive('cons', (2, 2), _prim_cons,
                       doc=(
        "Return a newly allocated pair whose car is a and whose cdr is b.\n"
        "The pair is guaranteed to be different (in the sense of eq?) from every\n"
        "existing object."),
        category=CATEGORY)
    register_primitive('car', (1, 1), _prim_car,
                       doc=(
        "Return the contents of the car field of the pair a.  Signals an\n"
        "error if a is not a pair."),
        category=CATEGORY)
    register_primitive('cdr', (1, 1), _prim_cdr,
                       doc=(
        "Return the contents of the cdr field of the pair a.  Signals an\n"
        "error if a is not a pair."),
        category=CATEGORY)
    register_primitive('pair?', (1, 1), _prim_pair_p,
                       doc=(
        "Return #t if a is a pair, #f otherwise.  The empty list is not\n"
        "considered a pair."),
        category=CATEGORY)
    register_primitive('null?', (1, 1), _prim_null_p,
                       doc='Return #t if a is the empty list (), #f otherwise.',
                       category=CATEGORY)
    register_primitive('list', (0, None), _prim_list,
                       doc=(
        "Return a newly allocated list containing the arguments in order.\n"
        "With no arguments, returns the empty list."),
        category=CATEGORY)
    register_primitive('append', (0, None), _prim_append,
                       doc=(
        "Return a list that is the concatenation of its arguments.  All\n"
        "but the last argument must be proper lists; the last may be any\n"
        "value (forming the final cdr).  R7RS 6.4."),
        category=CATEGORY)
    register_primitive('list?', (1, 1), _prim_list_p,
                       doc='Return #t if a is a proper list (terminated by ()).  R7RS 6.4.',
                       category=CATEGORY)
    register_primitive('length', (1, 1), _prim_length,
                       doc=('Return the number of elements in the list.  Argument must '
                            'be a proper list.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('reverse', (1, 1), _prim_reverse,
                       doc=('Return a newly allocated list of the elements of list in '
                            'reverse order.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('list-tail', (2, 2), _prim_list_tail,
                       doc=('(list-tail list k) returns the sublist obtained by omitting '
                            'the first k elements.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('list-ref', (2, 2), _prim_list_ref,
                       doc=('(list-ref list k) returns the kth element of list (zero-based).  '
                            'R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('list-copy', (1, 1), _prim_list_copy,
                       doc=('Return a newly allocated copy of the spine of the list, '
                            'sharing element values.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('member', (2, 3), _make_member_search('member', _equal),
                       doc=('(member obj list [compare]) returns the first sublist of list whose car '
                            'is equal? to obj, or #f if no such sublist exists.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('memv', (2, 2), _make_member_search('memv', _eqv),
                       doc=('Like member but uses eqv? for comparison.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('memq', (2, 2), _make_member_search('memq', _eq),
                       doc=('Like member but uses eq? for comparison.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('assoc', (2, 3), _make_assoc_search('assoc', _equal),
                       doc=('(assoc obj alist [compare]) returns the first pair in alist whose car is '
                            'equal? to obj, or #f if no such pair exists.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('assv', (2, 2), _make_assoc_search('assv', _eqv),
                       doc=('Like assoc but uses eqv? for comparison.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('assq', (2, 2), _make_assoc_search('assq', _eq),
                       doc=('Like assoc but uses eq? for comparison.  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('map', (2, None), _prim_map,
                       doc=('(map proc list1 list2 ...) applies proc element-wise to '
                            'each list in parallel and returns a list of the results.  '
                            'All lists must be the same length.  R7RS 6.10.'),
                       category=CATEGORY)
    register_primitive('for-each', (2, None), _prim_for_each,
                       doc=('(for-each proc list1 list2 ...) applies proc element-wise '
                            'for effect and returns an unspecified value.  R7RS 6.10.'),
                       category=CATEGORY)
    register_primitive('filter', (2, 2), _prim_filter,
                       doc=('(filter pred list) returns a new list containing the elements '
                            'of list for which pred returns a true value.  R7RS-large / SRFI-1.'),
                       category=CATEGORY)
    register_primitive('set-car!', (2, 2), _prim_set_car,
                       doc='(set-car! pair obj) replaces the car of pair with obj.  R7RS 6.4.',
                       category=CATEGORY)
    register_primitive('set-cdr!', (2, 2), _prim_set_cdr,
                       doc='(set-cdr! pair obj) replaces the cdr of pair with obj.  R7RS 6.4.',
                       category=CATEGORY)
    register_primitive('make-list', (1, 2), _prim_make_list,
                       doc=('(make-list k [fill]) returns a list of length k with each '
                            'element fill (default unspecified).  R7RS 6.4.'),
                       category=CATEGORY)
    register_primitive('list-set!', (3, 3), _prim_list_set,
                       doc='(list-set! list k obj) replaces the kth element with obj.  R7RS 6.4.',
                       category=CATEGORY)
    # car/cdr family - generated from string descriptions.  R7RS 6.4 base
    # specifies caar / cadr / cdar / cddr; (scheme cxr) extends with the
    # full 28-name family up to 4 deep.  We register the lot here since
    # they are tiny and the (scheme cxr) library export filter picks up
    # whichever names are bound.
    _CXR_FAMILY = [
        'caar',  'cadr',  'cdar',  'cddr',
        'caaar', 'caadr', 'cadar', 'caddr',
        'cdaar', 'cdadr', 'cddar', 'cdddr',
        'caaaar', 'caaadr', 'caadar', 'caaddr',
        'cadaar', 'cadadr', 'caddar', 'cadddr',
        'cdaaar', 'cdaadr', 'cdadar', 'cdaddr',
        'cddaar', 'cddadr', 'cdddar', 'cddddr',
    ]
    for _n in _CXR_FAMILY:
        _ops = _n[1:-1]   # strip leading 'c' and trailing 'r'
        register_primitive(_n, (1, 1), _make_cxr(_n, _ops),
                           doc=('(' + _n + ' x) is shorthand for the corresponding car/cdr '
                                'composition.  R7RS 6.4 / (scheme cxr).'),
                           category=CATEGORY)
