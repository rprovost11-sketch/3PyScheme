"""
Test suite reorganization script.
Reads the 37 original source test log files, classifies each entry into
the new 25-file structure, deduplicates, and writes new files.

Classification rules:
  1. test05-R7RS-derived.log:  section-header routing
  2. FORCE_SOURCE_DEFAULT files: all entries go to SOURCE_DEFAULT
  3. Files with locally-defined names: all entries go to SOURCE_DEFAULT
  4. Files with no locally-defined names: PRIM routing with SOURCE_DEFAULT fallback
"""
import sys, io, os, re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pyscheme.Listener import _parse_log

TEST_DIR = 'testing'

# ── Original 37 source files ──────────────────────────────────────────────────

OLD_SOURCES = [
   'test00-parser.log',
   'test01-literals.log',
   'test02-basics.log',
   'test03-variadic.log',
   'test04-analyzer.log',
   'test05-R7RS-derived.log',
   'test06-quote-lists.log',
   'test07-arithmetic.log',
   'test08-predicates.log',
   'test09-programs.log',
   'test10-help.log',
   'test11-meta.log',
   'test12-hygiene.log',
   'test13-syntax-rules.log',
   'test14-numbers.log',
   'test15-numbers2.log',
   'test16-numbers3.log',
   'test17-numbers4.log',
   'test18-numbers5.log',
   'test19-library.log',
   'test20-compliance.log',
   'test21-compliance2.log',
   'test22-compliance3.log',
   'test23-parser2.log',
   'test24-arithmetic2.log',
   'test25-math.log',
   'test26-predicates2.log',
   'test27-special-forms2.log',
   'test28-pairs-lists2.log',
   'test29-strings-chars2.log',
   'test30-vectors2.log',
   'test31-io-ports2.log',
   'test32-control2.log',
   'test33-macros2.log',
   'test34-modules2.log',
   'test35-records2.log',
   'test36-analyzer2.log',
]

# ── New file descriptors ──────────────────────────────────────────────────────

NEW_FILES = {
   'test00-parser.log':         ';;; Lexer/parser: special syntax, comments, datum labels, character/string/symbol escapes',
   'test01-literals.log':       ';;; Literal value representation: integers, floats, rationals, booleans, chars, strings, symbols, vectors, bytevectors',
   'test02-expander.log':       ';;; Expander: cond-expand, include, include-ci',
   'test03-analyzer.log':       ';;; Static analysis: malformed forms, arity errors',
   'test04-core-forms.log':     ';;; Core evaluator: define, lambda, if, begin, set!, quote, quasiquote',
   'test05-derived-forms.log':  ';;; Derived forms: cond, case, and/or, when/unless, let variants, do, define-values',
   'test06-advanced-forms.log': ';;; Advanced forms: case-lambda, delay/force, call/cc, values, parameterize, dynamic-wind, guard, raise',
   'test07-programs.log':       ';;; TCO, named let, practical recursive programs',
   'test08-macros.log':         ';;; syntax-rules, define-syntax, hygiene, let-syntax, letrec-syntax, ellipsis',
   'test09-modules.log':        ';;; define-library, import/export, import filters, cond-expand (module level)',
   'test10-records.log':        ';;; define-record-type',
   'test20-arithmetic.log':     ';;; arithmetic.py: basic ops, div/mod, floor/ceil/round, min/max, gcd/lcm, expt, sqrt, number<->string',
   'test21-arithmetic2.log':    ';;; arithmetic.py: numeric tower - exact/inexact, rationals, complex, special floats, transcendentals',
   'test22-bytevectors.log':    ';;; bytevectors.py: all bytevector operations',
   'test23-chars.log':          ';;; chars.py: character predicates, comparison, conversion',
   'test24-comparison.log':     ';;; comparison.py: =, <, >, <=, >= across all numeric types',
   'test25-control.log':        ';;; control.py: apply, map, for-each, filter, string-map, vector-map, higher-order',
   'test26-equivalence.log':    ';;; equivalence.py: eq?, eqv?, equal?, boolean=?, symbol=?',
   'test27-help.log':           ';;; help_sys.py: help, apropos',
   'test28-lists.log':          ';;; lists.py: all list primitives, cxr, assoc/member, list-set!, list-copy',
   'test29-logical.log':        ';;; logical.py: not',
   'test30-meta.log':           ';;; meta.py: eval, environments, error objects, process context, time',
   'test31-ports.log':          ';;; ports.py: all I/O - ports, string/bytevector ports, file I/O, read/write variants, write-shared',
   'test32-predicates.log':     ';;; predicates.py: all type predicates, numeric predicates',
   'test33-strings.log':        ';;; strings.py: all string operations, case conversion, string<->list/vector',
   'test34-vectors.log':        ';;; vectors.py: all vector operations, vector-map/for-each, vector-append',
}

