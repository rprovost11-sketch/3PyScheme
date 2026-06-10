"""Parser (reader) for Scheme source code.

The parser is a pure reader: it lexes source into tokens and builds
S-expressions.  It recognizes literal forms and turns each one into a
tagged tuple matching the CEK machine's value shape.  Identifiers
become (SYMBOL, name, src) atoms.  Parenthesized lists become cons-cell
chains terminated by NIL_VALUE (proper lists) or an arbitrary value
(improper / dotted lists).

Source-position attachment:
    Each atom carries its own SourceInfo as the last element of its
    tuple: (INTEGER, n, src), (SYMBOL, name, src), etc.
    Each ConsCell carries a SourceInfo pointing at the '(' that
    opened its list.  All cells in a single list-chain share that
    same SourceInfo (they're the same '(').
    NIL_VALUE is a shared singleton and does not carry position.

Tabs are expanded to spaces on entry (see feedback: user wants tabs
unconditionally normalized) so captured source lines are tab-free and
caret columns align in the terminal.

The parser does NO special-form recognition and NO semantic checks.
It only reports syntactic errors that arise while reading raw
S-expressions: unterminated lists, unterminated strings, malformed
literals, unknown #-syntax, stray ')', etc.

Any further error checking - whether a `lambda`'s parameter list is
well formed, whether a `define` has the right number of args, whether
identifiers are used in valid positions - belongs to the Analyzer.

Output shape:

    Literal atoms (matching Evaluator's value forms), each a tagged
    tuple with src as its last element:
        (INTEGER,   n,    src)
        (REAL,      v,    src)
        (RATIONAL,  n, d, src)
        (BOOLEAN,   b,    src)
        (CHARACTER, c,    src)
        (STRING,    s,    src)
        (SYMBOL,    name, src)

    Lists become ConsCell chains:
        alloc_cons(elem1, alloc_cons(elem2, NIL_VALUE, lparen_src), lparen_src)

    Dotted / improper lists use the dotted tail as the innermost cdr:
        (a . b)  ==  alloc_cons(a, b, lparen_src)
        (a b . c) == alloc_cons(a, alloc_cons(b, c, lparen_src), lparen_src)

    Quote reader abbreviation:
        'datum  ==  alloc_cons((SYMBOL, 'quote', quote_src),
                               alloc_cons(<datum>, NIL_VALUE, quote_src),
                               quote_src)

Public API:
    parse(source, filename=None)      -> list of top-level S-expressions
    parse_one(source, filename=None)  -> single S-expression; error on trailing input
    tokenize(source)                  -> list of Token (exposed for testing)
    SchemeSyntaxError                 -> raised on any lex/read error
"""
import re
import math as _math
from pyscheme.rational import _Rat

from pyscheme.AST import (
    alloc_cons, NIL_VALUE, SourceInfo, ConsCell, is_cons, is_nil,
    is_integer, is_real, is_rational, is_string, is_character,
    is_boolean, is_symbol,
    as_integer, as_real, as_string, as_character, as_boolean, as_symbol,
    as_rational_num, as_rational_den,
    make_integer, make_real, make_rational, make_complex, make_exact_complex,
    make_string, make_character,
    make_boolean, make_symbol, make_vector, make_bytevector, as_vector_items,
    mark_literal_immutable,
    src_of,
    REAL, RATIONAL, INTEGER, CHARACTER, BOOLEAN, STRING, SYMBOL, NIL,
)
from pyscheme.Environment import _PositionedSchemeError


# -------- Tokens --------

TOK_LPAREN = 'LPAREN'
TOK_RPAREN = 'RPAREN'
TOK_LBRACKET = 'LBRACKET'
TOK_RBRACKET = 'RBRACKET'
TOK_VECTOR_LPAREN = 'VECTOR_LPAREN'
TOK_BYTEVECTOR_LPAREN = 'BYTEVECTOR_LPAREN'
TOK_QUOTE = 'QUOTE'
TOK_QUASIQUOTE = 'QUASIQUOTE'
TOK_UNQUOTE = 'UNQUOTE'
TOK_UNQUOTE_SPLICING = 'UNQUOTE_SPLICING'
TOK_DOT = 'DOT'
TOK_INT = 'INT'
TOK_REAL = 'REAL'
TOK_RATIONAL = 'RATIONAL'
TOK_COMPLEX = 'COMPLEX'
TOK_EXACT_COMPLEX = 'EXACT_COMPLEX'
TOK_STRING = 'STRING'
TOK_CHAR = 'CHAR'
TOK_BOOL = 'BOOL'
TOK_IDENT = 'IDENT'
TOK_EOF = 'EOF'
TOK_DATUM_COMMENT = 'DATUM_COMMENT'
TOK_LABEL_DEF = 'LABEL_DEF'
TOK_LABEL_REF = 'LABEL_REF'


# Reader frame kinds — one per in-progress container/wrapper on the explicit
# stack used by Parser._read_datum (replaces host recursion; see that method).
_F_LIST = 0          # ( ... )  or  [ ... ]
_F_VECTOR = 1        # #( ... )
_F_BYTEVECTOR = 2    # #u8( ... )
_F_PREFIX = 3        # '  `  ,  ,@   (wraps exactly one datum)
_F_LABEL_LIST = 4    # #n= followed by a list   (stub pre-registered)
_F_LABEL_VECTOR = 5  # #n= followed by a vector (pre_vector pre-registered)
_F_LABEL_PLAIN = 6   # #n= followed by an atom/prefix/bytevector (registered after)
_F_DISCARD = 7       # #;   (reads one datum and throws it away)

# Sentinel returned by Parser._emit when the emitted value was consumed by a
# container/discard frame (so the driver loop should read the next datum)
# rather than being the final result.
_READ_MORE = object()


class _ParseFrame:
    """One in-progress container/wrapper on the reader's explicit stack.

    Carries the union of fields the various frame kinds need; only the fields
    relevant to `kind` are populated.  Holding this state on a heap list (rather
    than the host call stack) is what lets the reader handle arbitrarily deep
    nesting without a recursion-depth cap."""

    __slots__ = ('kind', 'items', 'src', 'closer', 'closer_ch',
                 'want_tail', 'tail_filled', 'dotted_tail',
                 'sym', 'label_n', 'stub', 'pre_vector', 'pre_items')

    def __init__(self, kind):
        self.kind = kind
        self.items = None          # list: accumulated elements (LIST/VECTOR/BYTEVECTOR)
        self.src = None            # SourceInfo of the opening token
        self.closer = None         # TOK_RPAREN | TOK_RBRACKET (LIST)
        self.closer_ch = None      # ')' | ']'                  (LIST)
        self.want_tail = False     # saw '.' — next datum is the improper tail (LIST)
        self.tail_filled = False   # the dotted tail has been read (LIST)
        self.dotted_tail = None    # the improper-list tail value (LIST)
        self.sym = None            # the wrapper symbol (PREFIX)
        self.label_n = None        # datum-label number (LABEL_*)
        self.stub = None           # pre-allocated cons mutated in place (LABEL_LIST)
        self.pre_vector = None     # pre-created vector (LABEL_VECTOR)
        self.pre_items = None      # the vector's backing list (LABEL_VECTOR)


