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

from pyscheme.AST import (
   alloc_cons, NIL_VALUE, SourceInfo, ConsCell, is_cons, is_nil,
   is_integer, is_real, is_rational, is_string, is_character,
   is_boolean, is_symbol,
   as_integer, as_real, as_string, as_character, as_boolean, as_symbol,
   as_rational_num, as_rational_den,
   make_integer, make_real, make_rational, make_string, make_character,
   make_boolean, make_symbol,
   REAL, RATIONAL, INTEGER, CHARACTER, BOOLEAN, STRING, SYMBOL, NIL,
)
from pyscheme.Environment import _PositionedSchemeError


# -------- Tokens --------

TOK_LPAREN            = 'LPAREN'
TOK_RPAREN            = 'RPAREN'
TOK_QUOTE             = 'QUOTE'
TOK_QUASIQUOTE        = 'QUASIQUOTE'
TOK_UNQUOTE           = 'UNQUOTE'
TOK_UNQUOTE_SPLICING  = 'UNQUOTE_SPLICING'
TOK_DOT               = 'DOT'
TOK_INT               = 'INT'
TOK_REAL              = 'REAL'
TOK_RATIONAL          = 'RATIONAL'
TOK_STRING            = 'STRING'
TOK_CHAR              = 'CHAR'
TOK_BOOL              = 'BOOL'
TOK_IDENT             = 'IDENT'
TOK_EOF               = 'EOF'


class Token:
   """Lexer token.  POD; no methods except __init__.  src holds the
   SourceInfo for the token's starting position.  The parser uses src
   when building atom tuples and cons cells."""

   def __init__(self, kind, value, src):
      self.kind  = kind
      self.value = value
      self.src   = src


class SchemeSyntaxError(_PositionedSchemeError):
   """Raised on any lex/read error."""
   pass


# -------- Lexer --------
#
# Number patterns use a trailing lookahead (?=[\s()'";]|$) so "1abc"
# does not greedily tokenize as INT 1 + IDENT abc - it falls through
# to IDENT, where the digit-prefix check rejects it.

_TOKEN_RE = re.compile(r'''
      [ \r\n]+                                                             # whitespace (tabs pre-expanded)
    | ;[^\n]*                                                              # line comment
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<QUASIQUOTE>`)
    | (?P<UNQUOTE_SPLICING>,@)
    | (?P<UNQUOTE>,)
    | (?P<QUOTE>')
    | (?P<DOT>\.(?=[\s()'"`,;]|$))                                         # lone . is the dotted-pair marker
    | (?P<STRING>"(?:[^"\\]|\\.)*")
    | (?P<CHAR>\#\\(?:[a-zA-Z]+|.))
    | (?P<TRUE>\#t(?:rue)?)
    | (?P<FALSE>\#f(?:alse)?)
    | (?P<RATIONAL>[+-]?\d+/\d+(?=[\s()'"`,;]|$))
    | (?P<REAL>
          [+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?(?=[\s()'"`,;]|$)
        | [+-]?\d+[eE][+-]?\d+(?=[\s()'"`,;]|$)
      )
    | (?P<INT>[+-]?\d+(?=[\s()'"`,;]|$))
    | (?P<IDENT>[^\s()'"`,;]+)
''', re.VERBOSE)


_STRING_ESCAPES = {
   'n':  '\n',
   't':  '\t',
   'r':  '\r',
   '\\': '\\',
   '"':  '"',
   'a':  '\a',
   'b':  '\b',
   '0':  '\0',
}

