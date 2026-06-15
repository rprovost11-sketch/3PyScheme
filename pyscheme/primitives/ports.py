"""Port primitives.

R7RS 6.13 - input and output.  Covers the practical subset most user
code needs: file and string ports, char-level read / peek, eof-object,
read (one S-expression), write-char / write-string, plus optional port
arguments on display / write / newline.

Port semantics:
   open-input-file  - opens a file, slurps its contents into the port's
                      buffer (so read/read-char operate on a snapshot).
                      The file handle is held only for the close call.
   open-input-string - wraps a string as an input port.
   open-output-file - opens a file for writing; output goes to the file
                      handle directly.
   open-output-string - accumulates chunks; get-output-string returns
                        the joined result.

Streaming-from-stdin / interactive line-by-line reads are not supported
by this first cut: open-input-file reads the whole file up front.

current-input-port and current-output-port are R7RS parameters; the
Listener installs sys.stdin / sys.stdout as their initial values.
"""

import io as _pyio

from pyscheme.primitives import register_primitive, parse_start_end
from pyscheme.AST import (
    alloc_cons, NIL_VALUE, VOID_VALUE,
    is_cons, is_string, is_character, is_integer, is_port, is_eof,
    is_bytevector,
    as_string, as_character, as_integer, as_port, as_bytevector_items,
    make_port, make_eof, make_string, make_character, make_integer,
    make_boolean, make_parameter, make_bytevector,
    Port,
    as_primitive_fn,
    src_of,
)
from pyscheme.Environment import SchemeTypeError, SchemeFileError


CATEGORY = 'ports'


# Module-level handles to the current input / output / error parameters.
# Initialised lazily on first use.  _current_*_default holds the initial
# default port value so _get_current_output can detect whether the parameter
# has been overridden by user code (parameterize) without depending on
# sys.stdout identity, which changes when the test runner redirects stdout.
_current_input_param = [None]
_current_output_param = [None]
_current_error_param = [None]
_current_input_default = [None]
_current_output_default = [None]
_current_error_default = [None]


def reset_current_port_params():
    """Reset all three current-port parameters to uninitialized.  Called by
    Interpreter.reboot() so each fresh session reinitializes from the real
    sys.stdout/stdin/stderr rather than a file that may have been closed."""
    _current_input_param[0] = None
    _current_output_param[0] = None
    _current_error_param[0] = None
    _current_input_default[0] = None
    _current_output_default[0] = None
    _current_error_default[0] = None


def _stdin_port():
    import sys
    p = Port('', is_input=True, is_text=True, file_h=sys.stdin, name='<stdin>',
             from_stdin=True)
    return make_port(p)


def _ensure_stdin_loaded(p):
    """The first read from the <stdin> port slurps all of sys.stdin into the
    port buffer -- the same read-it-all-up-front model open-input-file uses.
    Done lazily (on first read, not at port creation) so merely fetching
    (current-input-port) does not block, and one-shot via the from_stdin flag.
    This lets a program read its data from redirected stdin (e.g. `prog <
    input`), which the benchmark harness relies on."""
    if p.from_stdin:
        import sys
        try:
            data = sys.stdin.read()
        except Exception:
            data = ''
        p.buf = data if data is not None else ''
        p.pos = 0
        p.from_stdin = False


def _stdout_port():
    import sys
    p = Port([], is_input=False, is_text=True,
             file_h=sys.stdout, name='<stdout>')
    return make_port(p)


def _stderr_port():
    import sys
    p = Port([], is_input=False, is_text=True,
             file_h=sys.stderr, name='<stderr>')
    return make_port(p)


def _get_current_input(ctx):
    if _current_input_param[0] is None:
        _current_input_param[0] = make_parameter(_stdin_port(), None)
    from pyscheme.AST import as_parameter_value
    return as_parameter_value(_current_input_param[0])


def _get_current_output(ctx):
    if _current_output_param[0] is None:
        default_port = _stdout_port()
        _current_output_default[0] = default_port
        _current_output_param[0] = make_parameter(default_port, None)
    from pyscheme.AST import as_parameter_value
    p = as_parameter_value(_current_output_param[0])
    if ctx is not None and ctx.outStrm is not None:
        if p is _current_output_default[0]:
            # Capture path (test harness / REPL redirect): return ONE stable port
            # object per ctx so (current-output-port) is eq?-consistent across
            # calls, refreshing its underlying stream (the harness hands a fresh
            # outStrm per evaluated form).  Allocating a new port each call would
            # break (eq? (current-output-port) saved) -- see R7RS 6.13.1.
            cap_val = getattr(ctx, '_capture_out_port', None)
            if cap_val is None:
                cap_val = make_port(Port([], is_input=False, is_text=True,
                                         file_h=ctx.outStrm, name='<capture>'))
                ctx._capture_out_port = cap_val
            else:
                as_port(cap_val).file_h = ctx.outStrm
            return cap_val
    return p


def _check_port(v, name, app_node, idx=1):
    if not is_port(v):
        raise SchemeTypeError(
            '%s: argument %d must be a port' % (name, idx), src_of(app_node))
    p = as_port(v)
    if not p.is_open:
        raise SchemeTypeError(
            '%s: port is closed' % name, src_of(app_node))
    return p