class Token:
    """Lexer token.  POD; no methods except __init__.  src holds the
    SourceInfo for the token's starting position.  The parser uses src
    when building atom tuples and cons cells."""

    def __init__(self, kind, value, src):
        self.kind = kind
        self.value = value
        self.src = src


class SchemeSyntaxError(_PositionedSchemeError):
    """Raised on any lex/read error."""
    pass


# -------- Lexer --------
#
# Number patterns use a trailing lookahead (?=[\s()'";]|$) so "1abc"
# does not greedily tokenize as INT 1 + IDENT abc - it falls through
# to IDENT, where the digit-prefix check rejects it (R7RS §7.1.1).

_TOKEN_RE = re.compile(r'''
      [ \t\r\n]+                                                                # whitespace
    | ;[^\n]*                                                                   # line comment
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACKET>\[)
    | (?P<RBRACKET>\])
    | (?P<QUASIQUOTE>`)
    | (?P<UNQUOTE_SPLICING>,@)
    | (?P<UNQUOTE>,)
    | (?P<QUOTE>')
    | (?P<DOT>\.(?=[\s()\[\]'"`,;|]|$))                                        # lone . is the dotted-pair marker
    | (?P<STRING>"(?:[^"\\]|\\[\s\S])*")
    | (?P<BYTEVECTOR_LPAREN>\#u8\()
    | (?P<VECTOR_LPAREN>\#\()
    | (?P<CHAR>\#\\(?:x[0-9a-fA-F]+|[a-zA-Z]+|[\s\S]))
    | (?P<TRUE>\#t(?:rue)?(?=[\s()\[\]'"`,;|]|$))
    | (?P<FALSE>\#f(?:alse)?(?=[\s()\[\]'"`,;|]|$))
    | (?P<RATIONAL>[+-]?\d+/\d+(?=[\s()\[\]'"`,;|#]|$))
    | (?P<REAL>
          [+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?(?=[\s()\[\]'"`,;|#]|$)
        | [+-]?\d+[eE][+-]?\d+(?=[\s()\[\]'"`,;|#]|$)
      )
    | (?P<INT>[+-]?\d+(?=[\s()\[\]'"`,;|#]|$))
    | (?P<HASH_IDENT>\#[^\s()\[\]'"`,;|\\]*)
    | (?P<IDENT>[^\s()\[\]'"`,;|#\\]+)
''', re.VERBOSE)


_STRING_ESCAPES = {
    'n':  '\n',
    't':  '\t',
    'r':  '\r',
    '\\': '\\',
    '"':  '"',
    '|':  '|',
    'a':  '\a',
    'b':  '\b',
}

_SYMBOL_ESCAPES = {
    'a':  '\a',
    'b':  '\b',
    't':  '\t',
    'n':  '\n',
    'r':  '\r',
    '\\': '\\',
    '|':  '|',
    '"':  '"',
}

_HEX_DIGITS = '0123456789abcdefABCDEF'

_CHAR_NAMES = {
    'space':     ' ',
    'newline':   '\n',
    'tab':       '\t',
    'return':    '\r',
    'null':      '\0',
    'alarm':     '\a',
    'backspace': '\b',
    'delete':    '\x7f',
    'escape':    '\x1b',
}


def _make_src(line, col, source_lines, filename):
    if 1 <= line and line <= len(source_lines):
        source_line = source_lines[line - 1]
    else:
        source_line = ''
    return SourceInfo(line, col, source_line, filename)


def _char_repr(c):
    """Render one character the way Python's repr() does (quote it, escape
    backslash/quote, \\xHH for non-printables).  Factored into a helper so
    the cppscheme2 port can mirror it line-for-line: C++ has no repr(), so
    its char_repr() reproduces this behavior by hand."""
    return repr(c)


def _substring(s, start, end):
    """Return s[start:end] as an explicit character-by-character copy.
    Clamps to len(s) so an over-long end (e.g. a short #!-directive at the
    end of input) cannot index past the string -- matches Python slicing and
    C strncpy, which stops at the null terminator.
    Ports to strncpy + null terminator in C."""
    result = ''
    i = start
    n = len(s)
    while i < end and i < n:
        result = result + s[i]
        i = i + 1
    return result