_CHAR_NAMES = {
   'space':     ' ',
   'newline':   '\n',
   'tab':       '\t',
   'return':    '\r',
   'null':      '\0',
   'nul':       '\0',
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


def _substring(s, start, end):
   """Return s[start:end] as an explicit character-by-character copy.
   Ports to strncpy + null terminator in C."""
   result = ''
   i = start
   while i < end:
      result = result + s[i]
      i = i + 1
   return result


def tokenize(source, filename=None):
   source       = source.expandtabs()
   source_lines = source.splitlines()

   tokens = []
   pos  = 0
   line = 1
   col  = 1
   n    = len(source)
   while pos < n:
      match = _TOKEN_RE.match(source, pos)
      if not match:
         raise SchemeSyntaxError(
            "unexpected character %r" % source[pos],
            _make_src(line, col, source_lines, filename))
      text = match.group(0)
      kind = match.lastgroup
      if kind is not None:
         src = _make_src(line, col, source_lines, filename)
         tokens.append(_build_token(kind, text, src))
      newlines = text.count('\n')
      if newlines > 0:
         line = line + newlines
         col  = len(text) - text.rfind('\n')
      else:
         col  = col + len(text)
      pos = match.end()
   tokens.append(Token(TOK_EOF, None, _make_src(line, col, source_lines, filename)))
   return tokens


def _build_token(kind, text, src):
   if kind == 'LPAREN':
      return Token(TOK_LPAREN, '(', src)
   if kind == 'RPAREN':
      return Token(TOK_RPAREN, ')', src)
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
                   _decode_string_escapes(_substring(text, 1, len(text) - 1), src),
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
   if kind == 'IDENT':
      if text.startswith('#'):
         raise SchemeSyntaxError("unknown #-syntax: %r" % text, src)
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
   if s[0] in '+-' and len(s) > 1 and (s[1].isdigit() or s[1] == '.'):
      return True
   if s[0] == '.' and len(s) > 1 and s[1].isdigit():
      return True
   return False


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
         else:
            raise SchemeSyntaxError("unknown string escape \\%s" % esc, src)
         i = i + 2
      else:
         result.append(c)
         i = i + 1
   return ''.join(result)


def _decode_char_literal(text, src):
   rest = _substring(text, 2, len(text))
   if len(rest) == 1:
      return rest
   name = rest.lower()
   if name in _CHAR_NAMES:
      return _CHAR_NAMES[name]
   raise SchemeSyntaxError("unknown character name: #\\%s" % rest, src)


# -------- S-expression reader --------

class Parser:
   def __init__(self, tokens):
      self.tokens = tokens
      self.pos    = 0

   def _peek(self):
      return self.tokens[self.pos]

   def _advance(self):
      tok = self.tokens[self.pos]
      if tok.kind != TOK_EOF:
         self.pos = self.pos + 1
      return tok

   def parse_program(self):
      forms = []
      while self._peek().kind != TOK_EOF:
         forms.append(self.parse_expr())
      return forms

   def parse_expr(self):
      tok  = self._peek()
      kind = tok.kind
      if kind == TOK_INT:
         self._advance()
         return make_integer(tok.value, tok.src)
      if kind == TOK_REAL:
         self._advance()
         return make_real(tok.value, tok.src)
      if kind == TOK_RATIONAL:
         self._advance()
         return make_rational(tok.value[0], tok.value[1], tok.src)
      if kind == TOK_STRING:
         self._advance()
         return make_string(tok.value, tok.src)
      if kind == TOK_CHAR:
         self._advance()
         return make_character(tok.value, tok.src)
      if kind == TOK_BOOL:
         self._advance()
         return make_boolean(tok.value, tok.src)
      if kind == TOK_IDENT:
         self._advance()
         return make_symbol(tok.value, tok.src)
      if kind == TOK_LPAREN:
         return self._read_list()
      if kind == TOK_QUOTE:
         quote_tok = self._advance()
         datum     = self.parse_expr()
         quote_sym = make_symbol('quote', quote_tok.src)
         inner = alloc_cons(datum, NIL_VALUE, quote_tok.src)
         return alloc_cons(quote_sym, inner, quote_tok.src)
      if kind == TOK_QUASIQUOTE:
         qq_tok = self._advance()
         datum  = self.parse_expr()
         qq_sym = make_symbol('quasiquote', qq_tok.src)
         inner  = alloc_cons(datum, NIL_VALUE, qq_tok.src)
         return alloc_cons(qq_sym, inner, qq_tok.src)
      if kind == TOK_UNQUOTE:
         uq_tok = self._advance()
         datum  = self.parse_expr()
         uq_sym = make_symbol('unquote', uq_tok.src)
         inner  = alloc_cons(datum, NIL_VALUE, uq_tok.src)
         return alloc_cons(uq_sym, inner, uq_tok.src)
      if kind == TOK_UNQUOTE_SPLICING:
         us_tok = self._advance()
         datum  = self.parse_expr()
         us_sym = make_symbol('unquote-splicing', us_tok.src)
         inner  = alloc_cons(datum, NIL_VALUE, us_tok.src)
         return alloc_cons(us_sym, inner, us_tok.src)
      if kind == TOK_RPAREN:
         raise SchemeSyntaxError("unexpected ')'", tok.src)
      if kind == TOK_DOT:
         raise SchemeSyntaxError("unexpected '.' outside of a list", tok.src)
      if kind == TOK_EOF:
         raise SchemeSyntaxError("unexpected end of input", tok.src)
      raise SchemeSyntaxError("unexpected token %s" % kind, tok.src)

   def _read_list(self):
      lparen = self._advance()   # consume '('
      items  = []
      dotted_tail = None
      while True:
         tok = self._peek()
         if tok.kind == TOK_RPAREN:
            self._advance()
            return _build_list(items, dotted_tail, lparen.src)
         if tok.kind == TOK_DOT:
            dot = self._advance()
            if not items:
               raise SchemeSyntaxError(
                  "dot must be preceded by at least one element", dot.src)
            nxt = self._peek()
            if nxt.kind == TOK_RPAREN or nxt.kind == TOK_EOF or nxt.kind == TOK_DOT:
               raise SchemeSyntaxError(
                  "dot must be followed by an expression", nxt.src)
            dotted_tail = self.parse_expr()
            closing = self._peek()
            if closing.kind != TOK_RPAREN:
               raise SchemeSyntaxError(
                  "expected ')' after dotted tail", closing.src)
            self._advance()
            return _build_list(items, dotted_tail, lparen.src)
         if tok.kind == TOK_EOF:
            raise SchemeSyntaxError(
               "unterminated list (missing ')')", lparen.src)
         items.append(self.parse_expr())


def _build_list(items, dotted_tail, list_src):
   """Fold a Python list of Values into a cons-cell chain.
   All cells share list_src (the position of the opening '(').
   If dotted_tail is non-None, the innermost cdr is that value instead
   of NIL_VALUE.  A literal () in source is returned as a positioned
   nil (NIL, list_src) so diagnostics that reject () as an expression
   can point at its '('."""
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


# -------- Public entry points --------

def parse(source, filename=None):
   """Read all top-level forms.  Returns a Python list of Values (each
   a cons-cell chain or an atom tuple)."""
   return Parser(tokenize(source, filename)).parse_program()


def parse_one(source, filename=None):
   """Read a single expression; error if source contains anything after it."""
   parser = Parser(tokenize(source, filename))
   expr   = parser.parse_expr()
   tok    = parser._peek()
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
      cur   = v
      while is_cons(cur):
         items.append(_strip_src(cur.car))
         cur = cur.cdr
      if is_nil(cur):
         return items
      return (items, _strip_src(cur))
   if is_nil(v):
      return []
   if is_integer(v):     return (INTEGER,   as_integer(v))
   if is_real(v):        return (REAL,      as_real(v))
   if is_rational(v):    return (RATIONAL,  as_rational_num(v), as_rational_den(v))
   if is_string(v):      return (STRING,    as_string(v))
   if is_character(v):   return (CHARACTER, as_character(v))
   if is_boolean(v):     return (BOOLEAN,   as_boolean(v))
   if is_symbol(v):      return (SYMBOL,    as_symbol(v))
   return v


if __name__ == '__main__':
   n_pass = 0
   n_fail = 0

   happy = [
      # literals
      ('42',                  (INTEGER, 42)),
      ('-5',                  (INTEGER, -5)),
      ('3.14',                (REAL, 3.14)),
      ('3/4',                 (RATIONAL, 3, 4)),
      ('#t',                  (BOOLEAN, True)),
      ('#f',                  (BOOLEAN, False)),
      ('#\\a',                (CHARACTER, 'a')),
      ('#\\space',            (CHARACTER, ' ')),
      ('"hello"',             (STRING, 'hello')),

      # identifiers
      ('x',                   (SYMBOL, 'x')),
      ('foo-bar',             (SYMBOL, 'foo-bar')),
      ('+',                   (SYMBOL, '+')),
      ('lambda',              (SYMBOL, 'lambda')),
      ('set!',                (SYMBOL, 'set!')),

      # lists - no special-form handling
      ('()',                  []),
      ('(1 2 3)',             [(INTEGER, 1), (INTEGER, 2), (INTEGER, 3)]),
      ('(lambda (x) x)',
         [(SYMBOL, 'lambda'), [(SYMBOL, 'x')], (SYMBOL, 'x')]),
      ('(define (f x) x)',
         [(SYMBOL, 'define'),
             [(SYMBOL, 'f'), (SYMBOL, 'x')],
             (SYMBOL, 'x')]),
      ('(if #t 1)',
         [(SYMBOL, 'if'), (BOOLEAN, True), (INTEGER, 1)]),
      ('(if #t 1 2)',
         [(SYMBOL, 'if'), (BOOLEAN, True), (INTEGER, 1), (INTEGER, 2)]),

      # nested
      ('((lambda (x) x) 7)',
         [[(SYMBOL, 'lambda'), [(SYMBOL, 'x')], (SYMBOL, 'x')],
          (INTEGER, 7)]),

      # quote shortcut becomes a list
      ("'x",
         [(SYMBOL, 'quote'), (SYMBOL, 'x')]),
      ("'42",
         [(SYMBOL, 'quote'), (INTEGER, 42)]),
      ("'(a b c)",
         [(SYMBOL, 'quote'),
             [(SYMBOL, 'a'), (SYMBOL, 'b'), (SYMBOL, 'c')]]),
      ("''x",
         [(SYMBOL, 'quote'),
             [(SYMBOL, 'quote'), (SYMBOL, 'x')]]),

      # dotted / improper lists
      ('(a . b)',             ([(SYMBOL, 'a')], (SYMBOL, 'b'))),
      ('(a b . c)',           ([(SYMBOL, 'a'), (SYMBOL, 'b')], (SYMBOL, 'c'))),

      # comments / whitespace
      ('  42 ; comment\n',    (INTEGER, 42)),
   ]

   print('-- happy path --')
   i = 0
   while i < len(happy):
      source   = happy[i][0]
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
      source            = errors[i][0]
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
   foo_src  = foo_atom[-1]
   if foo_src is not None and foo_src.line == 1 and foo_src.col == 2:
      print("[ OK ] atom 'foo' src (line 1 col 2)")
      n_pass = n_pass + 1
   else:
      print("[FAIL] atom 'foo' src: expected line 1 col 2, got %r" % foo_src)
      n_fail = n_fail + 1

   bar_atom = expr.cdr.car
   bar_src  = bar_atom[-1]
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
      print("[FAIL] cons filename: expected %r, got %r" % (REPL_FILENAME, rexpr.src))
      n_fail = n_fail + 1
   a_atom = rexpr.car
   if a_atom[-1] is not None and a_atom[-1].filename == REPL_FILENAME:
      print("[ OK ] atom filename propagated")
      n_pass = n_pass + 1
   else:
      print("[FAIL] atom filename: expected %r, got %r" % (REPL_FILENAME, a_atom[-1]))
      n_fail = n_fail + 1

   print()
   print("%d passed, %d failed" % (n_pass, n_fail))