# ── Source-file defaults ──────────────────────────────────────────────────────

GENERIC_DEFAULT = 'test04-core-forms.log'

SOURCE_DEFAULT = {
   'test00-parser.log':         'test00-parser.log',
   'test01-literals.log':       'test01-literals.log',
   'test02-basics.log':         GENERIC_DEFAULT,
   'test03-variadic.log':       GENERIC_DEFAULT,
   'test04-analyzer.log':       'test03-analyzer.log',
   'test05-R7RS-derived.log':   'test05-derived-forms.log',
   'test06-quote-lists.log':    GENERIC_DEFAULT,
   'test07-arithmetic.log':     'test20-arithmetic.log',
   'test08-predicates.log':     'test32-predicates.log',
   'test09-programs.log':       'test07-programs.log',
   'test10-help.log':           'test27-help.log',
   'test11-meta.log':           'test30-meta.log',
   'test12-hygiene.log':        'test08-macros.log',
   'test13-syntax-rules.log':   'test08-macros.log',
   'test14-numbers.log':        'test21-arithmetic2.log',
   'test15-numbers2.log':       'test21-arithmetic2.log',
   'test16-numbers3.log':       'test21-arithmetic2.log',
   'test17-numbers4.log':       'test21-arithmetic2.log',
   'test18-numbers5.log':       'test21-arithmetic2.log',
   'test19-library.log':        'test09-modules.log',
   'test20-compliance.log':     GENERIC_DEFAULT,
   'test21-compliance2.log':    GENERIC_DEFAULT,
   'test22-compliance3.log':    GENERIC_DEFAULT,
   'test23-parser2.log':        'test00-parser.log',
   'test24-arithmetic2.log':    'test20-arithmetic.log',
   'test25-math.log':           'test21-arithmetic2.log',
   'test26-predicates2.log':    'test32-predicates.log',
   'test27-special-forms2.log': GENERIC_DEFAULT,
   'test28-pairs-lists2.log':   'test28-lists.log',
   'test29-strings-chars2.log': 'test33-strings.log',
   'test30-vectors2.log':       'test34-vectors.log',
   'test31-io-ports2.log':      'test31-ports.log',
   'test32-control2.log':       'test25-control.log',
   'test33-macros2.log':        'test08-macros.log',
   'test34-modules2.log':       'test09-modules.log',
   'test35-records2.log':       'test10-records.log',
   'test36-analyzer2.log':      'test03-analyzer.log',
}

# Files where all entries always go to SOURCE_DEFAULT (state or intent override)
FORCE_SOURCE_DEFAULT = frozenset([
   'test00-parser.log',    # parser tests - keep char/string calls in parser file
   'test19-library.log',   # write-file -> load-file -> check: inter-entry state
   'test23-parser2.log',   # parser gap tests - keep with test00-parser.log
])

# Structural forms: always use SOURCE_DEFAULT (introduce top-level state)
STRUCTURAL_FORMS = frozenset([
   'define', 'define-values', 'define-record-type',
   'define-syntax', 'define-library', 'set!',
])

# ── PRIM routing dict ─────────────────────────────────────────────────────────
# Used only for files with no locally-defined names.

# Expander forms
_EXP = {k: 'test02-expander.log' for k in [
   'include', 'include-ci', 'cond-expand',
]}