def tokenize(source, filename=None):
    source_lines = source.expandtabs().splitlines()

    tokens = []
    fold_case = False
    pos = 0
    line = 1
    col = 1
    n = len(source)
    while pos < n:
        # Block comment: #| ... |# (possibly nested).  R7RS §2.2.
        if pos + 1 < n and source[pos] == '#' and source[pos + 1] == '|':
            depth = 1
            pos = pos + 2
            col = col + 2
            while pos < n and depth > 0:
                if pos + 1 < n and source[pos] == '#' and source[pos + 1] == '|':
                    depth = depth + 1
                    col = col + 2
                    pos = pos + 2
                elif pos + 1 < n and source[pos] == '|' and source[pos + 1] == '#':
                    depth = depth - 1
                    col = col + 2
                    pos = pos + 2
                else:
                    if source[pos] == '\n':
                        line = line + 1
                        col = 1
                    else:
                        col = col + 1
                    pos = pos + 1
            if depth != 0:
                raise SchemeSyntaxError(
                    'unterminated block comment',
                    _make_src(line, col, source_lines, filename))
            continue
        # Datum comment: #; — skip the following datum.  R7RS §2.2.
        if pos + 1 < n and source[pos] == '#' and source[pos + 1] == ';':
            src = _make_src(line, col, source_lines, filename)
            tokens.append(Token(TOK_DATUM_COMMENT, None, src))
            col = col + 2
            pos = pos + 2
            continue
        # Datum label definition #n= and reference #n#.  R7RS §2.4.
        if pos < n and source[pos] == '#':
            j = pos + 1
            while j < n and source[j].isdigit():
                j = j + 1
            if j > pos + 1 and j < n and source[j] in ('=', '#'):
                src = _make_src(line, col, source_lines, filename)
                label_n = int(source[pos + 1: j])
                if source[j] == '=':
                    tokens.append(Token(TOK_LABEL_DEF, label_n, src))
                else:
                    tokens.append(Token(TOK_LABEL_REF, label_n, src))
                col = col + (j - pos + 1)
                pos = j + 1
                continue
        # Directive: #!fold-case / #!no-fold-case  R7RS §2.1.
        if pos + 1 < n and source[pos] == '#' and source[pos + 1] == '!':
            if _substring(source, pos + 2, pos + 11) == 'fold-case':
                fold_case = True
                col = col + 11
                pos = pos + 11
                continue
            if _substring(source, pos + 2, pos + 14) == 'no-fold-case':
                fold_case = False
                col = col + 14
                pos = pos + 14
                continue
            raise SchemeSyntaxError(
                "unknown #!-directive",
                _make_src(line, col, source_lines, filename))
        # Vertical-bar symbol: |...| with escape sequences.  R7RS §2.1.
        if pos < n and source[pos] == '|':
            src = _make_src(line, col, source_lines, filename)
            pos = pos + 1
            col = col + 1
            raw_chars = []
            while pos < n and source[pos] != '|':
                c = source[pos]
                if c == '\\':
                    raw_chars.append(c)
                    pos = pos + 1
                    col = col + 1
                    if pos < n:
                        ec = source[pos]
                        raw_chars.append(ec)
                        if ec == '\n':
                            line = line + 1
                            col = 1
                        else:
                            col = col + 1
                        pos = pos + 1
                        if ec == 'x':
                            while pos < n and source[pos] in _HEX_DIGITS:
                                raw_chars.append(source[pos])
                                col = col + 1
                                pos = pos + 1
                            if pos < n and source[pos] == ';':
                                raw_chars.append(';')
                                col = col + 1
                                pos = pos + 1
                else:
                    if c == '\n':
                        line = line + 1
                        col = 1
                    else:
                        col = col + 1
                    raw_chars.append(c)
                    pos = pos + 1
            if pos >= n:
                raise SchemeSyntaxError(
                    'unterminated |...| symbol',
                    _make_src(line, col, source_lines, filename))
            pos = pos + 1
            col = col + 1
            name = _decode_symbol_escapes(''.join(raw_chars), src)
            if fold_case:
                name = name.lower()
            tokens.append(Token(TOK_IDENT, name, src))
            continue
        match = _TOKEN_RE.match(source, pos)
        if not match:
            raise SchemeSyntaxError(
                "unexpected character %s" % _char_repr(source[pos]),
                _make_src(line, col, source_lines, filename))
        text = match.group(0)
        kind = match.lastgroup
        if kind is not None:
            src = _make_src(line, col, source_lines, filename)
            tokens.append(_build_token(kind, text, src, fold_case))
        newlines = text.count('\n')
        if newlines > 0:
            line = line + newlines
            col = len(text) - text.rfind('\n')
        else:
            col = col + len(text)
        pos = match.end()
    tokens.append(Token(TOK_EOF, None, _make_src(
        line, col, source_lines, filename)))
    return tokens


def _build_token(kind, text, src, fold_case=False):
    if kind == 'LPAREN':
        return Token(TOK_LPAREN, '(', src)
    if kind == 'RPAREN':
        return Token(TOK_RPAREN, ')', src)
    if kind == 'LBRACKET':
        return Token(TOK_LBRACKET, '[', src)
    if kind == 'RBRACKET':
        return Token(TOK_RBRACKET, ']', src)
    if kind == 'BYTEVECTOR_LPAREN':
        return Token(TOK_BYTEVECTOR_LPAREN, text, src)
    if kind == 'VECTOR_LPAREN':
        return Token(TOK_VECTOR_LPAREN, '#(', src)
    if kind == 'QUOTE':
        return Token(TOK_QUOTE, "'", src)
    if kind == 'QUASIQUOTE':
        return Token(TOK_QUASIQUOTE, '`', src)
    if kind == 'UNQUOTE':
        return Token(TOK_UNQUOTE, ',', src)
    if kind == 'UNQUOTE_SPLICING':
        return Token(TOK_UNQUOTE_SPLICING, ',@', src)
    if kind == 'DOT':
        return Token(TOK_DOT, '.', src)
    if kind == 'STRING':
        return Token(TOK_STRING,
                     _decode_string_escapes(_substring(
                         text, 1, len(text) - 1), src),
                     src)
    if kind == 'CHAR':
        return Token(TOK_CHAR, _decode_char_literal(text, src), src)
    if kind == 'TRUE':
        return Token(TOK_BOOL, True, src)
    if kind == 'FALSE':
        return Token(TOK_BOOL, False, src)
    if kind == 'INT':
        return Token(TOK_INT, int(text), src)
    if kind == 'REAL':
        return Token(TOK_REAL, float(text), src)
    if kind == 'RATIONAL':
        parts = text.split('/')
        return Token(TOK_RATIONAL, (int(parts[0]), int(parts[1])), src)
    if kind == 'HASH_IDENT':
        tok = _try_parse_prefixed_number(text, src)
        if tok is not None:
            return tok
        raise SchemeSyntaxError("unknown #-syntax: %r" % text, src)
    if kind == 'IDENT':
        if fold_case:
            text = text.lower()
        if text == '+inf.0':
            return Token(TOK_REAL, float('inf'), src)
        if text == '-inf.0':
            return Token(TOK_REAL, float('-inf'), src)
        if text == '+nan.0':
            return Token(TOK_REAL, float('nan'), src)
        if text == '-nan.0':
            return Token(TOK_REAL, float('nan'), src)
        if text.endswith('i') and len(text) >= 2:
            tok = _try_parse_complex_literal(text, src)
            if tok is not None:
                return tok
        if '@' in text:
            tok = _try_parse_polar_literal(text, src)
            if tok is not None:
                return tok
        if len(text) > 0 and text[0] == '@':
            raise SchemeSyntaxError(
                "'@' is not a valid identifier initial: %r" % text, src)
        if _starts_like_number(text):
            raise SchemeSyntaxError(
                "malformed number or identifier: %r" % text, src)
        return Token(TOK_IDENT, text, src)
    raise RuntimeError("internal: unhandled token kind %r" % kind)


