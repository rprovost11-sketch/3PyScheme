"""Vector primitives.

R7RS 6.8 - vectors are heterogeneous fixed-length sequences with
constant-time indexed access.  Our impl backs them with a Python list
held in a tagged tuple (VECTOR, items); mutations via vector-set! /
vector-fill! / vector-copy! mutate the list in place, matching Scheme
vector semantics.

Covers: vector?, make-vector, vector, vector-length, vector-ref,
vector-set!, vector->list, list->vector, vector-fill!, vector-copy,
vector-copy!, vector-append, vector->string, string->vector,
vector-for-each, vector-map.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
    alloc_cons, NIL_VALUE, VOID_VALUE,
    is_cons, is_nil, is_integer, is_string, is_character, is_vector,
    as_integer, as_string, as_character, as_vector_items,
    make_vector, make_integer, make_string, make_character, make_boolean,
    src_of,
)
from pyscheme.Environment import SchemeTypeError, SchemeArityError, arity_mismatch_msg


CATEGORY = 'vectors'


def _check_vector(v, name, app_node, idx=1):
    if not is_vector(v):
        raise SchemeTypeError(
            '%s: argument %d must be a vector' % (name, idx), src_of(app_node))
    return as_vector_items(v)


def _check_index(v, name, length, app_node):
    if not is_integer(v):
        raise SchemeTypeError(
            '%s: index must be an integer' % name, src_of(app_node))
    k = as_integer(v)
    if k < 0 or k >= length:
        raise SchemeTypeError(
            '%s: index %d out of range' % (name, k), src_of(app_node))
    return k


def _prim_vector_p(ctx, env, args, app_node):
    return make_boolean(is_vector(args[0]))


def _prim_make_vector(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'make-vector: length must be an integer', src_of(app_node))
    k = as_integer(args[0])
    if k < 0:
        raise SchemeTypeError(
            'make-vector: length must be non-negative', src_of(app_node))
    fill = VOID_VALUE
    if len(args) >= 2:
        fill = args[1]
    return make_vector([fill] * k)


def _prim_vector(ctx, env, args, app_node):
    return make_vector(list(args))


def _prim_vector_length(ctx, env, args, app_node):
    items = _check_vector(args[0], 'vector-length', app_node)
    return make_integer(len(items))


def _prim_vector_ref(ctx, env, args, app_node):
    items = _check_vector(args[0], 'vector-ref', app_node)
    k = _check_index(args[1], 'vector-ref', len(items), app_node)
    return items[k]


def _prim_vector_set(ctx, env, args, app_node):
    v = args[0]
    items = _check_vector(v, 'vector-set!', app_node)
    if v.immutable:
        raise SchemeTypeError(
            'vector-set!: argument is an immutable literal', src_of(app_node))
    k = _check_index(args[1], 'vector-set!', len(items), app_node)
    items[k] = args[2]
    return VOID_VALUE


def _prim_vector_to_list(ctx, env, args, app_node):
    items = _check_vector(args[0], 'vector->list', app_node)
    start = 0
    end = len(items)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'vector->list: start must be an integer', src_of(app_node))
        start = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'vector->list: end must be an integer', src_of(app_node))
        end = as_integer(args[2])
    if start < 0 or end > len(items) or start > end:
        raise SchemeTypeError(
            'vector->list: range out of bounds', src_of(app_node))
    result = NIL_VALUE
    i = end - 1
    while i >= start:
        result = alloc_cons(items[i], result, None)
        i = i - 1
    return result


def _prim_list_to_vector(ctx, env, args, app_node):
    v = args[0]
    collected = []
    cur = v
    while is_cons(cur):
        collected.append(cur.car)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'list->vector: argument must be a proper list', src_of(app_node))
    return make_vector(collected)


def _prim_vector_fill(ctx, env, args, app_node):
    v = args[0]
    items = _check_vector(v, 'vector-fill!', app_node)
    if v.immutable:
        raise SchemeTypeError(
            'vector-fill!: argument is an immutable literal', src_of(app_node))
    fill = args[1]
    start = 0
    end = len(items)
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'vector-fill!: start must be an integer', src_of(app_node))
        start = as_integer(args[2])
    if len(args) >= 4:
        if not is_integer(args[3]):
            raise SchemeTypeError(
                'vector-fill!: end must be an integer', src_of(app_node))
        end = as_integer(args[3])
    if start < 0 or end > len(items) or start > end:
        raise SchemeTypeError(
            'vector-fill!: range out of bounds', src_of(app_node))
    i = start
    while i < end:
        items[i] = fill
        i = i + 1
    return VOID_VALUE


def _prim_vector_copy(ctx, env, args, app_node):
    items = _check_vector(args[0], 'vector-copy', app_node)
    start = 0
    end = len(items)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'vector-copy: start must be an integer', src_of(app_node))
        start = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'vector-copy: end must be an integer', src_of(app_node))
        end = as_integer(args[2])
    if start < 0 or end > len(items) or start > end:
        raise SchemeTypeError(
            'vector-copy: range out of bounds', src_of(app_node))
    return make_vector(list(items[start:end]))


def _prim_vector_copy_bang(ctx, env, args, app_node):
    dst_v = args[0]
    dst = _check_vector(dst_v, 'vector-copy!', app_node, 1)
    if dst_v.immutable:
        raise SchemeTypeError(
            'vector-copy!: destination is an immutable literal', src_of(app_node))
    if not is_integer(args[1]):
        raise SchemeTypeError(
            'vector-copy!: at must be an integer', src_of(app_node))
    at = as_integer(args[1])
    src = _check_vector(args[2], 'vector-copy!', app_node, 3)
    start = 0
    end = len(src)
    if len(args) >= 4:
        if not is_integer(args[3]):
            raise SchemeTypeError(
                'vector-copy!: start must be an integer', src_of(app_node))
        start = as_integer(args[3])
    if len(args) >= 5:
        if not is_integer(args[4]):
            raise SchemeTypeError(
                'vector-copy!: end must be an integer', src_of(app_node))
        end = as_integer(args[4])
    if (at < 0 or start < 0 or end > len(src) or start > end
            or at + (end - start) > len(dst)):
        raise SchemeTypeError(
            'vector-copy!: range out of bounds', src_of(app_node))
    # When src and dst alias and the source slice overlaps the destination,
    # iterate in reverse to avoid clobbering yet-to-be-read elements.
    if dst is src and at > start:
        i = end - start - 1
        while i >= 0:
            dst[at + i] = src[start + i]
            i = i - 1
    else:
        i = 0
        n = end - start
        while i < n:
            dst[at + i] = src[start + i]
            i = i + 1
    return VOID_VALUE


def _prim_vector_append(ctx, env, args, app_node):
    collected = []
    i = 0
    while i < len(args):
        collected.extend(_check_vector(
            args[i], 'vector-append', app_node, i + 1))
        i = i + 1
    return make_vector(collected)


def _prim_vector_to_string(ctx, env, args, app_node):
    items = _check_vector(args[0], 'vector->string', app_node)
    start = 0
    end = len(items)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'vector->string: start must be an integer', src_of(app_node))
        start = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'vector->string: end must be an integer', src_of(app_node))
        end = as_integer(args[2])
    if start < 0 or end > len(items) or start > end:
        raise SchemeTypeError(
            'vector->string: range out of bounds', src_of(app_node))
    chars = []
    i = start
    while i < end:
        c = items[i]
        if not is_character(c):
            raise SchemeTypeError(
                'vector->string: vector elements must be characters',
                src_of(app_node))
        chars.append(as_character(c))
        i = i + 1
    return make_string(''.join(chars))


def _prim_string_to_vector(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'string->vector: argument must be a string', src_of(app_node))
    s = as_string(args[0])
    start = 0
    end = len(s)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'string->vector: start must be an integer', src_of(app_node))
        start = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'string->vector: end must be an integer', src_of(app_node))
        end = as_integer(args[2])
    if start < 0 or end > len(s) or start > end:
        raise SchemeTypeError(
            'string->vector: range out of bounds', src_of(app_node))
    chars = []
    i = start
    while i < end:
        chars.append(make_character(s[i]))
        i = i + 1
    return make_vector(chars)


def _prim_vector_for_each(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    if len(args) < 2:
        raise SchemeArityError(
            arity_mismatch_msg('vector-for-each', 2, None, len(args)),
            src_of(app_node))
    proc = args[0]
    vectors = []
    i = 1
    while i < len(args):
        vectors.append(_check_vector(
            args[i], 'vector-for-each', app_node, i + 1))
        i = i + 1
    shortest = len(vectors[0])
    _vi = 1
    while _vi < len(vectors):
        if len(vectors[_vi]) < shortest:
            shortest = len(vectors[_vi])
        _vi = _vi + 1
    i = 0
    while i < shortest:
        arg_row = []
        _vi = 0
        while _vi < len(vectors):
            arg_row.append(vectors[_vi][i])
            _vi = _vi + 1
        _apply_scheme_proc(proc, arg_row, ctx, None, app_node)
        i = i + 1
    return VOID_VALUE


def _prim_vector_map(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    if len(args) < 2:
        raise SchemeArityError(
            arity_mismatch_msg('vector-map', 2, None, len(args)),
            src_of(app_node))
    proc = args[0]
    vectors = []
    i = 1
    while i < len(args):
        vectors.append(_check_vector(args[i], 'vector-map', app_node, i + 1))
        i = i + 1
    shortest = len(vectors[0])
    _vi = 1
    while _vi < len(vectors):
        if len(vectors[_vi]) < shortest:
            shortest = len(vectors[_vi])
        _vi = _vi + 1
    collected = []
    i = 0
    while i < shortest:
        arg_row = []
        _vi = 0
        while _vi < len(vectors):
            arg_row.append(vectors[_vi][i])
            _vi = _vi + 1
        collected.append(_apply_scheme_proc(
            proc, arg_row, ctx, None, app_node))
        i = i + 1
    return make_vector(collected)


def register():
    register_primitive('vector?', (1, 1), _prim_vector_p,
                       doc='Return #t if obj is a vector.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('make-vector', (1, 2), _prim_make_vector,
                       doc=('(make-vector k [fill]) returns a vector of length k filled '
                            'with fill (default unspecified).  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector', (0, None), _prim_vector,
                       doc='Return a vector containing the arguments.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector-length', (1, 1), _prim_vector_length,
                       doc='Return the number of elements in vector.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector-ref', (2, 2), _prim_vector_ref,
                       doc='(vector-ref vec k) returns the kth element.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector-set!', (3, 3), _prim_vector_set,
                       doc='(vector-set! vec k obj) stores obj at index k; returns void.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector->list', (1, 3), _prim_vector_to_list,
                       doc=('(vector->list vec [start [end]]) returns a list of the '
                            'vector\'s (sliced) elements.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('list->vector', (1, 1), _prim_list_to_vector,
                       doc='Return a vector built from the list.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector-fill!', (2, 4), _prim_vector_fill,
                       doc=('(vector-fill! vec fill [start [end]]) sets each element of '
                            'the (sliced) vector to fill.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector-copy', (1, 3), _prim_vector_copy,
                       doc=('(vector-copy vec [start [end]]) returns a new vector '
                            'containing a copy of the (sliced) elements.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector-copy!', (3, 5), _prim_vector_copy_bang,
                       doc=('(vector-copy! dst at src [start [end]]) copies elements from '
                            'src[start..end] into dst starting at index at.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector-append', (0, None), _prim_vector_append,
                       doc='Return a vector that is the concatenation of its arguments.  R7RS 6.8.',
                       category=CATEGORY)
    register_primitive('vector->string', (1, 3), _prim_vector_to_string,
                       doc=('Return a string built from the (sliced) vector\'s character '
                            'elements.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('string->vector', (1, 3), _prim_string_to_vector,
                       doc=('Return a vector of the characters in the (sliced) string.  '
                            'R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector-for-each', (2, None), _prim_vector_for_each,
                       doc=('(vector-for-each proc vec1 vec2 ...) applies proc element-wise '
                            'across the vectors for effect.  R7RS 6.8.'),
                       category=CATEGORY)
    register_primitive('vector-map', (2, None), _prim_vector_map,
                       doc=('(vector-map proc vec1 vec2 ...) applies proc element-wise '
                            'across the vectors and returns a vector of results.  R7RS 6.8.'),
                       category=CATEGORY)