# Advanced language forms
_ADV_FORMS = {k: 'test06-advanced-forms.log' for k in [
   'case-lambda',
   'delay', 'delay-force',
   'let-values', 'let*-values',
   'guard', 'parameterize',
   # meta.py advanced primitives
   'error', 'raise', 'raise-continuable', 'with-exception-handler',
   'error-object?', 'error-object-message', 'error-object-irritants',
   'file-error?', 'read-error?',
   'force', 'make-promise', 'promise?',
   'values', 'call-with-values',
   'make-parameter',
   'call-with-current-continuation', 'call/cc',
   'dynamic-wind',
]}

# Macro forms
_MAC = {k: 'test08-macros.log' for k in [
   'let-syntax', 'letrec-syntax', 'syntax-rules',
]}

# Module forms
_MOD = {k: 'test09-modules.log' for k in ['import', 'export']}

# arithmetic.py basic
_ARITH = {k: 'test20-arithmetic.log' for k in [
   '+', '-', '*', '/',
   'abs', 'quotient', 'remainder', 'modulo',
   'floor/', 'floor-quotient', 'floor-remainder',
   'truncate/', 'truncate-quotient', 'truncate-remainder',
   'min', 'max', 'gcd', 'lcm', 'expt', 'sqrt', 'exact-integer-sqrt',
   'number->string', 'string->number',
   'floor', 'ceiling', 'truncate', 'round',
]}

# arithmetic.py numeric tower
_TOWER = {k: 'test21-arithmetic2.log' for k in [
   'exp', 'log', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan',
   'sinh', 'cosh', 'tanh', 'asinh', 'acosh', 'atanh',
   'square', 'exact', 'inexact', 'exact->inexact', 'inexact->exact',
   'numerator', 'denominator', 'rationalize',
   'make-rectangular', 'make-polar', 'real-part', 'imag-part',
   'magnitude', 'angle',
   'exact?', 'inexact?', 'exact-integer?',
]}

# bytevectors.py
_BVEC = {k: 'test22-bytevectors.log' for k in [
   'bytevector', 'make-bytevector', 'bytevector?',
   'bytevector-length', 'bytevector-u8-ref', 'bytevector-u8-set!',
   'bytevector-copy', 'bytevector-copy!', 'bytevector-append',
   'bytevector->u8-list', 'u8-list->bytevector',
   'string->utf8', 'utf8->string',
]}

# chars.py
_CHARS = {k: 'test23-chars.log' for k in [
   'char?', 'char=?', 'char<?', 'char>?', 'char<=?', 'char>=?',
   'char-ci=?', 'char-ci<?', 'char-ci>?', 'char-ci<=?', 'char-ci>=?',
   'char-alphabetic?', 'char-numeric?', 'char-whitespace?',
   'char-upper-case?', 'char-lower-case?', 'digit-value',
   'char->integer', 'integer->char',
   'char-upcase', 'char-downcase', 'char-foldcase',
]}

# comparison.py
_CMP = {k: 'test24-comparison.log' for k in ['=', '<', '>', '<=', '>=']}

# control.py (higher-order ops)
_HOF = {k: 'test25-control.log' for k in [
   'apply', 'map', 'for-each', 'filter',
   'fold-left', 'fold-right', 'reduce',
]}

# equivalence.py
_EQV = {k: 'test26-equivalence.log' for k in [
   'eq?', 'eqv?', 'equal?', 'boolean=?', 'symbol=?',
]}

# help_sys.py
_HELP = {k: 'test27-help.log' for k in ['help', 'apropos']}

# lists.py
_LISTS = {k: 'test28-lists.log' for k in [
   'cons', 'car', 'cdr', 'set-car!', 'set-cdr!',
   'list', 'list?', 'pair?', 'null?',
   'length', 'append', 'reverse',
   'list-tail', 'list-ref', 'list-set!', 'list-copy', 'last-pair',
   'make-list', 'iota',
   'assoc', 'assv', 'assq', 'member', 'memv', 'memq',
   'caar', 'cadr', 'cdar', 'cddr',
   'caaar', 'caadr', 'cadar', 'caddr', 'cdaar', 'cdadr', 'cddar', 'cdddr',
   'caaaar', 'caaadr', 'caadar', 'caaddr',
   'cadaar', 'cadadr', 'caddar', 'cadddr',
   'cdaaar', 'cdaadr', 'cdadar', 'cdaddr',
   'cddaar', 'cddadr', 'cdddar', 'cddddr',
]}