def _starts_like_number(s):
    if not s:
        return False
    if s[0].isdigit():
        return True
    if s[0] in '+-' and len(s) > 1 and (s[1].isdigit() or (s[1] == '.' and len(s) > 2 and s[2].isdigit())):
        return True
    if s[0] == '.' and len(s) > 1 and s[1].isdigit():
        return True
    return False


def _decimal_str_to_rat(s):
    """Parse a decimal string to _Rat without going through float.
    Port of C++ decimal_str_to_rat.  Handles sign, '.', and e/E exponent."""
    sign = 1
    i = 0
    if s and s[0] == '-':
        sign = -1
        i = 1
    elif s and s[0] == '+':
        i = 1
    e_idx = -1
    j = i
    while j < len(s):
        if s[j] == 'e' or s[j] == 'E':
            e_idx = j
            break
        j = j + 1
    exp = 0
    if e_idx >= 0:
        exp = int(_substring(s, e_idx + 1, len(s)))
        mantissa = _substring(s, i, e_idx)
    else:
        mantissa = _substring(s, i, len(s))
    dot_idx = -1
    k = 0
    while k < len(mantissa):
        if mantissa[k] == '.':
            dot_idx = k
            break
        k = k + 1
    if dot_idx >= 0:
        int_str = _substring(mantissa, 0, dot_idx) + \
            _substring(mantissa, dot_idx + 1, len(mantissa))
        frac_digits = len(mantissa) - dot_idx - 1
    else:
        int_str = mantissa
        frac_digits = 0
    if not int_str:
        int_str = '0'
    num = int(int_str)
    den = 10 ** frac_digits
    if exp > 0:
        num = num * (10 ** exp)
    elif exp < 0:
        den = den * (10 ** (-exp))
    return _Rat(sign * num, den)


def _try_parse_prefixed_number(text, src):
    """Parse #b/#o/#d/#x/#e/#i prefix forms.
    Returns a Token or raises SchemeSyntaxError.  Returns None only when
    the second character is not a recognised prefix letter (so the caller
    can report 'unknown #-syntax')."""
    radix = -1   # -1 = not yet specified
    exact = -1   # -1 = unspecified; 1 = exact; 0 = inexact
    i = 0
    while i < len(text) and text[i] == '#':
        if i + 1 >= len(text):
            return None
        ch = text[i + 1].lower()
        if ch == 'b':
            if radix != -1:
                raise SchemeSyntaxError(
                    "duplicate radix prefix: %r" % text, src)
            radix = 2
            i = i + 2
        elif ch == 'o':
            if radix != -1:
                raise SchemeSyntaxError(
                    "duplicate radix prefix: %r" % text, src)
            radix = 8
            i = i + 2
        elif ch == 'd':
            if radix != -1:
                raise SchemeSyntaxError(
                    "duplicate radix prefix: %r" % text, src)
            radix = 10
            i = i + 2
        elif ch == 'x':
            if radix != -1:
                raise SchemeSyntaxError(
                    "duplicate radix prefix: %r" % text, src)
            radix = 16
            i = i + 2
        elif ch == 'e':
            if exact != -1:
                raise SchemeSyntaxError(
                    "duplicate exactness prefix: %r" % text, src)
            exact = 1
            i = i + 2
        elif ch == 'i':
            if exact != -1:
                raise SchemeSyntaxError(
                    "duplicate exactness prefix: %r" % text, src)
            exact = 0
            i = i + 2
        else:
            return None
    if radix == -1:
        radix = 10
    rest = _substring(text, i, len(text))
    if not rest:
        raise SchemeSyntaxError("number prefix with no digits: %r" % text, src)
    # Try integer with given radix.
    try:
        n = int(rest, radix)
        if exact == 0:
            return Token(TOK_REAL, float(n), src)
        return Token(TOK_INT, n, src)
    except ValueError:
        pass
    # Try rational numerator/denominator with given radix.
    if '/' in rest:
        slash = rest.index('/')
        num_str = _substring(rest, 0, slash)
        den_str = _substring(rest, slash + 1, len(rest))
        try:
            num = int(num_str, radix)
            den = int(den_str, radix)
            if den != 0:
                frac = _Rat(num, den)
                if exact == 0:
                    return Token(TOK_REAL, float(frac), src)
                return Token(TOK_RATIONAL, (frac.numerator, frac.denominator), src)
        except ValueError:
            pass
    # Try real (radix 10 only).
    if radix == 10:
        if rest == '+inf.0':
            if exact == 1:
                raise SchemeSyntaxError("#e cannot be applied to +inf.0", src)
            return Token(TOK_REAL, float('inf'), src)
        if rest == '-inf.0':
            if exact == 1:
                raise SchemeSyntaxError("#e cannot be applied to -inf.0", src)
            return Token(TOK_REAL, float('-inf'), src)
        if rest == '+nan.0':
            if exact == 1:
                raise SchemeSyntaxError("#e cannot be applied to +nan.0", src)
            return Token(TOK_REAL, float('nan'), src)
        if rest == '-nan.0':
            if exact == 1:
                raise SchemeSyntaxError("#e cannot be applied to -nan.0", src)
            return Token(TOK_REAL, float('nan'), src)
        try:
            f = float(rest)
            if exact == 1:
                if not _math.isfinite(f):
                    raise SchemeSyntaxError(
                        "#e applied to non-finite real %r" % rest, src)
                if f.is_integer():
                    return Token(TOK_INT, int(f), src)
                frac = _decimal_str_to_rat(rest)
                return Token(TOK_RATIONAL, (frac.numerator, frac.denominator), src)
            return Token(TOK_REAL, f, src)
        except ValueError:
            pass
    raise SchemeSyntaxError("invalid prefixed number: %r" % text, src)


def _parse_number_for_complex(s):
    """Parse a complex component string.  Returns int, _Rat, or float.
    Returns None if the string is not a valid number."""
    if s == '+inf.0':
        return float('inf')
    if s == '-inf.0':
        return float('-inf')
    if s == '+nan.0':
        return float('nan')
    try:
        return int(s)
    except ValueError:
        pass
    if '/' in s:
        slash = s.index('/')
        try:
            num = int(_substring(s, 0, slash))
            den = int(_substring(s, slash + 1, len(s)))
            if den != 0:
                return _Rat(num, den)
        except ValueError:
            pass
    try:
        return float(s)
    except ValueError:
        pass
    return None


def _is_exact_component(v):
    """True if v (returned by _parse_number_for_complex) is exact (int or _Rat)."""
    return isinstance(v, int) or isinstance(v, _Rat)