def _check_input_port(v, name, app_node, idx=1):
    p = _check_port(v, name, app_node, idx)
    if not p.is_input:
        raise SchemeTypeError(
            '%s: argument %d must be an input port' % (name, idx),
            src_of(app_node))
    # First actual read from the <stdin> port fills its buffer from sys.stdin.
    _ensure_stdin_loaded(p)
    return p


def _check_output_port(v, name, app_node, idx=1):
    p = _check_port(v, name, app_node, idx)
    if p.is_input:
        raise SchemeTypeError(
            '%s: argument %d must be an output port' % (name, idx),
            src_of(app_node))
    return p


def _check_textual_input(v, name, app_node, idx=1):
    p = _check_input_port(v, name, app_node, idx)
    if not p.is_text:
        raise SchemeTypeError(
            '%s: argument %d must be a textual input port' % (name, idx),
            src_of(app_node))
    return p


def _check_binary_input(v, name, app_node, idx=1):
    p = _check_input_port(v, name, app_node, idx)
    if p.is_text:
        raise SchemeTypeError(
            '%s: argument %d must be a binary input port' % (name, idx),
            src_of(app_node))
    return p


def _check_textual_output(v, name, app_node, idx=1):
    p = _check_output_port(v, name, app_node, idx)
    if not p.is_text:
        raise SchemeTypeError(
            '%s: argument %d must be a textual output port' % (name, idx),
            src_of(app_node))
    return p


def _check_binary_output(v, name, app_node, idx=1):
    p = _check_output_port(v, name, app_node, idx)
    if p.is_text:
        raise SchemeTypeError(
            '%s: argument %d must be a binary output port' % (name, idx),
            src_of(app_node))
    return p


# ── Predicates ─────────────────────────────────────────────────────────────

def _prim_port_p(ctx, env, args, app_node):
    return make_boolean(is_port(args[0]))


def _prim_input_port_p(ctx, env, args, app_node):
    v = args[0]
    return make_boolean(is_port(v) and as_port(v).is_input)


def _prim_output_port_p(ctx, env, args, app_node):
    v = args[0]
    return make_boolean(is_port(v) and not as_port(v).is_input)


def _prim_textual_port_p(ctx, env, args, app_node):
    v = args[0]
    return make_boolean(is_port(v) and as_port(v).is_text)


def _prim_binary_port_p(ctx, env, args, app_node):
    v = args[0]
    return make_boolean(is_port(v) and not as_port(v).is_text)


def _prim_input_port_open_p(ctx, env, args, app_node):
    v = args[0]
    if not is_port(v):
        return make_boolean(False)
    p = as_port(v)
    return make_boolean(p.is_input and p.is_open)


def _prim_output_port_open_p(ctx, env, args, app_node):
    v = args[0]
    if not is_port(v):
        return make_boolean(False)
    p = as_port(v)
    return make_boolean((not p.is_input) and p.is_open)


def _prim_port_open_p(ctx, env, args, app_node):
    v = args[0]
    if not is_port(v):
        return make_boolean(False)
    return make_boolean(as_port(v).is_open)


def _prim_eof_object_p(ctx, env, args, app_node):
    return make_boolean(is_eof(args[0]))


def _prim_eof_object(ctx, env, args, app_node):
    return make_eof()


# ── Constructors / closers ────────────────────────────────────────────────

def _prim_open_input_file(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'open-input-file: filename must be a string', src_of(app_node))
    path = as_string(args[0])
    try:
        f = open(path, 'r', encoding='utf-8')
    except OSError as e:
        raise SchemeFileError(
            'open-input-file: cannot open %s: %s' % (path, str(e)),
            src_of(app_node))
    buf = f.read()
    p = Port(buf, is_input=True, is_text=True, file_h=f, name=path)
    return make_port(p)


def _prim_open_output_file(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'open-output-file: filename must be a string', src_of(app_node))
    path = as_string(args[0])
    try:
        f = open(path, 'w', encoding='utf-8')
    except OSError as e:
        raise SchemeFileError(
            'open-output-file: cannot open %s: %s' % (path, str(e)),
            src_of(app_node))
    p = Port([], is_input=False, is_text=True, file_h=f, name=path)
    return make_port(p)


def _prim_open_input_string(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'open-input-string: argument must be a string', src_of(app_node))
    p = Port(as_string(args[0]), is_input=True, is_text=True,
             name='<input-string>')
    return make_port(p)


def _prim_open_output_string(ctx, env, args, app_node):
    p = Port([], is_input=False, is_text=True, name='<output-string>')
    return make_port(p)


def _prim_get_output_string(ctx, env, args, app_node):
    p = _check_textual_output(args[0], 'get-output-string', app_node)
    if p.file_h is not None:
        raise SchemeTypeError(
            'get-output-string: port is not a string output port',
            src_of(app_node))
    return make_string(''.join(p.buf))


def _prim_open_input_bytevector(ctx, env, args, app_node):
    if not is_bytevector(args[0]):
        raise SchemeTypeError(
            'open-input-bytevector: argument must be a bytevector',
            src_of(app_node))
    src = as_bytevector_items(args[0])
    # Copy so the source bytevector remains independent of port reads.
    p = Port(bytearray(src), is_input=True, is_text=False,
             name='<input-bytevector>')
    return make_port(p)


def _prim_open_output_bytevector(ctx, env, args, app_node):
    p = Port(bytearray(), is_input=False, is_text=False,
             name='<output-bytevector>')
    return make_port(p)


