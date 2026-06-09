"""String primitives.

R7RS 6.7.  Strings are mutable via a SchemeString wrapper that holds a Python
str internally and rebuilds it on mutation; all references to the same
SchemeString instance see the change.

Covers: string-length, string-ref, string=?, string<?, string<=?,
string>?, string>=?, substring, string-append, string->list,
list->string, string-copy, string-copy!, string-set!, string-fill!,
make-string, string, string->symbol, symbol->string,
string-upcase, string-downcase.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
    alloc_cons, NIL_VALUE,
    is_cons, is_nil, is_string, is_character, is_integer, is_symbol,
    as_string, as_character, as_integer, as_symbol,
    make_string, make_character, make_integer, make_symbol, make_boolean,
    src_of,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'strings'


def _check_string(v, name, app_node, idx=1):
    if not is_string(v):
        raise SchemeTypeError(
            '%s: argument %d must be a string' % (name, idx), src_of(app_node))
    return as_string(v)


def _check_index(v, name, length, app_node):
    if not is_integer(v):
        raise SchemeTypeError(
            '%s: index must be an integer' % name, src_of(app_node))
    k = as_integer(v)
    if k < 0 or k >= length:
        raise SchemeTypeError(
            '%s: index %d out of range' % (name, k), src_of(app_node))
    return k


def _prim_string_length(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-length', app_node)
    return make_integer(len(s))


def _prim_string_ref(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-ref', app_node)
    k = _check_index(args[1], 'string-ref', len(s), app_node)
    return make_character(s[k])


def _string_compare(name, args, app_node, op):
    if len(args) < 2:
        return make_boolean(True)
    prev = _check_string(args[0], name, app_node, 1)
    i = 1
    while i < len(args):
        cur = _check_string(args[i], name, app_node, i + 1)
        if not op(prev, cur):
            return make_boolean(False)
        prev = cur
        i = i + 1
    return make_boolean(True)


def _prim_string_eq(ctx, env, args, app_node):
    return _string_compare('string=?', args, app_node, lambda a, b: a == b)


def _prim_string_lt(ctx, env, args, app_node):
    return _string_compare('string<?', args, app_node, lambda a, b: a < b)


def _prim_string_le(ctx, env, args, app_node):
    return _string_compare('string<=?', args, app_node, lambda a, b: a <= b)


def _prim_string_gt(ctx, env, args, app_node):
    return _string_compare('string>?', args, app_node, lambda a, b: a > b)


def _prim_string_ge(ctx, env, args, app_node):
    return _string_compare('string>=?', args, app_node, lambda a, b: a >= b)


def _prim_substring(ctx, env, args, app_node):
    s = _check_string(args[0], 'substring', app_node)
    start = args[1]
    end = args[2] if len(args) >= 3 else None
    if not is_integer(start):
        raise SchemeTypeError(
            'substring: start must be an integer', src_of(app_node))
    start_i = as_integer(start)
    if end is None:
        end_i = len(s)
    else:
        if not is_integer(end):
            raise SchemeTypeError(
                'substring: end must be an integer', src_of(app_node))
        end_i = as_integer(end)
    if start_i < 0 or end_i > len(s) or start_i > end_i:
        raise SchemeTypeError(
            'substring: start/end out of range', src_of(app_node))
    buf = ''
    j = start_i
    while j < end_i:
        buf = buf + s[j]
        j = j + 1
    return make_string(buf)


def _prim_string_append(ctx, env, args, app_node):
    parts = []
    i = 0
    while i < len(args):
        parts.append(_check_string(args[i], 'string-append', app_node, i + 1))
        i = i + 1
    result = ''
    j = 0
    while j < len(parts):
        result = result + parts[j]
        j = j + 1
    return make_string(result)


def _prim_string_to_list(ctx, env, args, app_node):
    s = _check_string(args[0], 'string->list', app_node)
    start = 0
    end = len(s)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'string->list: start must be an integer', src_of(app_node))
        start = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'string->list: end must be an integer', src_of(app_node))
        end = as_integer(args[2])
    if start < 0 or end > len(s) or start > end:
        raise SchemeTypeError(
            'string->list: start/end out of range', src_of(app_node))
    result = NIL_VALUE
    i = end - 1
    while i >= start:
        result = alloc_cons(make_character(s[i]), result, None)
        i = i - 1
    return result


def _prim_list_to_string(ctx, env, args, app_node):
    v = args[0]
    chars = []
    cur = v
    while is_cons(cur):
        c = cur.car
        if not is_character(c):
            raise SchemeTypeError(
                'list->string: list elements must be characters',
                src_of(app_node))
        chars.append(as_character(c))
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            'list->string: argument must be a proper list', src_of(app_node))
    result = ''
    j = 0
    while j < len(chars):
        result = result + chars[j]
        j = j + 1
    return make_string(result)


def _prim_string_copy(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-copy', app_node)
    start_i = 0
    end_i = len(s)
    if len(args) >= 2:
        if not is_integer(args[1]):
            raise SchemeTypeError(
                'string-copy: start must be an integer', src_of(app_node))
        start_i = as_integer(args[1])
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'string-copy: end must be an integer', src_of(app_node))
        end_i = as_integer(args[2])
    if start_i < 0 or end_i > len(s) or start_i > end_i:
        raise SchemeTypeError(
            'string-copy: start/end out of range', src_of(app_node))
    buf = ''
    j = start_i
    while j < end_i:
        buf = buf + s[j]
        j = j + 1
    return make_string(buf)


def _prim_make_string(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'make-string: length must be an integer', src_of(app_node))
    k = as_integer(args[0])
    if k < 0:
        raise SchemeTypeError(
            'make-string: length must be non-negative', src_of(app_node))
    fill = ' '
    if len(args) >= 2:
        c = args[1]
        if not is_character(c):
            raise SchemeTypeError(
                'make-string: fill must be a character', src_of(app_node))
        fill = as_character(c)
    buf = ''
    j = 0
    while j < k:
        buf = buf + fill
        j = j + 1
    return make_string(buf)


def _prim_string(ctx, env, args, app_node):
    chars = []
    i = 0
    while i < len(args):
        c = args[i]
        if not is_character(c):
            raise SchemeTypeError(
                'string: argument %d must be a character' % (i + 1),
                src_of(app_node))
        chars.append(as_character(c))
        i = i + 1
    result = ''
    j = 0
    while j < len(chars):
        result = result + chars[j]
        j = j + 1
    return make_string(result)


def _prim_string_to_symbol(ctx, env, args, app_node):
    s = _check_string(args[0], 'string->symbol', app_node)
    return make_symbol(s)


def _prim_symbol_to_string(ctx, env, args, app_node):
    v = args[0]
    if not is_symbol(v):
        raise SchemeTypeError(
            'symbol->string: argument must be a symbol', src_of(app_node))
    return make_string(as_symbol(v))


def _prim_string_upcase(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-upcase', app_node)
    return make_string(s.upper())


def _prim_string_downcase(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-downcase', app_node)
    return make_string(s.lower())


def _prim_string_foldcase(ctx, env, args, app_node):
    s = _check_string(args[0], 'string-foldcase', app_node)
    return make_string(s.casefold())


def _string_compare_ci(name, args, app_node, op):
    if len(args) < 2:
        return make_boolean(True)
    prev = _check_string(args[0], name, app_node, 1).casefold()
    i = 1
    while i < len(args):
        cur = _check_string(args[i], name, app_node, i + 1).casefold()
        if not op(prev, cur):
            return make_boolean(False)
        prev = cur
        i = i + 1
    return make_boolean(True)


def _prim_string_ci_eq(ctx, env, args, app_node):
    return _string_compare_ci('string-ci=?', args, app_node, lambda a, b: a == b)


def _prim_string_ci_lt(ctx, env, args, app_node):
    return _string_compare_ci('string-ci<?', args, app_node, lambda a, b: a < b)


def _prim_string_ci_le(ctx, env, args, app_node):
    return _string_compare_ci('string-ci<=?', args, app_node, lambda a, b: a <= b)


def _prim_string_ci_gt(ctx, env, args, app_node):
    return _string_compare_ci('string-ci>?', args, app_node, lambda a, b: a > b)


def _prim_string_ci_ge(ctx, env, args, app_node):
    return _string_compare_ci('string-ci>=?', args, app_node, lambda a, b: a >= b)


def _prim_string_map(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    from pyscheme.AST import is_character, as_character
    if len(args) < 2:
        raise SchemeTypeError(
            'string-map: at least one string is required', src_of(app_node))
    proc = args[0]
    strings = []
    i = 1
    while i < len(args):
        strings.append(_check_string(args[i], 'string-map', app_node, i + 1))
        i = i + 1
    shortest = len(strings[0])
    _si = 1
    while _si < len(strings):
        if len(strings[_si]) < shortest:
            shortest = len(strings[_si])
        _si = _si + 1
    result = ''
    i = 0
    while i < shortest:
        arg_row = []
        _si = 0
        while _si < len(strings):
            arg_row.append(make_character(strings[_si][i]))
            _si = _si + 1
        ch_val = _apply_scheme_proc(proc, arg_row, ctx, None, app_node)
        if not is_character(ch_val):
            raise SchemeTypeError(
                'string-map: proc must return a character', src_of(app_node))
        result = result + as_character(ch_val)
        i = i + 1
    return make_string(result)


def _prim_string_set_bang(ctx, env, args, app_node):
    from pyscheme.AST import VOID_VALUE, is_character, as_character
    s = args[0]
    _check_string(s, 'string-set!', app_node, 1)
    if s.immutable:
        raise SchemeTypeError(
            'string-set!: argument is an immutable literal', src_of(app_node))
    if not is_integer(args[1]):
        raise SchemeTypeError(
            'string-set!: index must be an integer', src_of(app_node))
    k = as_integer(args[1])
    if not is_character(args[2]):
        raise SchemeTypeError(
            'string-set!: third argument must be a character', src_of(app_node))
    ch = as_character(args[2])
    n = len(s._s)
    if k < 0 or k >= n:
        raise SchemeTypeError(
            'string-set!: index ' +
            str(k) + ' out of range for string of length ' + str(n),
            src_of(app_node))
    buf = ''
    j = 0
    while j < n:
        if j == k:
            buf = buf + ch
        else:
            buf = buf + s._s[j]
        j = j + 1
    s._s = buf
    return VOID_VALUE


def _prim_string_fill_bang(ctx, env, args, app_node):
    from pyscheme.AST import VOID_VALUE, is_character, as_character
    s = args[0]
    _check_string(s, 'string-fill!', app_node, 1)
    if s.immutable:
        raise SchemeTypeError(
            'string-fill!: argument is an immutable literal', src_of(app_node))
    if not is_character(args[1]):
        raise SchemeTypeError(
            'string-fill!: second argument must be a character', src_of(app_node))
    ch = as_character(args[1])
    n = len(s._s)
    start = 0
    end = n
    if len(args) >= 3:
        if not is_integer(args[2]):
            raise SchemeTypeError(
                'string-fill!: start must be an integer', src_of(app_node))
        start = as_integer(args[2])
    if len(args) >= 4:
        if not is_integer(args[3]):
            raise SchemeTypeError(
                'string-fill!: end must be an integer', src_of(app_node))
        end = as_integer(args[3])
    if start < 0 or end > n or start > end:
        raise SchemeTypeError(
            'string-fill!: range out of bounds', src_of(app_node))
    buf = ''
    j = 0
    while j < n:
        if j >= start and j < end:
            buf = buf + ch
        else:
            buf = buf + s._s[j]
        j = j + 1
    s._s = buf
    return VOID_VALUE


def _prim_string_copy_bang(ctx, env, args, app_node):
    from pyscheme.AST import VOID_VALUE
    to = args[0]
    _check_string(to, 'string-copy!', app_node, 1)
    if not is_integer(args[1]):
        raise SchemeTypeError(
            'string-copy!: at must be an integer', src_of(app_node))
    at = as_integer(args[1])
    frm = args[2]
    _check_string(frm, 'string-copy!', app_node, 3)
    n_from = len(frm._s)
    start = 0
    end = n_from
    if len(args) >= 4:
        if not is_integer(args[3]):
            raise SchemeTypeError(
                'string-copy!: start must be an integer', src_of(app_node))
        start = as_integer(args[3])
    if len(args) >= 5:
        if not is_integer(args[4]):
            raise SchemeTypeError(
                'string-copy!: end must be an integer', src_of(app_node))
        end = as_integer(args[4])
    if start < 0 or end > n_from or start > end:
        raise SchemeTypeError(
            'string-copy!: source range out of bounds', src_of(app_node))
    chunk_len = end - start
    n_to = len(to._s)
    if at < 0 or at + chunk_len > n_to:
        raise SchemeTypeError(
            'string-copy!: destination range out of bounds', src_of(app_node))
    buf = ''
    j = 0
    while j < n_to:
        if j >= at and j < at + chunk_len:
            buf = buf + frm._s[start + (j - at)]
        else:
            buf = buf + to._s[j]
        j = j + 1
    to._s = buf
    return VOID_VALUE


def _prim_string_for_each(ctx, env, args, app_node):
    from pyscheme.primitives.meta import _apply_scheme_proc
    from pyscheme.AST import VOID_VALUE
    if len(args) < 2:
        raise SchemeTypeError(
            'string-for-each: at least one string is required',
            src_of(app_node))
    proc = args[0]
    strings = []
    i = 1
    while i < len(args):
        strings.append(
            _check_string(args[i], 'string-for-each', app_node, i + 1))
        i = i + 1
    shortest = len(strings[0])
    _si = 1
    while _si < len(strings):
        if len(strings[_si]) < shortest:
            shortest = len(strings[_si])
        _si = _si + 1
    i = 0
    while i < shortest:
        arg_row = []
        _si = 0
        while _si < len(strings):
            arg_row.append(make_character(strings[_si][i]))
            _si = _si + 1
        _apply_scheme_proc(proc, arg_row, ctx, None, app_node)
        i = i + 1
    return VOID_VALUE


def register():
    register_primitive('string-length', (1, 1), _prim_string_length,
                       doc='Return the number of characters in the string.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ref', (2, 2), _prim_string_ref,
                       doc='(string-ref string k) returns the kth character.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string=?', (2, None), _prim_string_eq,
                       doc='Return #t if all string arguments compare equal.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string<?', (2, None), _prim_string_lt,
                       doc='Return #t if the strings are in strictly ascending order.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string<=?', (2, None), _prim_string_le,
                       doc='Return #t if the strings are in non-descending order.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string>?', (2, None), _prim_string_gt,
                       doc='Return #t if the strings are in strictly descending order.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string>=?', (2, None), _prim_string_ge,
                       doc='Return #t if the strings are in non-ascending order.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('substring', (2, 3), _prim_substring,
                       doc=('(substring string start [end]) returns the substring from '
                            'index start (inclusive) to end (exclusive).  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string-append', (0, None), _prim_string_append,
                       doc='Concatenate string arguments into a new string.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string->list', (1, 3), _prim_string_to_list,
                       doc='(string->list string [start [end]]) returns a list of the characters in string[start:end].  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('list->string', (1, 1), _prim_list_to_string,
                       doc='Return a string built from the characters in the list.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-copy', (1, 3), _prim_string_copy,
                       doc=('(string-copy string [start [end]]) returns a copy of (a slice '
                            'of) the string.  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('make-string', (1, 2), _prim_make_string,
                       doc=('(make-string k [char]) returns a string of length k filled '
                            'with char (default space).  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string', (0, None), _prim_string,
                       doc='Return a string composed of the character arguments.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string->symbol', (1, 1), _prim_string_to_symbol,
                       doc='Return a symbol whose name is the string.  R7RS 6.5.',
                       category=CATEGORY)
    register_primitive('symbol->string', (1, 1), _prim_symbol_to_string,
                       doc='Return the symbol\'s name as a string.  R7RS 6.5.',
                       category=CATEGORY)
    register_primitive('string-upcase', (1, 1), _prim_string_upcase,
                       doc='Return the string with each character upcased.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-downcase', (1, 1), _prim_string_downcase,
                       doc='Return the string with each character downcased.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-foldcase', (1, 1), _prim_string_foldcase,
                       doc='Return the string with Unicode full case folding applied.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ci=?', (2, None), _prim_string_ci_eq,
                       doc='Case-insensitive string=?.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ci<?', (2, None), _prim_string_ci_lt,
                       doc='Case-insensitive string<?.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ci<=?', (2, None), _prim_string_ci_le,
                       doc='Case-insensitive string<=?.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ci>?', (2, None), _prim_string_ci_gt,
                       doc='Case-insensitive string>?.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-ci>=?', (2, None), _prim_string_ci_ge,
                       doc='Case-insensitive string>=?.  R7RS 6.7.',
                       category=CATEGORY)
    register_primitive('string-map', (2, None), _prim_string_map,
                       doc=('(string-map proc str1 str2 ...) returns a string built by '
                            'applying proc element-wise across the strings.  proc must '
                            'return a character.  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string-for-each', (2, None), _prim_string_for_each,
                       doc=('(string-for-each proc str1 str2 ...) applies proc element-'
                            'wise for effect; returns an unspecified value.  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string-set!', (3, 3), _prim_string_set_bang,
                       doc=('(string-set! string k char) sets character k of string to char in place.  '
                            'All references to the string see the change.  R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string-fill!', (2, 4), _prim_string_fill_bang,
                       doc=('(string-fill! string char [start [end]]) fills string[start..end) with char.  '
                            'R7RS 6.7.'),
                       category=CATEGORY)
    register_primitive('string-copy!', (3, 5), _prim_string_copy_bang,
                       doc=('(string-copy! to at from [start [end]]) copies from[start..end) into to '
                            'starting at index at.  R7RS 6.7.'),
                       category=CATEGORY)