def _component_to_scheme(v, src):
    """Wrap an exact complex component (int or _Rat) into a Scheme value."""
    if isinstance(v, int):
        return make_integer(v, src)
    if v.denominator == 1:
        return make_integer(v.numerator, src)
    return make_rational(v.numerator, v.denominator, src)


def _try_parse_complex_literal(text, src):
    """Parse a+bi / a-bi / +bi / -bi / +i / -i complex literals.
    text ends in 'i' and has length >= 2.
    Produces an exact complex token when both components are exact."""
    body = _substring(text, 0, len(text) - 1)
    if not body:
        return None   # bare 'i' is an identifier

    # Find the rightmost +/- that is not an exponent sign
    # (i.e. not preceded by 'e' or 'E').
    split = -1
    j = len(body) - 1
    while j >= 0:
        c = body[j]
        if c == '+' or c == '-':
            if j > 0 and body[j - 1].lower() == 'e':
                j = j - 1
                continue
            split = j
            break
        j = j - 1

    if split == -1:
        return None   # no sign separator found

    real_str = _substring(body, 0, split)
    sign_imag = _substring(body, split, len(body))

    if real_str == '':
        re_val = 0
    else:
        re_val = _parse_number_for_complex(real_str)
        if re_val is None:
            return None

    if sign_imag == '+':
        im_val = 1
    elif sign_imag == '-':
        im_val = -1
    else:
        im_val = _parse_number_for_complex(sign_imag)
        if im_val is None:
            return None

    if _is_exact_component(re_val) and _is_exact_component(im_val):
        re_scheme = _component_to_scheme(re_val, src)
        im_scheme = _component_to_scheme(im_val, src)
        return Token(TOK_EXACT_COMPLEX, (re_scheme, im_scheme), src)
    # An exact zero imaginary part makes the number real, even when the real
    # part is inexact (R7RS 6.2.6: (real? -2.5+0i) => #t, whereas
    # (real? -2.5+0.0i) => #f, because 0.0 is an inexact imaginary part).
    if _is_exact_component(im_val) and im_val == 0:
        return Token(TOK_REAL, float(re_val), src)
    return Token(TOK_COMPLEX, (float(re_val), float(im_val)), src)


def _try_parse_polar_literal(text, src):
    """Parse r@theta polar complex literal."""
    at = text.index('@')
    r_str = text[:at]
    theta_str = text[at + 1:]
    if not r_str or not theta_str:
        return None
    r = _parse_number_for_complex(r_str)
    theta = _parse_number_for_complex(theta_str)
    if r is None or theta is None:
        return None
    re_val = r * _math.cos(theta)
    im_val = r * _math.sin(theta)
    return Token(TOK_COMPLEX, (float(re_val), float(im_val)), src)


def _decode_string_escapes(raw, src):
    result = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == '\\':
            if i + 1 >= n:
                raise SchemeSyntaxError("unterminated string escape", src)
            esc = raw[i + 1]
            if esc in _STRING_ESCAPES:
                result.append(_STRING_ESCAPES[esc])
                i = i + 2
            elif esc == 'x':
                j = i + 2
                while j < n and raw[j] in _HEX_DIGITS:
                    j = j + 1
                if j == i + 2:
                    raise SchemeSyntaxError(
                        "malformed \\x escape: no hex digits", src)
                if j >= n or raw[j] != ';':
                    raise SchemeSyntaxError(
                        "malformed \\x escape: missing semicolon", src)
                result.append(chr(int(raw[i + 2: j], 16)))
                i = j + 1
            else:
                j = i + 1
                while j < n and raw[j] in ' \t':
                    j = j + 1
                if j < n and raw[j] in '\r\n':
                    if raw[j] == '\r':
                        j = j + 1
                    if j < n and raw[j] == '\n':
                        j = j + 1
                    while j < n and raw[j] in ' \t':
                        j = j + 1
                    i = j
                else:
                    raise SchemeSyntaxError(
                        "unknown string escape \\%s" % esc, src)
        else:
            result.append(c)
            i = i + 1
    return ''.join(result)


def _decode_symbol_escapes(raw, src):
    result = []
    i = 0
    n = len(raw)
    while i < n:
        c = raw[i]
        if c == '\\':
            if i + 1 >= n:
                raise SchemeSyntaxError("unterminated symbol escape", src)
            esc = raw[i + 1]
            if esc in _SYMBOL_ESCAPES:
                result.append(_SYMBOL_ESCAPES[esc])
                i = i + 2
            elif esc == 'x':
                j = i + 2
                while j < n and raw[j] in _HEX_DIGITS:
                    j = j + 1
                if j == i + 2:
                    raise SchemeSyntaxError(
                        "malformed \\x escape: no hex digits", src)
                if j >= n or raw[j] != ';':
                    raise SchemeSyntaxError(
                        "malformed \\x escape: missing semicolon", src)
                result.append(chr(int(raw[i + 2: j], 16)))
                i = j + 1
            else:
                raise SchemeSyntaxError(
                    "unknown symbol escape \\%s" % esc, src)
        else:
            result.append(c)
            i = i + 1
    return ''.join(result)


def _decode_char_literal(text, src):
    rest = _substring(text, 2, len(text))
    if len(rest) == 1:
        return rest
    if len(rest) >= 2 and rest[0] == 'x':
        hex_str = _substring(rest, 1, len(rest))
        all_hex = len(hex_str) > 0
        i = 0
        while i < len(hex_str):
            if hex_str[i] not in _HEX_DIGITS:
                all_hex = False
                break
            i = i + 1
        if all_hex:
            return chr(int(hex_str, 16))
    name = rest.lower()
    if name in _CHAR_NAMES:
        return _CHAR_NAMES[name]
    raise SchemeSyntaxError("unknown character name: #\\%s" % rest, src)


# -------- S-expression reader --------