def _prim_get_output_bytevector(ctx, env, args, app_node):
    p = _check_binary_output(args[0], 'get-output-bytevector', app_node)
    if p.file_h is not None:
        raise SchemeTypeError(
            'get-output-bytevector: port is not a bytevector output port',
            src_of(app_node))
    return make_bytevector(bytearray(p.buf))


def _prim_open_binary_input_file(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'open-binary-input-file: filename must be a string',
            src_of(app_node))
    path = as_string(args[0])
    try:
        f = open(path, 'rb')
    except OSError as e:
        raise SchemeFileError(
            'open-binary-input-file: cannot open %s: %s' % (path, str(e)),
            src_of(app_node))
    data = bytearray(f.read())
    p = Port(data, is_input=True, is_text=False, file_h=f, name=path)
    return make_port(p)


def _prim_open_binary_output_file(ctx, env, args, app_node):
    if not is_string(args[0]):
        raise SchemeTypeError(
            'open-binary-output-file: filename must be a string',
            src_of(app_node))
    path = as_string(args[0])
    try:
        f = open(path, 'wb')
    except OSError as e:
        raise SchemeFileError(
            'open-binary-output-file: cannot open %s: %s' % (path, str(e)),
            src_of(app_node))
    p = Port(bytearray(), is_input=False, is_text=False,
             file_h=f, name=path)
    return make_port(p)


def _prim_close_port(ctx, env, args, app_node):
    if not is_port(args[0]):
        raise SchemeTypeError(
            'close-port: argument must be a port', src_of(app_node))
    p = as_port(args[0])
    if p.is_open and p.file_h is not None:
        try:
            p.file_h.close()
        except OSError:
            pass
    p.is_open = False
    return VOID_VALUE


def _prim_close_input_port(ctx, env, args, app_node):
    # R7RS 6.13.1: close-input-port has no effect on an already-closed port,
    # so check the port TYPE (must be an input port) without requiring it to
    # be open, then delegate to the idempotent close-port.
    v = args[0]
    if not is_port(v):
        raise SchemeTypeError(
            'close-input-port: argument must be a port', src_of(app_node))
    if not as_port(v).is_input:
        raise SchemeTypeError(
            'close-input-port: argument must be an input port', src_of(app_node))
    return _prim_close_port(ctx, env, args, app_node)


def _prim_close_output_port(ctx, env, args, app_node):
    # R7RS 6.13.1: close-output-port has no effect on an already-closed port.
    v = args[0]
    if not is_port(v):
        raise SchemeTypeError(
            'close-output-port: argument must be a port', src_of(app_node))
    if as_port(v).is_input:
        raise SchemeTypeError(
            'close-output-port: argument must be an output port', src_of(app_node))
    return _prim_close_port(ctx, env, args, app_node)


# ── Read primitives ───────────────────────────────────────────────────────

def _read_one_char(p):
    """Return the next char from a textual input port, or None at eof."""
    if p.pos >= len(p.buf):
        return None
    c = p.buf[p.pos]
    p.pos = p.pos + 1
    return c


def _peek_one_char(p):
    if p.pos >= len(p.buf):
        return None
    return p.buf[p.pos]


def _prim_read_char(ctx, env, args, app_node):
    if len(args) == 0:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[0]
    p = _check_textual_input(port_val, 'read-char', app_node)
    c = _read_one_char(p)
    if c is None:
        return make_eof()
    return make_character(c)


def _prim_peek_char(ctx, env, args, app_node):
    if len(args) == 0:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[0]
    p = _check_textual_input(port_val, 'peek-char', app_node)
    c = _peek_one_char(p)
    if c is None:
        return make_eof()
    return make_character(c)


def _prim_char_ready_p(ctx, env, args, app_node):
    # In our impl, all input ports are buffered (open-input-file slurps
    # the whole file up front; open-input-string holds the source string),
    # so a character is always ready when not at EOF.  R7RS allows always
    # returning #t when the impl never blocks for I/O.
    if len(args) == 0:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[0]
    _check_textual_input(port_val, 'char-ready?', app_node)
    return make_boolean(True)


def _prim_read(ctx, env, args, app_node):
    from pyscheme.Parser import Parser, tokenize, TOK_EOF, SchemeSyntaxError
    if len(args) == 0:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[0]
    p = _check_input_port(port_val, 'read', app_node)
    remaining = p.buf[p.pos:]
    if not remaining.strip():
        return make_eof()
    try:
        tokens = tokenize(remaining, p.name)
    except SchemeSyntaxError:
        raise
    parser = Parser(tokens)
    if parser._peek().kind == TOK_EOF:
        return make_eof()
    form = parser.parse_expr()
    # Advance the port past the consumed text.  Tokens carry source
    # positions but not byte offsets; the simplest reliable advance is
    # to compute how many characters were consumed by re-tokenizing
    # everything before the next unconsumed token.
    nxt = parser._peek()
    if nxt.kind == TOK_EOF:
        p.pos = len(p.buf)
    elif nxt.src is not None:
        # Translate (line, col) to a character offset within `remaining`.
        line = nxt.src.line
        col = nxt.src.col
        lines_seen = 0
        i = 0
        while lines_seen < line - 1 and i < len(remaining):
            if remaining[i] == '\n':
                lines_seen = lines_seen + 1
            i = i + 1
        i = i + col - 1
        p.pos = p.pos + i
    else:
        # Fallback: skip to end so the port reports eof on next read.
        p.pos = len(p.buf)
    return form


