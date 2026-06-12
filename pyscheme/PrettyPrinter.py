"""Pretty printer for CEK values and quoted data.

Converts values produced by the CEK evaluator into human-readable
Scheme surface syntax.  Mirrors the lexer's literal forms so that the
printed output can be re-read by the parser where applicable.

Atoms are tagged tuples; pairs are ConsCell instances.

    (INTEGER, 42, src)              ->  "42"
    (REAL, 3.14, src)               ->  "3.14"
    (RATIONAL, 3, 4, src)           ->  "3/4"
    (BOOLEAN, True, src)            ->  "#t"
    (CHARACTER, 'a', src)           ->  "#\\a"
    (STRING, "hi", src)             ->  '"hi"'
    (CLOSURE, ...)                  ->  "#<procedure>"
    (PRIMITIVE, name, fn)           ->  "#<primitive name>"
    (SYMBOL, "foo", src)            ->  "foo"
    (NIL,)                          ->  "()"
    ConsCell(car, cdr, src)         ->  "(a b c)" or "(a . b)"
"""
import math as _math

from pyscheme.AST import (
    is_cons, is_nil, is_void, is_boolean, is_integer, is_real, is_rational,
    is_complex, is_exact_complex, is_character, is_string, is_symbol,
    is_closure, is_primitive, is_case_closure, is_promise, is_multi_values,
    is_record, is_parameter, is_error_object, is_continuation,
    is_syntax_transformer, is_environment,
    is_record_accessor, is_record_mutator, is_vector, is_bytevector,
    is_port, is_eof,
    as_boolean, as_integer, as_real, as_character, as_string, as_symbol,
    as_rational_num, as_rational_den, as_complex_real, as_complex_imag,
    as_exact_complex_real, as_exact_complex_imag,
    as_primitive_name, as_promise_is_done, as_multi_values_list,
    as_record_type, as_record_fields, as_record_type_name,
    as_error_object_message, as_error_object_irritants,
    as_syntax_transformer_name, as_vector_items, as_bytevector_items,
    make_string,
    INTEGER, REAL, RATIONAL, BOOLEAN, CHARACTER, STRING, CLOSURE, SYMBOL,
    NIL_VALUE, gensym_display_name,
)


def _format_float(f):
    """Render a Python float as R7RS surface syntax."""
    if _math.isnan(f):
        return '+nan.0'
    if f == float('inf'):
        return '+inf.0'
    if f == float('-inf'):
        return '-inf.0'
    s = repr(f)
    if '.' not in s and 'e' not in s:
        s = s + '.0'
    return s


_CHAR_NAMES_REVERSE = {
    ' ':    'space',
    '\n':   'newline',
    '\t':   'tab',
    '\r':   'return',
    '\0':   'null',
    '\a':   'alarm',
    '\b':   'backspace',
    '\x7f': 'delete',
    '\x1b': 'escape',
}


def _has_cycle(val):
    """Return True if val contains any reference cycle (cons or vector).

    Uses enter/exit events to track nodes currently on the DFS path (gray
    set).  A node is a cycle only if it is encountered while already on the
    current path.  Shared-but-acyclic structure (e.g. (list x x)) is not a
    false positive: after x is fully explored it leaves the gray set, so a
    second encounter does not trigger the cycle check."""
    gray = set()
    stack = [(val, False)]
    while stack:
        v, exiting = stack.pop()
        if not (is_cons(v) or is_vector(v)):
            continue
        k = id(v)
        if exiting:
            gray.discard(k)
            continue
        if k in gray:
            return True
        gray.add(k)
        stack.append((v, True))
        if is_cons(v):
            stack.append((v.car, False))
            stack.append((v.cdr, False))
        else:
            items = as_vector_items(v)
            i = 0
            while i < len(items):
                stack.append((items[i], False))
                i = i + 1
    return False