# logical.py
_LOGI = {'not': 'test29-logical.log'}

# meta.py (environment/process/time)
_META = {k: 'test30-meta.log' for k in [
   'eval', 'interaction-environment', 'environment',
   'null-environment', 'scheme-report-environment',
   'load', 'command-line', 'exit',
   'get-environment-variable', 'get-environment-variables',
   'current-second', 'current-jiffy', 'jiffies-per-second',
]}

# ports.py
_PORTS = {k: 'test31-ports.log' for k in [
   'port?', 'input-port?', 'output-port?', 'textual-port?', 'binary-port?',
   'input-port-open?', 'output-port-open?',
   'eof-object?', 'eof-object',
   'open-input-file', 'open-output-file',
   'open-input-string', 'open-output-string', 'get-output-string',
   'open-input-bytevector', 'open-output-bytevector', 'get-output-bytevector',
   'open-binary-input-file', 'open-binary-output-file',
   'close-port', 'close-input-port', 'close-output-port',
   'read-char', 'peek-char', 'read', 'read-line', 'read-string',
   'char-ready?', 'read-u8', 'peek-u8', 'u8-ready?',
   'read-bytevector', 'read-bytevector!',
   'write-u8', 'write-bytevector',
   'write-char', 'write-string', 'newline',
   'display', 'write', 'write-shared', 'write-simple',
   'flush-output-port',
   'current-input-port', 'current-output-port', 'current-error-port',
   'call-with-port', 'call-with-input-file', 'call-with-output-file',
   'with-input-from-file', 'with-output-to-file',
   'file-exists?', 'delete-file', 'rename-file',
]}

# predicates.py
_PRED = {k: 'test32-predicates.log' for k in [
   'number?', 'complex?', 'real?', 'rational?', 'integer?',
   'zero?', 'positive?', 'negative?', 'even?', 'odd?',
   'boolean?', 'procedure?', 'symbol?', 'string?',
   'promise?', 'parameter?',
   'finite?', 'infinite?', 'nan?',
]}

# strings.py
_STRS = {k: 'test33-strings.log' for k in [
   'string-length', 'string-ref',
   'string=?', 'string<?', 'string<=?', 'string>?', 'string>=?',
   'substring', 'string-append', 'string->list', 'list->string',
   'string-copy', 'make-string', 'string',
   'string->symbol', 'symbol->string',
   'string-upcase', 'string-downcase', 'string-foldcase',
   'string-ci=?', 'string-ci<?', 'string-ci<=?', 'string-ci>?', 'string-ci>=?',
   'string-map', 'string-for-each',
   'string-set!', 'string-fill!', 'string-copy!',
]}

# vectors.py
_VECS = {k: 'test34-vectors.log' for k in [
   'vector?', 'make-vector', 'vector',
   'vector-length', 'vector-ref', 'vector-set!',
   'vector->list', 'list->vector',
   'vector-fill!', 'vector-copy', 'vector-copy!', 'vector-append',
   'vector->string', 'string->vector',
   'vector-map', 'vector-for-each',
]}

PRIM = {}
PRIM.update(_EXP)
PRIM.update(_ADV_FORMS)
PRIM.update(_MAC)
PRIM.update(_MOD)
PRIM.update(_ARITH)
PRIM.update(_TOWER)
PRIM.update(_BVEC)
PRIM.update(_CHARS)
PRIM.update(_CMP)
PRIM.update(_HOF)
PRIM.update(_EQV)
PRIM.update(_HELP)
PRIM.update(_LISTS)
PRIM.update(_LOGI)
PRIM.update(_META)
PRIM.update(_PORTS)
PRIM.update(_PRED)
PRIM.update(_STRS)
PRIM.update(_VECS)

# ── Section map for test05-R7RS-derived.log ───────────────────────────────────