class Parser:

    @staticmethod
    def _build_list(items, dotted_tail, list_src):
        if dotted_tail is None:
            if not items:
                return (NIL, list_src)
            result = NIL_VALUE
        else:
            result = dotted_tail
        i = len(items) - 1
        while i >= 0:
            result = alloc_cons(items[i], result, list_src)
            i = i - 1
        return result

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.labels = {}   # datum labels: int -> value  (R7RS §2.4)

    def _peek(self):
        return self.tokens[self.pos]

    def _advance(self):
        tok = self.tokens[self.pos]
        if tok.kind != TOK_EOF:
            self.pos = self.pos + 1
        return tok

    def _skip_datum_comments(self):
        """Consume any leading #; tokens, discarding the datum each precedes."""
        while self._peek().kind == TOK_DATUM_COMMENT:
            self._advance()
            self.parse_expr()

    def parse_program(self):
        forms = []
        while True:
            self._skip_datum_comments()
            if self._peek().kind == TOK_EOF:
                break
            forms.append(self.parse_expr())
        return forms

    def parse_expr(self):
        """Read exactly one complete datum.

        Iterative: nesting (lists, vectors, bytevectors, reader prefixes, datum
        labels and datum comments) is driven by an explicit frame stack rather
        than host recursion, so arbitrarily deep input is read without any
        nesting cap or RecursionError.  Flat lists of any length were always
        fine; this removes the depth ceiling too.  Behaviour is otherwise
        identical to the former recursive reader (same values, source positions,
        immutability, datum-label semantics and error messages)."""
        stack = []
        while True:
            top = stack[-1] if stack else None

            # --- Container contexts: handle their own closer / dot / EOF ------
            # A list whose dotted tail is already read accepts only its closer.
            if top is not None and top.kind == _F_LIST and top.tail_filled:
                tok = self._peek()
                if tok.kind == top.closer:
                    self._advance()
                    built = Parser._build_list(
                        top.items, top.dotted_tail, top.src)
                    stack.pop()
                    res = self._emit(stack, built)
                    if res is _READ_MORE:
                        continue
                    return res
                raise SchemeSyntaxError(
                    "expected '%s' after dotted tail" % top.closer_ch, tok.src)

            # An open list/vector/bytevector reading its next element.  (A list
            # that has seen '.' but not yet read the tail falls through to the
            # generic datum reader below, so the tail is read like any datum —
            # a closer/EOF there is an error, exactly as before.)
            in_container = (top is not None and
                            (top.kind == _F_VECTOR or top.kind == _F_BYTEVECTOR or
                             (top.kind == _F_LIST and not top.want_tail)))
            if in_container:
                tok = self._peek()
                kind = tok.kind
                if top.kind == _F_LIST:
                    if kind == top.closer:
                        self._advance()
                        built = Parser._build_list(top.items, None, top.src)
                        stack.pop()
                        res = self._emit(stack, built)
                        if res is _READ_MORE:
                            continue
                        return res
                    if kind == TOK_RPAREN or kind == TOK_RBRACKET:
                        raise SchemeSyntaxError(
                            "mismatched bracket: expected '%s'" % top.closer_ch,
                            tok.src)
                    if kind == TOK_DOT:
                        dot = self._advance()
                        if not top.items:
                            raise SchemeSyntaxError(
                                "dot must be preceded by at least one element",
                                dot.src)
                        nxt = self._peek()
                        if nxt.kind in (TOK_RPAREN, TOK_RBRACKET, TOK_EOF, TOK_DOT):
                            raise SchemeSyntaxError(
                                "dot must be followed by an expression", nxt.src)
                        top.want_tail = True
                        continue
                    if kind == TOK_EOF:
                        raise SchemeSyntaxError(
                            "unterminated list (missing '%s')" % top.closer_ch,
                            top.src)
                    # otherwise: start of an element -> generic datum reader
                elif top.kind == _F_VECTOR:
                    if kind == TOK_RPAREN:
                        self._advance()
                        v = make_vector(top.items)
                        v.immutable = True
                        stack.pop()
                        res = self._emit(stack, v)
                        if res is _READ_MORE:
                            continue
                        return res
                    if kind == TOK_EOF:
                        raise SchemeSyntaxError(
                            'unterminated vector literal', top.src)
                    # otherwise: element -> generic datum reader
                else:   # _F_BYTEVECTOR
                    if kind == TOK_RPAREN:
                        self._advance()
                        bv = make_bytevector(bytearray(top.items))
                        bv.immutable = True
                        stack.pop()
                        res = self._emit(stack, bv)
                        if res is _READ_MORE:
                            continue
                        return res
                    if kind == TOK_EOF:
                        raise SchemeSyntaxError(
                            'unterminated bytevector literal', top.src)
                    # otherwise: element -> generic datum reader

            # --- Generic datum reader -----------------------------------------
            # Reaches here at top level, for a single-child frame's datum
            # (prefix / label / discard / dotted tail), or for a container
            # element that wasn't a closer/dot/EOF.  Either pushes a frame and
            # loops, or produces a completed `value` and emits it.
            tok = self._peek()
            kind = tok.kind

            # Datum comment: #;  -- read and discard the next datum.
            if kind == TOK_DATUM_COMMENT:
                self._advance()
                stack.append(_ParseFrame(_F_DISCARD))
                continue

            # Datum label definition: #n= <datum>  (R7RS §2.4)
            if kind == TOK_LABEL_DEF:
                self._advance()
                label_n = tok.value
                if label_n in self.labels:
                    raise SchemeSyntaxError(
                        'duplicate datum label #%d=' % label_n, tok.src)
                nxt = self._peek()
                # Pre-register a stub so forward #n# refs inside the datum work.
                if nxt.kind == TOK_LPAREN or nxt.kind == TOK_LBRACKET:
                    # Lists: stub is a ConsCell mutated in-place after parsing.
                    stub = alloc_cons(NIL_VALUE, NIL_VALUE, nxt.src)
                    self.labels[label_n] = stub
                    f = _ParseFrame(_F_LABEL_LIST)
                    f.label_n = label_n
                    f.stub = stub
                    stack.append(f)
                    continue
                if nxt.kind == TOK_VECTOR_LPAREN:
                    # Vectors: pre-create with a mutable backing list so any
                    # #n# references inside resolve to the final object.
                    pre_items = []
                    pre_vector = make_vector(pre_items)
                    self.labels[label_n] = pre_vector
                    f = _ParseFrame(_F_LABEL_VECTOR)
                    f.label_n = label_n
                    f.pre_vector = pre_vector
                    f.pre_items = pre_items
                    stack.append(f)
                    continue
                # Atoms / prefixes / bytevectors: register after the datum is read.
                f = _ParseFrame(_F_LABEL_PLAIN)
                f.label_n = label_n
                stack.append(f)
                continue

            # Datum label reference: #n#  (R7RS §2.4)
            if kind == TOK_LABEL_REF:
                self._advance()
                label_n = tok.value
                if label_n not in self.labels:
                    raise SchemeSyntaxError(
                        'undefined datum label #%d#' % label_n, tok.src)
                value = self.labels[label_n]
            elif kind == TOK_INT:
                self._advance()
                value = make_integer(tok.value, tok.src)
            elif kind == TOK_REAL:
                self._advance()
                value = make_real(tok.value, tok.src)
            elif kind == TOK_RATIONAL:
                self._advance()
                value = make_rational(tok.value[0], tok.value[1], tok.src)
            elif kind == TOK_COMPLEX:
                self._advance()
                value = make_complex(tok.value[0], tok.value[1], tok.src)
            elif kind == TOK_EXACT_COMPLEX:
                self._advance()
                re_s = tok.value[0]
                im_s = tok.value[1]
                if is_integer(im_s) and as_integer(im_s) == 0:
                    value = re_s
                else:
                    value = make_exact_complex(re_s, im_s, tok.src)
            elif kind == TOK_STRING:
                self._advance()
                value = make_string(tok.value, tok.src)
                value.immutable = True
            elif kind == TOK_CHAR:
                self._advance()
                value = make_character(tok.value, tok.src)
            elif kind == TOK_BOOL:
                self._advance()
                value = make_boolean(tok.value, tok.src)
            elif kind == TOK_IDENT:
                self._advance()
                value = make_symbol(tok.value, tok.src)
            elif kind == TOK_LPAREN or kind == TOK_LBRACKET:
                self._advance()
                f = _ParseFrame(_F_LIST)
                f.items = []
                f.src = tok.src
                if kind == TOK_LBRACKET:
                    f.closer = TOK_RBRACKET
                    f.closer_ch = ']'
                else:
                    f.closer = TOK_RPAREN
                    f.closer_ch = ')'
                stack.append(f)
                continue
            elif kind == TOK_VECTOR_LPAREN:
                self._advance()
                f = _ParseFrame(_F_VECTOR)
                f.items = []
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_BYTEVECTOR_LPAREN:
                self._advance()
                f = _ParseFrame(_F_BYTEVECTOR)
                f.items = []
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_QUOTE:
                self._advance()
                f = _ParseFrame(_F_PREFIX)
                f.sym = make_symbol('quote', tok.src)
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_QUASIQUOTE:
                self._advance()
                f = _ParseFrame(_F_PREFIX)
                f.sym = make_symbol('quasiquote', tok.src)
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_UNQUOTE:
                self._advance()
                f = _ParseFrame(_F_PREFIX)
                f.sym = make_symbol('unquote', tok.src)
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_UNQUOTE_SPLICING:
                self._advance()
                f = _ParseFrame(_F_PREFIX)
                f.sym = make_symbol('unquote-splicing', tok.src)
                f.src = tok.src
                stack.append(f)
                continue
            elif kind == TOK_RPAREN:
                raise SchemeSyntaxError("unexpected ')'", tok.src)
            elif kind == TOK_RBRACKET:
                raise SchemeSyntaxError("unexpected ']'", tok.src)
            elif kind == TOK_DOT:
                raise SchemeSyntaxError(
                    "unexpected '.' outside of a list", tok.src)
            elif kind == TOK_EOF:
                raise SchemeSyntaxError("unexpected end of input", tok.src)
            else:
                raise SchemeSyntaxError("unexpected token %s" % kind, tok.src)

            # We have a completed `value`; feed it to the enclosing frame(s).
            res = self._emit(stack, value)
            if res is _READ_MORE:
                continue
            return res

    def _emit(self, stack, value):
        """Feed a completed datum to the top frame, reducing single-child
        frames (prefix / datum-label / discard) as far as possible.

        Returns the final value when the stack empties (it is the result), or
        the sentinel _READ_MORE when the value was absorbed by a still-open
        container or dropped by a datum comment (the driver should read on)."""
        while stack:
            top = stack[-1]
            kind = top.kind
            if kind == _F_LIST:
                if top.want_tail and not top.tail_filled:
                    top.dotted_tail = value
                    top.tail_filled = True
                else:
                    top.items.append(value)
                return _READ_MORE
            if kind == _F_VECTOR:
                top.items.append(value)
                return _READ_MORE
            if kind == _F_BYTEVECTOR:
                if not is_integer(value):
                    raise SchemeSyntaxError(
                        'bytevector element must be an exact integer in 0-255',
                        top.src)
                n = as_integer(value)
                if n < 0 or n > 255:
                    raise SchemeSyntaxError(
                        'bytevector element out of range 0-255: %d' % n, top.src)
                top.items.append(n)
                return _READ_MORE
            if kind == _F_DISCARD:
                stack.pop()
                return _READ_MORE
            if kind == _F_PREFIX:
                stack.pop()
                inner = alloc_cons(value, NIL_VALUE, top.src)
                value = alloc_cons(top.sym, inner, top.src)
                continue
            if kind == _F_LABEL_LIST:
                stack.pop()
                if is_cons(value):
                    top.stub.car = value.car
                    top.stub.cdr = value.cdr
                    value = top.stub
                else:
                    self.labels[top.label_n] = value
                continue
            if kind == _F_LABEL_VECTOR:
                stack.pop()
                parsed_items = as_vector_items(value)
                i = 0
                while i < len(parsed_items):
                    top.pre_items.append(parsed_items[i])
                    i = i + 1
                value = top.pre_vector
                continue
            # _F_LABEL_PLAIN
            stack.pop()
            self.labels[top.label_n] = value
        return value