def _render_structure(val, render_leaf):
    """Render an acyclic cons/vector structure to a string iteratively, using an
    explicit task stack so depth is heap-bounded -- no Python recursion on the
    car-chain or on vector nesting (the cdr-spine is already a loop).  render_leaf(v)
    renders any NON-cons/non-vector value; write and display differ only in that
    leaf renderer.  Nested cons/vector are expanded here, so list/vector elements
    inherit the caller's mode.  The caller guarantees val is acyclic (cyclic values
    are routed to the datum-label renderer first), so the walk terminates."""
    out = []
    # task stack entries: ('v', value) = render a value; ('e', text) = emit literal
    stack = [('v', val)]
    while stack:
        kind, x = stack.pop()
        if kind == 'e':
            out.append(x)
            continue
        if is_cons(x):
            elems = []
            cur = x
            while is_cons(cur):
                elems.append(cur.car)
                cur = cur.cdr
            # push in reverse so tasks pop in print order: ( e0 e1 ... [. tail] )
            stack.append(('e', ')'))
            if not is_nil(cur):
                stack.append(('v', cur))
                stack.append(('e', ' . '))
            i = len(elems) - 1
            while i >= 0:
                stack.append(('v', elems[i]))
                if i > 0:
                    stack.append(('e', ' '))
                i = i - 1
            stack.append(('e', '('))
        elif is_vector(x):
            items = as_vector_items(x)
            stack.append(('e', ')'))
            i = len(items) - 1
            while i >= 0:
                stack.append(('v', items[i]))
                if i > 0:
                    stack.append(('e', ' '))
                i = i - 1
            stack.append(('e', '#('))
        else:
            out.append(render_leaf(x))
    return ''.join(out)


def pretty_print(val):
    """Render a CEK value or quoted datum as Scheme surface syntax."""
    if is_cons(val) or is_vector(val):
        if _has_cycle(val):
            return pretty_print_shared(val)
        return _render_structure(val, pretty_print)
    if is_nil(val):
        return '()'
    if is_void(val):
        return '#<void>'
    if is_integer(val):
        return str(as_integer(val))
    if is_real(val):
        return _format_float(as_real(val))
    if is_rational(val):
        return str(as_rational_num(val)) + '/' + str(as_rational_den(val))
    if is_complex(val):
        re = as_complex_real(val)
        im = as_complex_imag(val)
        re_s = _format_float(re)
        im_s = _format_float(im)
        if _math.isnan(im) or im >= 0:
            return re_s + '+' + im_s + 'i'
        return re_s + im_s + 'i'
    if is_exact_complex(val):
        re_s = pretty_print(as_exact_complex_real(val))
        im_v = as_exact_complex_imag(val)
        im_s = pretty_print(im_v)
        im_py = as_integer(im_v) if is_integer(im_v) else as_rational_num(im_v)
        if im_py >= 0:
            return re_s + '+' + im_s + 'i'
        return re_s + im_s + 'i'
    if is_boolean(val):
        if as_boolean(val):
            return '#t'
        return '#f'
    if is_character(val):
        c = as_character(val)
        if c in _CHAR_NAMES_REVERSE:
            return '#\\' + _CHAR_NAMES_REVERSE[c]
        cp = ord(c)
        if 0x20 <= cp <= 0x7E:
            return '#\\' + c
        return '#\\x' + format(cp, 'X')
    if is_string(val):
        return '"' + _escape_string(as_string(val)) + '"'
    if is_closure(val):
        return '#<procedure>'
    if is_case_closure(val):
        return '#<procedure>'
    if is_record_accessor(val) or is_record_mutator(val):
        return '#<procedure>'
    if is_primitive(val):
        return '#<primitive ' + as_primitive_name(val) + '>'
    if is_promise(val):
        if as_promise_is_done(val):
            return '#<promise forced>'
        return '#<promise>'
    if is_multi_values(val):
        vs = as_multi_values_list(val)
        if len(vs) == 0:
            return ''
        parts = []
        i = 0
        while i < len(vs):
            parts.append(pretty_print(vs[i]))
            i = i + 1
        return '#<values ' + ' '.join(parts) + '>'
    if is_record(val):
        rt = as_record_type(val)
        fs = as_record_fields(val)
        tname = as_record_type_name(rt)
        if len(fs) == 0:
            return '#<' + tname + '>'
        parts = []
        i = 0
        while i < len(fs):
            parts.append(pretty_print(fs[i]))
            i = i + 1
        return '#<' + tname + ' ' + ' '.join(parts) + '>'
    if is_parameter(val):
        return '#<parameter>'
    if is_continuation(val):
        return '#<continuation>'
    if is_environment(val):
        return '#<environment>'
    if is_vector(val):
        items = as_vector_items(val)
        parts = []
        i = 0
        while i < len(items):
            parts.append(pretty_print(items[i]))
            i = i + 1
        return '#(' + ' '.join(parts) + ')'
    if is_bytevector(val):
        items = as_bytevector_items(val)
        parts = []
        i = 0
        while i < len(items):
            parts.append(str(items[i]))
            i = i + 1
        return '#u8(' + ' '.join(parts) + ')'
    if is_port(val):
        return '#<port>'
    if is_eof(val):
        return '#<eof>'
    if is_syntax_transformer(val):
        return '#<syntax-rules ' + as_syntax_transformer_name(val) + '>'
    if is_error_object(val):
        msg = as_error_object_message(val)
        irs = as_error_object_irritants(val)
        if len(irs) == 0:
            return '#<error-object "' + _escape_string(msg) + '">'
        parts = []
        i = 0
        while i < len(irs):
            parts.append(pretty_print(irs[i]))
            i = i + 1
        return ('#<error-object "' + _escape_string(msg) + '" (' +
                ' '.join(parts) + ')>')
    if is_symbol(val):
        name = _display_symbol_name(as_symbol(val))
        if _needs_vertical_bars(name):
            return '|' + _escape_symbol_name(name) + '|'
        return name
    return repr(val)