def _prim_read_line(ctx, env, args, app_node):
    if len(args) == 0:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[0]
    p = _check_textual_input(port_val, 'read-line', app_node)
    if p.pos >= len(p.buf):
        return make_eof()
    # R7RS 6.13.2: an end of line is a linefeed, a carriage return, or a
    # carriage return followed by a linefeed (CRLF counts as one ending).
    n = len(p.buf)
    i = p.pos
    while i < n and p.buf[i] != '\n' and p.buf[i] != '\r':
        i = i + 1
    if i >= n:
        line = p.buf[p.pos:]
        p.pos = n
    else:
        line = p.buf[p.pos:i]
        if p.buf[i] == '\r' and i + 1 < n and p.buf[i + 1] == '\n':
            p.pos = i + 2   # CRLF
        else:
            p.pos = i + 1   # lone LF or lone CR
    return make_string(line)


def _prim_read_string(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'read-string: count must be an integer', src_of(app_node))
    k = as_integer(args[0])
    if k < 0:
        raise SchemeTypeError(
            'read-string: count must be non-negative', src_of(app_node))
    if len(args) == 1:
        port_val = _get_current_input(ctx)
    else:
        port_val = args[1]
    p = _check_textual_input(port_val, 'read-string', app_node)
    if p.pos >= len(p.buf) and k > 0:
        return make_eof()
    end = min(p.pos + k, len(p.buf))
    s = p.buf[p.pos:end]
    p.pos = end
    return make_string(s)


# ── Binary port read primitives ───────────────────────────────────────────

def _prim_read_u8(ctx, env, args, app_node):
    port_val = args[0] if len(args) >= 1 else _get_current_input(ctx)
    p = _check_binary_input(port_val, 'read-u8', app_node)
    if p.pos >= len(p.buf):
        return make_eof()
    b = p.buf[p.pos]
    p.pos = p.pos + 1
    return make_integer(b)


def _prim_peek_u8(ctx, env, args, app_node):
    port_val = args[0] if len(args) >= 1 else _get_current_input(ctx)
    p = _check_binary_input(port_val, 'peek-u8', app_node)
    if p.pos >= len(p.buf):
        return make_eof()
    return make_integer(p.buf[p.pos])


def _prim_u8_ready_p(ctx, env, args, app_node):
    # Our binary ports are always buffered, so u8 is always ready.  R7RS
    # permits always returning #t when the impl never blocks.
    port_val = args[0] if len(args) >= 1 else _get_current_input(ctx)
    _check_binary_input(port_val, 'u8-ready?', app_node)
    return make_boolean(True)


def _prim_read_bytevector(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'read-bytevector: count must be an integer', src_of(app_node))
    k = as_integer(args[0])
    if k < 0:
        raise SchemeTypeError(
            'read-bytevector: count must be non-negative', src_of(app_node))
    port_val = args[1] if len(args) >= 2 else _get_current_input(ctx)
    p = _check_binary_input(port_val, 'read-bytevector', app_node)
    if p.pos >= len(p.buf) and k > 0:
        return make_eof()
    end = min(p.pos + k, len(p.buf))
    chunk = bytearray(p.buf[p.pos:end])
    p.pos = end
    return make_bytevector(chunk)


def _prim_read_bytevector_bang(ctx, env, args, app_node):
    if not is_bytevector(args[0]):
        raise SchemeTypeError(
            'read-bytevector!: first argument must be a bytevector',
            src_of(app_node))
    dst = as_bytevector_items(args[0])
    port_val = args[1] if len(args) >= 2 else _get_current_input(ctx)
    p = _check_binary_input(port_val, 'read-bytevector!', app_node)
    start, end = parse_start_end(args, 2, len(dst), 'read-bytevector!', app_node,
                                 range_msg='range out of bounds')
    want = end - start
    if p.pos >= len(p.buf) and want > 0:
        return make_eof()
    avail = min(want, len(p.buf) - p.pos)
    i = 0
    while i < avail:
        dst[start + i] = p.buf[p.pos + i]
        i = i + 1
    p.pos = p.pos + avail
    return make_integer(avail)


# ── Write primitives ──────────────────────────────────────────────────────

def _emit_to_port(p, text):
    """Write text to a textual output port.  Files write through their
    file_h directly; string ports accumulate chunks for later
    get-output-string."""
    if p.file_h is not None:
        p.file_h.write(text)
    else:
        p.buf.append(text)


def _emit_bytes_to_port(p, data):
    """Write bytes to a binary output port.  Files (opened with mode 'wb')
    write through their file_h directly; bytevector output ports
    accumulate into the underlying bytearray."""
    if p.file_h is not None:
        p.file_h.write(data)
    else:
        p.buf.extend(data)


def _resolve_output_port(ctx, args, idx_default_offset):
    """If args has more elements, treat args[idx_default_offset] as a
    port; otherwise return the current output port."""
    if len(args) > idx_default_offset:
        return args[idx_default_offset]
    return _get_current_output(ctx)


def _prim_write_char(ctx, env, args, app_node):
    c = args[0]
    if not is_character(c):
        raise SchemeTypeError(
            'write-char: first argument must be a character', src_of(app_node))
    port_val = _resolve_output_port(ctx, args, 1)
    p = _check_textual_output(port_val, 'write-char', app_node, 2)
    _emit_to_port(p, as_character(c))
    return VOID_VALUE


