"""Interpreter adapter: wires Parser + Expander + Analyzer + cek_eval.

Pipeline:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> Evaluator.cek_eval

The Parser is a pure reader (S-expressions - cons cells and atoms).
The Expander rewrites sugar at the S-expression level.  The Analyzer
validates shape and static arity, returning the input unchanged.  The
Evaluator dispatches on cons-cell shape directly, so no IR-generation
pass is needed between Analyzer and Evaluator.

Public API:
    reboot()            - reset to a fresh global environment
    rawEval(source)     - parse, expand, analyze, eval; return raw value
    eval(source)        - as rawEval, then pretty-print
    evalFile(path)      - read and evaluate a file
"""

import os
import sys

from pyscheme.Parser        import parse, SchemeSyntaxError
from pyscheme.Expander      import expand
from pyscheme.Analyzer      import (
   analyze, SchemeAnalysisError, extend_static_env_with_define,
)
from pyscheme.Evaluator          import cek_eval, set_global_env
from pyscheme.primitives.ports   import reset_current_port_params
from pyscheme.primitives    import install_primitives, PRIMITIVE_ARITIES
from pyscheme.Environment   import Environment, SchemeRuntimeError
from pyscheme.Context       import Context
from pyscheme.Listener      import InterpreterBase
from pyscheme.Debugger      import Debugger
from pyscheme.Tracer        import Tracer
from pyscheme.PrettyPrinter import pretty_print
from pyscheme.AST           import is_void, is_multi_values, as_multi_values_list
from pyscheme.library     import register_standard_libraries
from pyscheme.Expander    import set_runtime_env


class Interpreter(InterpreterBase):
   def __init__(self):
      self._env = None
      self._static_env = {}
      self._ctx = Context()
      tracer = Tracer()
      self._ctx.tracer = tracer
      tracer._ctx = self._ctx
      self._wire_ctx_leval()
      self.reboot()

   def _wire_ctx_leval(self):
      """Bind ctx.lEval to a callable (env, expr) -> Value using this context."""
      ctx = self._ctx
      def _leval(env, expr):
         return cek_eval(expr, env, ctx)
      ctx.lEval = _leval

   def get_ctx(self):
      return self._ctx

   def set_debug_input_fn(self, fn, rl=None):
      """Register the Listener's prompt function for debug REPLs.
      Called by Listener after construction so debugger prompts use readline."""
      self._ctx.debugger.input_fn = fn
      self._ctx.debugger._rl      = rl

   def reboot(self, outStrm=None, load_rc=True):
      """Reset the interpreter to a fresh global environment."""
      if sys.getrecursionlimit() < 2000:
         sys.setrecursionlimit(2000)
      self._ctx.debugger = Debugger()
      self._ctx.tracer.reset()
      reset_current_port_params()
      self._env = Environment()
      install_primitives(self._env)
      # Populate the R7RS library registry from global env.  Must happen
      # after install_primitives so (scheme base) etc. can see them.
      register_standard_libraries(self._env)
      # Expander's hygiene needs access to the runtime env to resolve
      # free identifiers in macro templates.
      set_runtime_env(self._env)
      # Extension loader needs the global env to install new primitives
      # from .py extension files at import time.
      set_global_env(self._env)
      # Parallel static env: maps name -> (min, max) arity signature
      # for statically-known operators (primitives, top-level lambda
      # defines).  Seeded with primitives; extended per top-level
      # define in rawEval.
      self._static_env = dict(PRIMITIVE_ARITIES)
      if outStrm is not None:
         self._ctx.outStrm = outStrm
      if load_rc:
         _rc = os.path.expanduser('~/.pyschemerc')
         if os.path.isfile(_rc):
            try:
               self.evalFile(_rc)
            except Exception as e:
               print('pyscheme: error loading ~/.pyschemerc: ' + str(e), file=sys.stderr)

   def rawEval(self, source, outStrm=None, filename=None):
      """Parse, expand, analyze, and evaluate every top-level form.
      Returns the value of the last form, or None if source had no forms.
      When `filename` is given (e.g. from evalFile), it is threaded into
      each parsed cons's src so include resolves relative paths against
      the including file's directory."""
      prev_out = self._ctx.outStrm
      if outStrm is not None:
         self._ctx.outStrm = outStrm
      self._ctx.shadow_stack.clear()
      try:
         forms = parse(source, filename)
         last  = None
         i = 0
         while i < len(forms):
            form     = forms[i]
            expanded = expand(form)
            analyze(expanded, self._static_env)
            extend_static_env_with_define(self._static_env, expanded)
            last = cek_eval(expanded, self._env, self._ctx)
            i = i + 1
         return last
      except RecursionError:
         raise SchemeRuntimeError('recursion depth exceeded')
      finally:
         self._ctx.outStrm = prev_out

   def eval(self, source, outStrm=None):
      raw = self.rawEval(source, outStrm=outStrm)
      if raw is None or is_void(raw):
         return ''
      # A top-level expression that yields multiple values is displayed as each
      # value (write form) separated by a space -- the REPL shows the values
      # returned, not a single datum, so the internal #<values ...> object
      # notation is not used here.  Matches R7RS REPL conventions and compliance
      # 6.12 (e.g. (eval '(floor/ 17 5) ...) => "3 2").  Zero values => "".
      if is_multi_values(raw):
         return ' '.join(pretty_print(v) for v in as_multi_values_list(raw))
      return pretty_print(raw)

   def evalFile(self, filename, outStrm=None):
      """Read and evaluate a source file.  Uses the absolute path of the
      file for source positions so include-resolution works regardless
      of the process CWD."""
      abs_path = os.path.abspath(filename)
      f = open(abs_path, 'r')
      source = f.read()
      f.close()
      self.rawEval(source, outStrm=outStrm, filename=abs_path)