SECTION_MAP = {
   'begin (R7RS 4.2.3 sequencing)':                                               'test05-derived-forms.log',
   'when / unless (R7RS 4.2.1)':                                                  'test05-derived-forms.log',
   'and / or (R7RS 4.2.1)':                                                       'test05-derived-forms.log',
   'cond (R7RS 4.2.1) - all three clause forms':                                  'test05-derived-forms.log',
   'case (R7RS 4.2.1) - eqv?-based literal dispatch':                             'test05-derived-forms.log',
   'let (R7RS 4.2.2)':                                                            'test05-derived-forms.log',
   'let* (R7RS 4.2.2)':                                                           'test05-derived-forms.log',
   'letrec / letrec* (R7RS 4.2.2)':                                              'test05-derived-forms.log',
   'named let (R7RS 4.2.4)':                                                      'test07-programs.log',
   'do (R7RS 4.2.4) - iteration':                                                 'test05-derived-forms.log',
   'cond-expand (R7RS 5.6.2) - compile-time feature dispatch':                    'test02-expander.log',
   'case-lambda (R7RS 4.2.9) - arity-dispatched lambda':                          'test06-advanced-forms.log',
   'quasiquote (R7RS 4.2.8) - template construction':                             'test04-core-forms.log',
   'delay / delay-force / force / make-promise (R7RS 4.2.5, 6.10)':              'test06-advanced-forms.log',
   'values / call-with-values / let-values / let*-values / define-values (R7RS 4.2.2, 5.3.3, 6.10)': 'test06-advanced-forms.log',
   'let-values: parallel semantics':                                               'test06-advanced-forms.log',
   'let*-values: sequential semantics':                                            'test06-advanced-forms.log',
   'define-values':                                                                'test05-derived-forms.log',
   'multi-values as single-value error':                                           'test06-advanced-forms.log',
   'multi-values legitimately allowed':                                            'test06-advanced-forms.log',
   'shape errors':                                                                 'test03-analyzer.log',
   'internal definitions hoist into letrec* (R7RS 5.3.2)':                       'test04-core-forms.log',
   'define-record-type (R7RS 5.5)':                                               'test10-records.log',
   'synthesized-name hygiene':                                                     'test08-macros.log',
   'make-parameter / parameterize (R7RS 4.2.6, 6.10)':                           'test06-advanced-forms.log',
   'raise / with-exception-handler / guard / error objects (R7RS 4.2.7, 6.11)':  'test06-advanced-forms.log',
   'call/cc / call-with-current-continuation (R7RS 6.10)':                        'test06-advanced-forms.log',
   'dynamic-wind (R7RS 6.10)':                                                    'test06-advanced-forms.log',
   'define-library / import / export (R7RS 5.6)':                                 'test09-modules.log',
   'define-syntax / syntax-rules (R7RS 4.3)':                                     'test08-macros.log',
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lead(expr):
   e = expr.strip()
   if not e.startswith('('):
      return None
   inner = e[1:].lstrip()
   m = re.match(r'([^\s()]+)', inner)
   return m.group(1) if m else None


def section_key(line):
   m = re.match(r';;;\s*-{4,}\s+(.+?)\s+-{4,}', line)
   return m.group(1).strip() if m else None


def normalize(entry):
   expr, out, ret, err = entry
   return (expr.strip(), out.strip(), ret.strip(), err.strip())


def extract_defined_names(expr):
   """Extract names introduced at top level (define, define-values, define-syntax)."""
   e = expr.strip()
   names = set()
   m = re.match(r'\(define\s+(?:\((\S+)[\s)]|(\S+)[\s)])', e)
   if m:
      name = m.group(1) or m.group(2)
      if name:
         names.add(name)
   m = re.match(r'\(define-values\s+\(([^)]+)\)', e)
   if m:
      for n in m.group(1).split():
         n = n.strip('() ')
         if n:
            names.add(n)
   m = re.match(r'\(define-syntax\s+(\S+)', e)
   if m:
      names.add(m.group(1))
   return names


def classify(entry, srcfile, section_target, locally_defined):
   expr, out, ret, err = entry

   # test05: section-based routing always
   if srcfile == 'test05-R7RS-derived.log' and section_target:
      return section_target

   default = SOURCE_DEFAULT.get(srcfile, GENERIC_DEFAULT)

   # Force-keep files: everything goes to their source default
   if srcfile in FORCE_SOURCE_DEFAULT:
      return default

   # Files with locally-defined names: keep all entries together
   if locally_defined:
      return default

   # Pure-call files (no local state): route leaf calls by PRIM
   lead = get_lead(expr)
   if lead is None or lead in STRUCTURAL_FORMS:
      return default
   if lead in PRIM:
      return PRIM[lead]
   return default


def fmt_entry(entry):
   expr, out, ret, err = entry
   lines = []
   elines = expr.splitlines(keepends=True)
   if elines:
      lines.append('>>> ' + elines[0])
      for el in elines[1:]:
         lines.append('... ' + el)
   if out:
      lines.append(out if out.endswith('\n') else out + '\n')
   if err:
      for el in err.splitlines(keepends=True):
         lines.append('%%% ' + el)
   else:
      if ret:
         lines.append('==> ' + ret + '\n')
      else:
         lines.append('==>\n')
   return ''.join(lines)


# ── Load and classify ─────────────────────────────────────────────────────────

buckets   = {fname: [] for fname in NEW_FILES}
seen_norm = {fname: set() for fname in NEW_FILES}

n_loaded = 0
n_dup    = 0

# Process test05-R7RS-derived.log last so its section-routed entries
# (which define global x/y/tmp) appear after hygiene-sensitive tests.
_SOURCES_ORDERED = [s for s in OLD_SOURCES if s != 'test05-R7RS-derived.log']
_SOURCES_ORDERED.append('test05-R7RS-derived.log')

for srcfile in _SOURCES_ORDERED:
   src_path = os.path.join(TEST_DIR, srcfile)
   if not os.path.isfile(src_path):
      print('WARNING: source not found: %s' % srcfile)
      continue
   with open(src_path, 'r', encoding='utf-8') as fh:
      text = fh.read()

   entries = _parse_log(text)

   # Build locally_defined for this source file
   locally_defined = set()
   for entry in entries:
      locally_defined.update(extract_defined_names(entry[0]))

   # test05: track section per entry by scanning raw lines
   if srcfile == 'test05-R7RS-derived.log':
      lines = text.splitlines(keepends=True)
      entry_sections = []
      cur_sec = None
      ei = 0
      for line in lines:
         sk = section_key(line)
         if sk is not None:
            cur_sec = SECTION_MAP.get(sk, None)
         if line.startswith('>>> ') and ei < len(entries):
            entry_sections.append(cur_sec)
            ei = ei + 1
      while len(entry_sections) < len(entries):
         entry_sections.append(cur_sec)
      src_entries = list(zip(entries, entry_sections))
   else:
      src_entries = [(e, None) for e in entries]

   for entry, sec_tgt in src_entries:
      n_loaded = n_loaded + 1
      target = classify(entry, srcfile, sec_tgt, locally_defined)
      if target not in buckets:
         print('WARNING: unknown target %s from %s' % (target, srcfile))
         target = GENERIC_DEFAULT
      norm = normalize(entry)
      if norm in seen_norm[target]:
         n_dup = n_dup + 1
         continue
      seen_norm[target].add(norm)
      buckets[target].append(entry)

print('Loaded %d entries, %d duplicates removed.' % (n_loaded, n_dup))

# ── Write new files ───────────────────────────────────────────────────────────

for fname, description in NEW_FILES.items():
   out_path = os.path.join(TEST_DIR, fname)
   entries  = buckets[fname]
   with open(out_path, 'w', encoding='utf-8') as fh:
      bar  = '=' * 60
      stem = fname.replace('.log', '')
      fh.write(';;; %s\n' % bar)
      fh.write(';;; %s\n' % stem)
      fh.write(';;; %s\n' % description[4:])
      fh.write(';;; %s\n' % bar)
      fh.write('\n')
      for e in entries:
         fh.write(fmt_entry(e))
         fh.write('\n')
   print('Wrote %s  (%d entries)' % (fname, len(entries)))

print('Done.')