def _prim_write_string(ctx, env, args, app_node):
    s = args[0]
    if not is_string(s):
        raise SchemeTypeError(
            'write-string: first argument must be a string', src_of(app_node))
    text = as_string(s)
    port_val = _resolve_output_port(ctx, args, 1)
    p = _check_textual_output(port_val, 'write-string', app_node, 2)
    start, end = parse_start_end(args, 2, len(text), 'write-string', app_node,
                                 range_msg='range out of bounds')
    _emit_to_port(p, text[start:end])
    return VOID_VALUE


def _prim_write_u8(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'write-u8: byte must be an integer', src_of(app_node))
    b = as_integer(args[0])
    if b < 0 or b > 255:
        raise SchemeTypeError(
            'write-u8: byte out of u8 range', src_of(app_node))
    port_val = args[1] if len(args) >= 2 else _get_current_output(ctx)
    p = _check_binary_output(port_val, 'write-u8', app_node, 2)
    _emit_bytes_to_port(p, bytes([b]))
    return VOID_VALUE


def _prim_write_bytevector(ctx, env, args, app_node):
    if not is_bytevector(args[0]):
        raise SchemeTypeError(
            'write-bytevector: first argument must be a bytevector',
            src_of(app_node))
    src = as_bytevector_items(args[0])
    port_val = args[1] if len(args) >= 2 else _get_current_output(ctx)
    p = _check_binary_output(port_val, 'write-bytevector', app_node, 2)
    start, end = parse_start_end(args, 2, len(src), 'write-bytevector', app_node,
                                 range_msg='range out of bounds')
    _emit_bytes_to_port(p, bytes(src[start:end]))
    return VOID_VALUE


def _prim_newline(ctx, env, args, app_node):
    port_val = _resolve_output_port(ctx, args, 0)
    p = _check_output_port(port_val, 'newline', app_node, 1)
    _emit_to_port(p, '\n')
    return VOID_VALUE


def _render_display(val):
    """display: strings without quotes, characters as bare glyph,
    lists rendered with display semantics for elements."""
    from pyscheme.AST import (
        is_string, is_character, is_void, is_cons, is_nil, is_vector,
        as_string, as_character, as_vector_items,
    )
    from pyscheme.PrettyPrinter import pretty_print, _has_cycle, _render_structure
    if is_string(val):
        return as_string(val)
    if is_character(val):
        return as_character(val)
    if is_void(val):
        return ''
    if is_cons(val) or is_vector(val):
        # Cyclic values keep the existing behaviour: route to the write-style
        # datum-label renderer.  Acyclic ones render iteratively via the shared
        # task-stack helper, passing _render_display as the leaf renderer so list
        # and vector elements inherit display semantics (heap-bounded depth).
        if _has_cycle(val):
            return pretty_print(val)
        return _render_structure(val, _render_display)
    return pretty_print(val)


def _prim_display(ctx, env, args, app_node):
    v = args[0]
    port_val = _resolve_output_port(ctx, args, 1)
    p = _check_textual_output(port_val, 'display', app_node, 2)
    _emit_to_port(p, _render_display(v))
    return VOID_VALUE


def _prim_write(ctx, env, args, app_node):
    from pyscheme.PrettyPrinter import pretty_print
    v = args[0]
    port_val = _resolve_output_port(ctx, args, 1)
    p = _check_textual_output(port_val, 'write', app_node, 2)
    # pretty_print routes to pretty_print_shared for cycles
    _emit_to_port(p, pretty_print(v))
    return VOID_VALUE


def _prim_flush_output_port(ctx, env, args, app_node):
    port_val = _resolve_output_port(ctx, args, 0)
    p = _check_output_port(port_val, 'flush-output-port', app_node, 1)
    if p.file_h is not None:
        try:
            p.file_h.flush()
        except OSError:
            pass
    return VOID_VALUE


# ── current-input-port / current-output-port parameters ────────────────────

def _prim_current_input_port(ctx, env, args, app_node):
    return _get_current_input(ctx)


def _prim_current_output_port(ctx, env, args, app_node):
    return _get_current_output(ctx)


def _prim_current_error_port(ctx, env, args, app_node):
    if _current_error_param[0] is None:
        _current_error_param[0] = make_parameter(_stderr_port(), None)
    from pyscheme.AST import as_parameter_value
    return as_parameter_value(_current_error_param[0])


def port_parameter_for_accessor(name, ctx):
    """Return the internal parameter object backing a current-*-port accessor
    primitive, or None if `name` is not one of them.  R7RS 6.13.1 specifies
    current-output-port / current-input-port / current-error-port as parameter
    objects; they are exposed as 0-arg accessor primitives (so the harness can
    redirect output), but parameterize must be able to rebind them.  This maps
    the accessor name to its backing parameter, initializing it if needed."""
    if name == 'current-output-port':
        _get_current_output(ctx)        # ensure initialized
        return _current_output_param[0]
    if name == 'current-input-port':
        _get_current_input(ctx)
        return _current_input_param[0]
    if name == 'current-error-port':
        if _current_error_param[0] is None:
            _current_error_param[0] = make_parameter(_stderr_port(), None)
        return _current_error_param[0]
    return None


def _prim_call_with_port(ctx, env, args, app_node):
    return _run_port_runner_reentrant('call-with-port', ctx, env, args, app_node)