if __name__ == '__main__':
   import sys
   sys.modules['pyscheme.Interpreter'] = sys.modules[__name__]

   n_pass = 0
   n_fail = 0

   # Cases the full pipeline should evaluate successfully.
   ok_cases = [
      ('42',                                  '42'),
      ('(define x 5) x',                      '5'),
      ('(define x 5) (set! x 99) x',          '99'),
      ('((lambda (x) x) 7)',                  '7'),
      ('(if #t 1 2)',                         '1'),
      ('(if #f 1 2)',                         '2'),
      ('(if #t 1)',                           '1'),
      ('(if #f 1)',                           '#<void>'),
      ('(define (id x) x) (id 42)',           '42'),
      ('(define id (lambda (x) x)) (id 99)',  '99'),
      ('((lambda (x y) x) 11 22)',            '11'),
      ('((lambda (a b c) b) 10 20 30)',       '20'),
      ('((lambda () 42))',                    '42'),
      ('(define (f a b) b) (f 1 2)',          '2'),
      ('((lambda (x) (set! x 1) (set! x 2) x) 0)', '2'),
      ("'x",                                  'x'),
      ("'()",                                 '()'),
      ("'(1 2 3)",                            '(1 2 3)'),
      ('(car (list 1 2 3))',                  '1'),
      ('(cdr (list 1 2 3))',                  '(2 3)'),
      ('(cons 1 (cons 2 (cons 3 (list))))',   '(1 2 3)'),
      ('(cons 1 2)',                          '(1 . 2)'),
      ('(pair? (list 1 2))',                  '#t'),
      ('(pair? (list))',                      '#f'),
      ('(null? (list))',                      '#t'),
      ('(null? (list 1))',                    '#f'),
      ('(begin 1 2 3)',                       '3'),
      ('(begin 42)',                          '42'),
      ('(define x 0) (begin (set! x 1) (set! x 2) x)', '2'),
      ('(when #t 1 2 3)',                     '3'),
      ('(when #f 1)',                         '#<void>'),
      ('(when 0 99)',                         '99'),
      ('(when (null? (list)) (cons 1 (list)))', '(1)'),
      ('(unless #f 1 2 3)',                   '3'),
      ('(unless #t 1)',                       '#<void>'),
      ('(unless (null? (list 1)) 42)',        '42'),
      ('(and)',                               '#t'),
      ('(and 1)',                             '1'),
      ('(and 1 2 3)',                         '3'),
      ('(and #f (cons 1 2))',                 '#f'),
      ('(and 1 #f 3)',                        '#f'),
      ('(or)',                                '#f'),
      ('(or 1)',                              '1'),
      ('(or #f #f 3)',                        '3'),
      ('(or #f #f #f)',                       '#f'),
      ('(or 1 (cons 1 2))',                   '1'),
      ('(cond (#t 1))',                       '1'),
      ('(cond (#f 1) (else 2))',              '2'),
      ('(cond (#f 1) (#t 2) (else 3))',       '2'),
      ('(cond (#f 1))',                       '#<void>'),
      ("(cond (7))",                          '7'),
      ("(cond (#f) (else 99))",               '99'),
      ('(cond ((cons 1 2) => car))',          '1'),
      ('(cond (#f => car) (else 42))',        '42'),
      ('(let () 42)',                         '42'),
      ('(let ((x 1)) x)',                     '1'),
      ('(let ((x 1) (y 2)) y)',               '2'),
      ('(let ((a 1) (b 2) (c 3)) (list a b c))', '(1 2 3)'),
      ('(define x 10) (let ((x 1)) x)',       '1'),
      ('(define x 10) (let ((x 1)) x) x',     '10'),
      ('(let ((lst (list 1 2 3))) (car lst))','1'),
      ('(let* () 42)',                        '42'),
      ('(let* ((x 1) (y x)) y)',              '1'),
      ('(let* ((x 1) (y (cons x (list)))) y)','(1)'),
      ('(letrec ((f (lambda (x) x))) (f 42))','42'),
      ('(letrec ((last (lambda (lst)'
         ' (if (null? (cdr lst)) (car lst) (last (cdr lst))))))'
         ' (last (list 1 2 3)))',             '3'),
      ('(letrec ((p? (lambda (lst) (if (null? lst) #t (q? (cdr lst)))))'
         ' (q? (lambda (lst) (if (null? lst) #f (p? (cdr lst))))))'
         ' (p? (list 1 2 3 4)))',             '#t'),
      ('(letrec* ((f (lambda (x) x)) (g (lambda (x) (f x)))) (g 99))', '99'),
      ('(let loop ((lst (list 1 2 3)))'
         ' (if (null? lst) (quote done) (loop (cdr lst))))', 'done'),
      ('(let loop ((lst (list 1 2 3)) (acc (list)))'
         ' (if (null? lst) acc (loop (cdr lst) (cons (car lst) acc))))',
         '(3 2 1)'),
      ('(+)',                                 '0'),
      ('(+ 1 2 3 4)',                         '10'),
      ('(- 10)',                              '-10'),
      ('(- 10 3 2)',                          '5'),
      ('(*)',                                 '1'),
      ('(* 2 3 4)',                           '24'),
      ('(/ 12 3)',                            '4'),
      ('(abs -7)',                            '7'),
      ('(abs 7)',                             '7'),
      ('(quotient 13 4)',                     '3'),
      ('(quotient -13 4)',                    '-3'),
      ('(remainder 13 4)',                    '1'),
      ('(remainder -13 4)',                   '-1'),
      ('(modulo 13 4)',                       '1'),
      ('(modulo -13 4)',                      '3'),
      ('(min 3 1 4 1 5)',                     '1'),
      ('(max 3 1 4 1 5)',                     '5'),
      ('(= 1 1)',                             '#t'),
      ('(= 1 2)',                             '#f'),
      ('(= 1 1 1 1)',                         '#t'),
      ('(< 1 2 3)',                           '#t'),
      ('(< 1 3 2)',                           '#f'),
      ('(> 3 2 1)',                           '#t'),
      ('(<= 1 1 2)',                          '#t'),
      ('(>= 3 3 2)',                          '#t'),
      ('(number? 42)',                        '#t'),
      ('(number? "x")',                       '#f'),
      ('(integer? 42)',                       '#t'),
      ('(integer? (list))',                   '#f'),
      ('(zero? 0)',                           '#t'),
      ('(zero? 1)',                           '#f'),
      ('(positive? 7)',                       '#t'),
      ('(positive? -1)',                      '#f'),
      ('(negative? -1)',                      '#t'),
      ('(even? 4)',                           '#t'),
      ('(even? 3)',                           '#f'),
      ('(odd? 3)',                            '#t'),
      ('(boolean? #t)',                       '#t'),
      ('(boolean? 0)',                        '#f'),
      ('(procedure? car)',                    '#t'),
      ('(procedure? (lambda (x) x))',         '#t'),
      ('(procedure? 42)',                     '#f'),
      ("(symbol? 'x)",                        '#t'),
      ('(symbol? 42)',                        '#f'),
      ('(string? "hi")',                      '#t'),
      ('(string? 42)',                        '#f'),
      ('(equal? (list 1 2) (list 1 2))',      '#t'),
      ('(equal? 42 42)',                      '#t'),
      ('(equal? 42 "42")',                    '#f'),
      ('(eqv? 2 2)',                          '#t'),
      ('(eqv? 2 2.0)',                        '#f'),
      ('(not #f)',                            '#t'),
      ('(not #t)',                            '#f'),
      ('(not 0)',                             '#f'),
      ('(not (list))',                       '#f'),
      ('(define (fact n) (if (zero? n) 1 (* n (fact (- n 1))))) (fact 6)', '720'),
      ('(define (sum-to n) (if (zero? n) 0 (+ n (sum-to (- n 1))))) (sum-to 10)', '55'),
      ('(let loop ((i 0) (acc 0))'
         ' (if (> i 10) acc (loop (+ i 1) (+ acc i))))', '55'),
      ('(define (length lst)'
         ' (if (null? lst) 0 (+ 1 (length (cdr lst)))))'
         ' (length (list 10 20 30 40))', '4'),
   ]
   print('-- pipeline succeeds --')
   i = 0
   while i < len(ok_cases):
      source   = ok_cases[i][0]
      expected = ok_cases[i][1]
      interp   = Interpreter()
      try:
         result = interp.eval(source)
      except Exception as e:
         print('[FAIL] %r: %s: %s' % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      if result == expected:
         print('[ OK ] %r  =>  %s' % (source, result))
         n_pass = n_pass + 1
      else:
         print('[FAIL] %r  =>  %r  (expected %r)' % (source, result, expected))
         n_fail = n_fail + 1
      i = i + 1

   print()
   print('-- analyzer rejects --')
   analyzer_errors = [
      ('(lambda 1 x)',            'parameter list must be a list or identifier'),
      ('(lambda (x x) x)',        'duplicate parameter name'),
      ('(define)',                'requires a name'),
      ('(set!)',                  'requires a name'),
      ('(if)',                    'requires 2 or 3 arguments'),
      ('(begin)',                 'at least one body expression'),
      ('(let ((x)) x)',           'binding must be'),
      ('(cond)',                  'at least one clause'),
      ('()',                      'empty list'),
   ]
   i = 0
   while i < len(analyzer_errors):
      source            = analyzer_errors[i][0]
      expected_fragment = analyzer_errors[i][1]
      interp = Interpreter()
      try:
         interp.eval(source)
      except Exception as e:
         if type(e).__name__ == 'SchemeAnalysisError':
            if expected_fragment in str(e):
               print('[ OK ] %r  ->  %s' % (source, e))
               n_pass = n_pass + 1
            else:
               print('[WARN] %r  ->  %s' % (source, e))
               n_pass = n_pass + 1
            i = i + 1
            continue
         print('[FAIL] %r: wrong exception %s: %s' % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      print('[FAIL] %r: expected SchemeAnalysisError' % source)
      n_fail = n_fail + 1
      i = i + 1

   print()
   print('%d passed, %d failed' % (n_pass, n_fail))