def _shared_scan(val, counts):
    """First pass for write-shared: count how many times each mutable object
    (cons cell, vector) is reachable.  counts maps id -> [val, n].  Iterative
    (explicit stack) so deep or long structures don't overflow the Python stack.
    Pushes cdr-then-car (and vector items in reverse) so first-encounter order is
    the same pre-order DFS as the original recursion -- this keeps datum-label
    numbering (assigned below by counts insertion order) byte-for-byte identical."""
    stack = [val]
    while stack:
        v = stack.pop()
        if is_cons(v):
            k = id(v)
            if k in counts:
                counts[k][1] = counts[k][1] + 1
                continue
            counts[k] = [v, 1]
            stack.append(v.cdr)
            stack.append(v.car)
        elif is_vector(v):
            k = id(v)
            if k in counts:
                counts[k][1] = counts[k][1] + 1
                continue
            counts[k] = [v, 1]
            items = as_vector_items(v)
            i = len(items) - 1
            while i >= 0:
                stack.append(items[i])
                i = i - 1


def _shared_render(val, labels, next_label, seen):
    """Render val with datum labels for any object that appears more than once
    in the structure.  labels: id -> int; next_label: [int] (mutable counter);
    seen: set of ids already emitted with #n=.  Iterative (explicit task stack)
    so depth is heap-bounded.  A node's #n= prefix is emitted and its id added to
    `seen` when the node is first popped -- BEFORE its children are pushed -- so a
    back-reference within the children renders as #n#, matching the recursion."""
    out = []
    # task stack entries: ('v', value) = render; ('e', text) = emit literal.
    stack = [('v', val)]
    while stack:
        kind, x = stack.pop()
        if kind == 'e':
            out.append(x)
            continue
        if is_cons(x):
            k = id(x)
            if k in labels:
                n = labels[k]
                if k in seen:
                    out.append('#' + str(n) + '#')
                    continue
                seen.add(k)
                out.append('#' + str(n) + '=')
            # Walk the spine with the same break logic as the original: stop at a
            # mid-spine labeled cell or a next cell already printed (-> shared tail).
            items = []
            cur = x
            tail = None
            while is_cons(cur):
                ck = id(cur)
                if cur is not x and ck in labels:
                    tail = cur
                    break
                items.append(cur.car)
                cur = cur.cdr
                if is_cons(cur) and id(cur) in seen:
                    tail = cur
                    break
            if tail is None and not is_nil(cur):
                tail = cur
            stack.append(('e', ')'))
            if tail is not None:
                stack.append(('v', tail))
                stack.append(('e', ' . '))
            i = len(items) - 1
            while i >= 0:
                stack.append(('v', items[i]))
                if i > 0:
                    stack.append(('e', ' '))
                i = i - 1
            stack.append(('e', '('))
        elif is_vector(x):
            k = id(x)
            if k in labels:
                n = labels[k]
                if k in seen:
                    out.append('#' + str(n) + '#')
                    continue
                seen.add(k)
                out.append('#' + str(n) + '=')
            items_list = as_vector_items(x)
            stack.append(('e', ')'))
            i = len(items_list) - 1
            while i >= 0:
                stack.append(('v', items_list[i]))
                if i > 0:
                    stack.append(('e', ' '))
                i = i - 1
            stack.append(('e', '#('))
        else:
            out.append(pretty_print(x))
    return ''.join(out)