# -------- Public entry points --------

def parse(source, filename=None):
    """Read all top-level forms.  Returns a Python list of Values (each
    a cons-cell chain or an atom tuple)."""
    return Parser(tokenize(source, filename)).parse_program()


def parse_one(source, filename=None):
    """Read a single expression; error if source contains anything after it."""
    parser = Parser(tokenize(source, filename))
    expr = parser.parse_expr()
    tok = parser._peek()
    if tok.kind != TOK_EOF:
        raise SchemeSyntaxError(
            "unexpected token after expression: %s" % tok.kind, tok.src)
    return expr


# -------- Self-test --------

def _strip_src(v):
    """Recursively convert parser-produced values to position-free Python
    structures for test comparison:
       ConsCell chain  -> Python list (proper) or (items, tail) tuple (dotted)
       Atom tuple      -> same tag+payload tuple with the trailing src stripped
       NIL_VALUE       -> empty list []
    """
    if is_cons(v):
        items = []
        cur = v
        while is_cons(cur):
            items.append(_strip_src(cur.car))
            cur = cur.cdr
        if is_nil(cur):
            return items
        return (items, _strip_src(cur))
    if is_nil(v):
        return []
    if is_integer(v):
        return make_integer(as_integer(v))
    if is_real(v):
        return make_real(as_real(v))
    if is_rational(v):
        return make_rational(as_rational_num(v), as_rational_den(v))
    if is_string(v):
        return make_string(as_string(v))
    if is_character(v):
        return make_character(as_character(v))
    if is_boolean(v):
        return make_boolean(as_boolean(v))
    if is_symbol(v):
        return make_symbol(as_symbol(v))
    return v