def _prim_write_shared(ctx, env, args, app_node):
    from pyscheme.PrettyPrinter import pretty_print_shared
    v = args[0]
    port_val = _resolve_output_port(ctx, args, 1)
    p = _check_output_port(port_val, 'write-shared', app_node, 2)
    _emit_to_port(p, pretty_print_shared(v))
    return VOID_VALUE


def _prim_write_simple(ctx, env, args, app_node):
    # write-simple explicitly skips datum-label encoding; same as write here.
    return _prim_write(ctx, env, args, app_node)


# ── File I/O wrappers ─────────────────────────────────────────────────────

def _prim_file_exists_p(ctx, env, args, app_node):
    import os as _os
    if not is_string(args[0]):
        raise SchemeTypeError(
            'file-exists?: argument must be a string', src_of(app_node))
    return make_boolean(_os.path.exists(as_string(args[0])))


def _prim_delete_file(ctx, env, args, app_node):
    import os as _os
    if not is_string(args[0]):
        raise SchemeTypeError(
            'delete-file: argument must be a string', src_of(app_node))
    path = as_string(args[0])
    try:
        _os.remove(path)
    except OSError as e:
        raise SchemeFileError('delete-file: ' + str(e), src_of(app_node))
    return VOID_VALUE


def _prim_rename_file(ctx, env, args, app_node):
    import os as _os
    if not is_string(args[0]):
        raise SchemeTypeError(
            'rename-file: first argument must be a string', src_of(app_node))
    if not is_string(args[1]):
        raise SchemeTypeError(
            'rename-file: second argument must be a string', src_of(app_node))
    try:
        _os.rename(as_string(args[0]), as_string(args[1]))
    except OSError as e:
        raise SchemeFileError('rename-file: ' + str(e), src_of(app_node))
    return VOID_VALUE


def _prim_call_with_input_file(ctx, env, args, app_node):
    return _run_port_runner_reentrant(
        'call-with-input-file', ctx, env, args, app_node)


def _prim_call_with_output_file(ctx, env, args, app_node):
    return _run_port_runner_reentrant(
        'call-with-output-file', ctx, env, args, app_node)


def _prim_with_input_from_file(ctx, env, args, app_node):
    return _run_port_runner_reentrant(
        'with-input-from-file', ctx, env, args, app_node)


def _prim_with_output_to_file(ctx, env, args, app_node):
    return _run_port_runner_reentrant(
        'with-output-to-file', ctx, env, args, app_node)


def _prim_with_input_from_string(ctx, env, args, app_node):
    return _run_port_runner_reentrant(
        'with-input-from-string', ctx, env, args, app_node)


def _close_port_obj(p):
    """Close a port object the way the call-with-* finally blocks did."""
    if p.is_open and p.file_h is not None:
        try:
            p.file_h.close()
        except OSError:
            pass
    p.is_open = False


def port_runner_setup(name, ctx, env, args, app_node):
    """Frame-driven setup for the port runners (call-with-port,
    call-with-{input,output}-file, with-{input,output}-{from,to}-{file,string}).

    Validates and opens exactly as the _prim_* bodies did, then returns
    (before_prim, after_prim, body_proc, body_args).  The evaluator pushes a
    dynamic-wind entry (before_prim, after_prim) and tail-calls body_proc on the
    K stack, so the user proc/thunk no longer re-enters cek_eval.  after_prim is
    a native (Python-backed) primitive performing the cleanup the old
    try/finally did -- close the port, and for the with-* forms restore the
    current-port parameter -- so it runs on normal return, error unwind, and
    continuation escape alike, yet never itself re-enters the evaluator.  The
    before-thunk is a no-op: these forms establish their state once, matching
    the old set-then-finally (re-entry through them is unspecified in R7RS)."""
    from pyscheme.AST import (make_primitive, as_parameter_value,
                              set_parameter_value)
    from pyscheme.Environment import SchemeArityError, arity_mismatch_msg
    src = src_of(app_node)
    if len(args) != 2:
        raise SchemeArityError(arity_mismatch_msg(name, 2, 2, len(args)), src)
    before = make_primitive('%port-runner-before',
                            lambda c, e, a, n: VOID_VALUE)

    # call-with-port / call-with-input-file / call-with-output-file:
    # body = (proc port); cleanup = close the port.
    if name == 'call-with-port':
        port_val = args[0]
        p = _check_port(port_val, name, app_node)
    elif name == 'call-with-input-file' or name == 'call-with-output-file':
        if not is_string(args[0]):
            raise SchemeTypeError(name + ': filename must be a string', src)
        if name == 'call-with-input-file':
            port_val = _prim_open_input_file(ctx, env, [args[0]], app_node)
        else:
            port_val = _prim_open_output_file(ctx, env, [args[0]], app_node)
        p = as_port(port_val)
    else:
        p = None

    if p is not None:
        def after(c, e, a, n, _p=p):
            _close_port_obj(_p)
            return VOID_VALUE
        return (before, make_primitive('%port-runner-after', after),
                args[1], [port_val])

    # with-{input,output}-{from,to}-{file,string}: body = (thunk); set the
    # current-port parameter now, restore it and close the port on exit.
    if name == 'with-input-from-file':
        if not is_string(args[0]):
            raise SchemeTypeError(name + ': filename must be a string', src)
        port_val = _prim_open_input_file(ctx, env, [args[0]], app_node)
        _get_current_input(ctx)
        param = _current_input_param[0]
    elif name == 'with-input-from-string':
        if not is_string(args[0]):
            raise SchemeTypeError(
                name + ': first argument must be a string', src)
        port_val = _prim_open_input_string(ctx, env, [args[0]], app_node)
        _get_current_input(ctx)
        param = _current_input_param[0]
    elif name == 'with-output-to-file':
        if not is_string(args[0]):
            raise SchemeTypeError(name + ': filename must be a string', src)
        port_val = _prim_open_output_file(ctx, env, [args[0]], app_node)
        _get_current_output(ctx)
        param = _current_output_param[0]
    else:
        raise SchemeTypeError(
            'port_runner_setup: unknown port runner ' + repr(name), src)

    old_val = as_parameter_value(param)
    set_parameter_value(param, port_val)

    def after(c, e, a, n, _param=param, _old=old_val, _port=port_val):
        set_parameter_value(_param, _old)
        _prim_close_port(ctx, env, [_port], app_node)
        return VOID_VALUE
    return (before, make_primitive('%port-runner-after', after),
            args[1], [])