def pretty_print_shared(val):
    """Like pretty_print but uses #n=/#n# datum labels for shared structure.
    This is the rendering used by write-shared (R7RS §6.13)."""
    counts = {}
    _shared_scan(val, counts)
    labels = {}
    next_label = [0]
    k = id(val)
    for obj_id in counts:
        if counts[obj_id][1] > 1:
            labels[obj_id] = next_label[0]
            next_label[0] = next_label[0] + 1
    seen = set()
    return _shared_render(val, labels, next_label, seen)


def _display_symbol_name(name):
    """Gensym-stripped name for display (see AST.gensym_display_name)."""
    return gensym_display_name(name)


def _is_safe_symbol_initial(c):
    return (('a' <= c <= 'z') or ('A' <= c <= 'Z') or
            c in '!$%&*/:<=>?^_~')


def _is_safe_symbol_subsequent(c):
    return _is_safe_symbol_initial(c) or ('0' <= c <= '9') or c in '+-@.'


def _needs_vertical_bars(name):
    if not name:
        return True
    if not _is_safe_symbol_initial(name[0]) and name[0] not in '+-@.':
        return True
    i = 0
    while i < len(name):
        if not _is_safe_symbol_subsequent(name[i]):
            return True
        i = i + 1
    return False


def _escape_symbol_name(name):
    result = []
    i = 0
    while i < len(name):
        c = name[i]
        if c == '|':
            result.append('\\|')
        elif c == '\\':
            result.append('\\\\')
        else:
            result.append(c)
        i = i + 1
    return ''.join(result)


def _escape_string(s):
    result = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\n':
            result.append('\\n')
        elif c == '\t':
            result.append('\\t')
        elif c == '\r':
            result.append('\\r')
        elif c == '\\':
            result.append('\\\\')
        elif c == '"':
            result.append('\\"')
        else:
            result.append(c)
        i = i + 1
    return ''.join(result)


if __name__ == '__main__':
    samples = [
        (INTEGER, 42),
        (INTEGER, -7),
        (REAL, 3.14),
        (REAL, 3.0),
        (REAL, 1e10),
        (RATIONAL, 3, 4),
        (BOOLEAN, True),
        (BOOLEAN, False),
        (CHARACTER, 'a'),
        (CHARACTER, ' '),
        (CHARACTER, '\n'),
        make_string('hello'),
        make_string('line1\nline2'),
        make_string('she said "hi"'),
        (CLOSURE, 'x', (None,), None),
        (SYMBOL, 'foo'),
        [(SYMBOL, 'a'), (SYMBOL, 'b'), (INTEGER, 3)],
    ]
    i = 0
    while i < len(samples):
        v = samples[i]
        print('%-60r  ->  %s' % (v, pretty_print(v)))
        i = i + 1