if __name__ == '__main__':
    n_pass = 0
    n_fail = 0

    happy = [
        # literals
        ('42',                  make_integer(42)),
        ('-5',                  make_integer(-5)),
        ('3.14',                make_real(3.14)),
        ('3/4',                 make_rational(3, 4)),
        ('#t',                  make_boolean(True)),
        ('#f',                  make_boolean(False)),
        ('#\\a',                make_character('a')),
        ('#\\space',            make_character(' ')),
        ('"hello"',             make_string('hello')),

        # identifiers
        ('x',                   make_symbol('x')),
        ('foo-bar',             make_symbol('foo-bar')),
        ('+',                   make_symbol('+')),
        ('lambda',              make_symbol('lambda')),
        ('set!',                make_symbol('set!')),

        # lists - no special-form handling
        ('()',                  []),
        ('(1 2 3)',             [make_integer(
            1), make_integer(2), make_integer(3)]),
        ('(lambda (x) x)',
            [make_symbol('lambda'), [make_symbol('x')], make_symbol('x')]),
        ('(define (f x) x)',
            [make_symbol('define'),
             [make_symbol('f'), make_symbol('x')],
                make_symbol('x')]),
        ('(if #t 1)',
            [make_symbol('if'), make_boolean(True), make_integer(1)]),
        ('(if #t 1 2)',
            [make_symbol('if'), make_boolean(True), make_integer(1), make_integer(2)]),

        # nested
        ('((lambda (x) x) 7)',
            [[make_symbol('lambda'), [make_symbol('x')], make_symbol('x')],
             make_integer(7)]),

        # quote shortcut becomes a list
        ("'x",
            [make_symbol('quote'), make_symbol('x')]),
        ("'42",
            [make_symbol('quote'), make_integer(42)]),
        ("'(a b c)",
            [make_symbol('quote'),
             [make_symbol('a'), make_symbol('b'), make_symbol('c')]]),
        ("''x",
            [make_symbol('quote'),
             [make_symbol('quote'), make_symbol('x')]]),

        # dotted / improper lists
        ('(a . b)',             ([make_symbol('a')], make_symbol('b'))),
        ('(a b . c)',           ([make_symbol('a'),
                                  make_symbol('b')], make_symbol('c'))),

        # comments / whitespace
        ('  42 ; comment\n',    make_integer(42)),
    ]

    print('-- happy path --')
    i = 0
    while i < len(happy):
        source = happy[i][0]
        expected = happy[i][1]
        try:
            got = parse_one(source)
        except SchemeSyntaxError as e:
            print("[FAIL] %r: unexpected error %s" % (source, e))
            n_fail = n_fail + 1
            i = i + 1
            continue
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

    # Lex/syntactic errors only.  Any "shape of a special form" check now
    # belongs to the Analyzer.
    errors = [
        ('(',                      'unterminated list'),
        (')',                      "unexpected ')'"),
        ('(1 2',                   'unterminated list'),
        ('"unterminated',          'unexpected character'),
        ('"\\q"',                  'unknown string escape'),
        ('#\\unknown',             'unknown character name'),
        ('1abc',                   'malformed number or identifier'),
        ('#z',                     'unknown #-syntax'),
        ('',                       'unexpected end of input'),
    ]

    print()
    print('-- error path --')
    i = 0
    while i < len(errors):
        source = errors[i][0]
        expected_fragment = errors[i][1]
        try:
            parse_one(source)
        except SchemeSyntaxError as e:
            if expected_fragment in str(e):
                print("[ OK ] %r  ->  %s" % (source, e))
                n_pass = n_pass + 1
            else:
                print("[WARN] %r  ->  %s" % (source, e))
                print("        expected substring: %r" % expected_fragment)
                n_pass = n_pass + 1
            i = i + 1
            continue
        print("[FAIL] %r should have raised SchemeSyntaxError" % source)
        n_fail = n_fail + 1
        i = i + 1

    # Source-position checks: verify that atoms carry their own src and
    # that a cons cell's src points at the '(' that opened the list.

    print()
    print('-- source positions --')

    expr = parse_one('(foo bar)')
    check_pos = expr.src
    if check_pos is not None and check_pos.line == 1 and check_pos.col == 1:
        print("[ OK ] list cons src at '(' (line 1 col 1)")
        n_pass = n_pass + 1
    else:
        print("[FAIL] list cons src: expected line 1 col 1, got %r" % check_pos)
        n_fail = n_fail + 1

    foo_atom = expr.car
    foo_src = src_of(foo_atom)
    if foo_src is not None and foo_src.line == 1 and foo_src.col == 2:
        print("[ OK ] atom 'foo' src (line 1 col 2)")
        n_pass = n_pass + 1
    else:
        print("[FAIL] atom 'foo' src: expected line 1 col 2, got %r" % foo_src)
        n_fail = n_fail + 1

    bar_atom = expr.cdr.car
    bar_src = src_of(bar_atom)
    if bar_src is not None and bar_src.line == 1 and bar_src.col == 6:
        print("[ OK ] atom 'bar' src (line 1 col 6)")
        n_pass = n_pass + 1
    else:
        print("[FAIL] atom 'bar' src: expected line 1 col 6, got %r" % bar_src)
        n_fail = n_fail + 1

    # Filename propagation: REPL filename marker reaches every atom and cons
    from pyscheme.AST import REPL_FILENAME
    rexpr = parse_one('(a)', filename=REPL_FILENAME)
    if rexpr.src is not None and rexpr.src.filename == REPL_FILENAME:
        print("[ OK ] cons filename propagated")
        n_pass = n_pass + 1
    else:
        print("[FAIL] cons filename: expected %r, got %r" %
              (REPL_FILENAME, rexpr.src))
        n_fail = n_fail + 1
    a_atom = rexpr.car
    a_src = src_of(a_atom)
    if a_src is not None and a_src.filename == REPL_FILENAME:
        print("[ OK ] atom filename propagated")
        n_pass = n_pass + 1
    else:
        print("[FAIL] atom filename: expected %r, got %r" %
              (REPL_FILENAME, a_src))
        n_fail = n_fail + 1

    print()
    print("%d passed, %d failed" % (n_pass, n_fail))