def _run_port_runner_reentrant(name, ctx, env, args, app_node):
    """Re-entrant fallback for the port runners.  port_runner_setup is the
    SINGLE source of their open / validate / teardown logic; the evaluator's
    FRAME_CALL and apply paths drive the body frame-based off it with no
    re-entry.  The _prim_* bodies registered for these names are reached only
    when a port runner is applied through a path that still calls a primitive
    body directly (e.g. a cond / case `=>` target); they reuse that one source
    by running its body and cleanup re-entrantly via _apply_scheme_proc, so the
    open/validate/close logic is no longer duplicated here."""
    from pyscheme.primitives.meta import _apply_scheme_proc
    before, after, body_proc, body_args = port_runner_setup(
        name, ctx, env, args, app_node)
    try:
        return _apply_scheme_proc(body_proc, body_args, ctx, None, app_node)
    finally:
        as_primitive_fn(after)(ctx, env, [], app_node)


def register():
    # Predicates
    register_primitive('port?', (1, 1), _prim_port_p,
                       doc='Return #t if obj is a port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('input-port?', (1, 1), _prim_input_port_p,
                       doc='Return #t if obj is an input port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('output-port?', (1, 1), _prim_output_port_p,
                       doc='Return #t if obj is an output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('textual-port?', (1, 1), _prim_textual_port_p,
                       doc='Return #t if obj is a textual port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('binary-port?', (1, 1), _prim_binary_port_p,
                       doc='Return #t if obj is a binary port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('input-port-open?', (1, 1), _prim_input_port_open_p,
                       doc='Return #t if obj is an open input port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('output-port-open?', (1, 1), _prim_output_port_open_p,
                       doc='Return #t if obj is an open output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('port-open?', (1, 1), _prim_port_open_p,
                       doc='Return #t if port is open (input or output).  Extension.',
                       category=CATEGORY)
    register_primitive('eof-object?', (1, 1), _prim_eof_object_p,
                       doc='Return #t if obj is the eof object.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('eof-object', (0, 0), _prim_eof_object,
                       doc='Return the eof object.  R7RS 6.13.',
                       category=CATEGORY)
    # Constructors / closers
    register_primitive('open-input-file', (1, 1), _prim_open_input_file,
                       doc=('(open-input-file path) opens the file and slurps its contents '
                            'into the port\'s buffer.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('open-output-file', (1, 1), _prim_open_output_file,
                       doc='(open-output-file path) opens a file for writing.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('open-input-string', (1, 1), _prim_open_input_string,
                       doc='(open-input-string string) returns an input port reading from string.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('open-output-string', (0, 0), _prim_open_output_string,
                       doc='Return a fresh output string port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('get-output-string', (1, 1), _prim_get_output_string,
                       doc=('(get-output-string port) returns the accumulated string from a '
                            'string output port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('open-input-bytevector', (1, 1),
                       _prim_open_input_bytevector,
                       doc=('(open-input-bytevector bv) returns a binary input port reading '
                            'from a copy of bv.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('open-output-bytevector', (0, 0),
                       _prim_open_output_bytevector,
                       doc='Return a fresh binary output port that accumulates bytes.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('get-output-bytevector', (1, 1),
                       _prim_get_output_bytevector,
                       doc=('(get-output-bytevector port) returns the accumulated bytevector '
                            'from a binary output port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('open-binary-input-file', (1, 1),
                       _prim_open_binary_input_file,
                       doc=('(open-binary-input-file path) opens path in binary mode and '
                            'returns a binary input port over its contents.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('open-binary-output-file', (1, 1),
                       _prim_open_binary_output_file,
                       doc=('(open-binary-output-file path) opens path for binary writing.  '
                            'R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('close-port', (1, 1), _prim_close_port,
                       doc='Close any port (input or output).  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('close-input-port', (1, 1), _prim_close_input_port,
                       doc='Close an input port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('close-output-port', (1, 1), _prim_close_output_port,
                       doc='Close an output port.  R7RS 6.13.',
                       category=CATEGORY)
    # Read primitives
    register_primitive('read-char', (0, 1), _prim_read_char,
                       doc=('(read-char [port]) reads one character.  Returns the eof object '
                            'at end of file.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('peek-char', (0, 1), _prim_peek_char,
                       doc=('(peek-char [port]) returns the next character without consuming '
                            'it.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read', (0, 1), _prim_read,
                       doc=('(read [port]) reads and returns one S-expression from the port.  '
                            'Returns the eof object at end of file.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read-line', (0, 1), _prim_read_line,
                       doc=('(read-line [port]) reads and returns characters up to (but not '
                            'including) the next newline; returns the eof object at end '
                            'of file.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read-string', (1, 2), _prim_read_string,
                       doc=('(read-string k [port]) reads up to k characters and returns '
                            'them as a string.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('char-ready?', (0, 1), _prim_char_ready_p,
                       doc=('(char-ready? [port]) returns #t when read-char would not block.  '
                            'Our buffered ports always return #t.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read-u8', (0, 1), _prim_read_u8,
                       doc=('(read-u8 [port]) reads one byte from a binary input port.  '
                            'Returns the eof object at end of port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('peek-u8', (0, 1), _prim_peek_u8,
                       doc=('(peek-u8 [port]) returns the next byte without consuming it.  '
                            'R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('u8-ready?', (0, 1), _prim_u8_ready_p,
                       doc=('(u8-ready? [port]) returns #t when read-u8 would not block.  '
                            'Our buffered ports always return #t.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read-bytevector', (1, 2), _prim_read_bytevector,
                       doc=('(read-bytevector k port) reads up to k bytes and returns a '
                            'fresh bytevector.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('read-bytevector!', (1, 4), _prim_read_bytevector_bang,
                       doc=('(read-bytevector! bv port [start [end]]) reads bytes into bv '
                            'in place; returns the count read.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('write-u8', (1, 2), _prim_write_u8,
                       doc=('(write-u8 byte port) writes one byte to a binary output port.  '
                            'R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('write-bytevector', (1, 4), _prim_write_bytevector,
                       doc=('(write-bytevector bv port [start [end]]) writes bytes to a '
                            'binary output port.  R7RS 6.13.'),
                       category=CATEGORY)
    # Write primitives (override the textless versions in primitives/io.py)
    register_primitive('write-char', (1, 2), _prim_write_char,
                       doc='(write-char char [port]) writes char to the output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('write-string', (1, 4), _prim_write_string,
                       doc='(write-string string [port]) writes string to the output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('newline', (0, 1), _prim_newline,
                       doc='(newline [port]) writes a newline to the output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('display', (1, 2), _prim_display,
                       doc=('(display obj [port]) writes a human-readable representation '
                            'of obj to the output port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('write', (1, 2), _prim_write,
                       doc=('(write obj [port]) writes a machine-readable representation '
                            'of obj to the output port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('write-shared', (1, 2), _prim_write_shared,
                       doc=('(write-shared obj [port]) like write, but uses #n=/#n# datum labels '
                            'for shared or circular structure.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('write-simple', (1, 2), _prim_write_simple,
                       doc=('(write-simple obj [port]) like write, but never emits datum labels.  '
                            'R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('flush-output-port', (0, 1), _prim_flush_output_port,
                       doc='(flush-output-port [port]) flushes any buffered output.  R7RS 6.13.',
                       category=CATEGORY)
    # Current-port accessors
    register_primitive('current-input-port', (0, 0), _prim_current_input_port,
                       doc='Return the current input port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('current-output-port', (0, 0), _prim_current_output_port,
                       doc='Return the current output port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('current-error-port', (0, 0), _prim_current_error_port,
                       doc='Return the current error port.  R7RS 6.13.',
                       category=CATEGORY)
    register_primitive('call-with-port', (2, 2), _prim_call_with_port,
                       doc=('(call-with-port port proc) calls (proc port), closes port '
                            'before returning, and returns proc\'s result.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('call-with-input-file', (2, 2), _prim_call_with_input_file,
                       doc=('(call-with-input-file path proc) opens path for reading, calls '
                            '(proc port), closes port, returns result.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('call-with-output-file', (2, 2), _prim_call_with_output_file,
                       doc=('(call-with-output-file path proc) opens path for writing, calls '
                            '(proc port), closes port, returns result.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('with-input-from-file', (2, 2), _prim_with_input_from_file,
                       doc=('(with-input-from-file path thunk) opens path, temporarily makes it '
                            'the current input port, calls (thunk), restores the port.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('with-output-to-file', (2, 2), _prim_with_output_to_file,
                       doc=('(with-output-to-file path thunk) opens path for writing, temporarily '
                            'makes it the current output port, calls (thunk), restores.  R7RS 6.13.'),
                       category=CATEGORY)
    register_primitive('with-input-from-string', (2, 2), _prim_with_input_from_string,
                       doc=('(with-input-from-string str thunk) temporarily makes a string input '
                            'port for str the current input port, calls (thunk), restores.  '
                            'Common extension (not R7RS).'),
                       category=CATEGORY)
    register_primitive('file-exists?', (1, 1), _prim_file_exists_p,
                       doc='(file-exists? path) returns #t if path names an existing file.  R7RS 6.14.',
                       category=CATEGORY)
    register_primitive('delete-file', (1, 1), _prim_delete_file,
                       doc='(delete-file path) deletes the named file.  R7RS 6.14.',
                       category=CATEGORY)
    register_primitive('rename-file', (2, 2), _prim_rename_file,
                       doc='(rename-file old new) renames old to new.  R7RS 6.14.',
                       category=CATEGORY)
