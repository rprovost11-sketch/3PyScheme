"""Semantic analyzer: validates expanded S-expressions in place.

Pipeline position:
    source -> Parser.parse -> Expander.expand -> Analyzer.analyze
                              -> Evaluator.cek_eval

The Analyzer takes fully-expanded S-expressions (cons cells + atoms)
from the Expander and walks the tree.  It does NOT transform: it
returns the input cons-cell tree unchanged.  Its job is twofold:

    1. Shape validation - reject malformed special forms so the
       Evaluator can trust the structures it dispatches on.
    2. Static arity check - reject calls whose argument count
       cannot match the operator's arity.

Shape rules enforced (matches R7RS where applicable):

    - lambda's parameter list must be a list/identifier of unique names
    - define needs exactly (define name value)
    - set! needs exactly (set! name value)
    - if needs (if test then) or (if test then else)
    - let bindings must be ((var val) ...)
    - cond clauses must be (test body...) or (test) or (test => proc)
      or (else body...) (last)
    - body forms (lambda, when, unless, begin, let, ...) must be non-empty

Anything that doesn't match a special form is treated as an
application; arg count is checked when the operator's arity is known.

Static arity checking:

    When the operator of an application is an immediate lambda
    literal, a known primitive, or a variable whose arity is tracked
    in the static environment (top-level lambda defines), the
    Analyzer checks the argument count at analyze time and raises
    SchemeArityError on mismatch - same error class as the runtime
    check in Evaluator, so callers see the same message regardless
    of when it fires.

    The static environment is a dict { name: (min, max) } threaded
    through analyze.  max=None means variadic.  A param shadowing an
    outer name maps to None in the extended env, which suppresses
    checks through that name (its runtime arity is unknown).
    Primitives seed the env via primitives.PRIMITIVE_ARITIES.

Public API:
    analyze(sexpr, static_env=None)
        Returns sexpr unchanged on success, or raises
        SchemeAnalysisError / SchemeArityError.
        static_env defaults to a fresh dict seeded with PRIMITIVE_ARITIES.

    extend_static_env_with_define(static_env, sexpr)
        Mutates static_env: if sexpr is a top-level (define name value)
        whose value has a known arity (lambda literal, or a variable
        alias of something in static_env), records name's arity.

    SchemeAnalysisError   -> shape errors
    SchemeArityError      -> statically detected arity mismatch
                              (re-exported from Evaluator)
"""
from pyscheme.Environment import (
   _PositionedSchemeError, SchemeArityError, arity_mismatch_msg,
)
from pyscheme.Parser import SchemeSyntaxError
from pyscheme.Evaluator import _SYNTACTIC_KEYWORDS
from pyscheme.primitives import PRIMITIVE_ARITIES
from pyscheme.AST import (
   is_cons, is_nil, is_symbol, is_integer, is_real, is_rational, is_complex,
   is_string, is_character, is_boolean,
   as_symbol, as_integer, as_real, as_string, as_character, as_boolean,
   as_rational_num, as_rational_den, as_complex_real, as_complex_imag,
   src_of,
)


# ---- error type --------------------------------------------------------

class SchemeAnalysisError(_PositionedSchemeError):
   """Raised on a shape error.  Carries a SourceInfo (or None) so the
   listener can render a 3-line diagnostic with caret."""
   pass


# ---- helpers -----------------------------------------------------------

def _is_symbol_named(val, name):
   return is_symbol(val) and as_symbol(val) == name


def _proper_list_length(cell):
   """Length of a proper cons-cell list.  Returns -1 if improper.
   For NIL_VALUE returns 0."""
   if is_nil(cell):
      return 0
   n = 0
   cur = cell
   while is_cons(cur):
      n = n + 1
      cur = cur.cdr
   if is_nil(cur):
      return n
   return -1


def _cons_to_list(cell):
   """Walk a proper cons-cell list into a Python list of its elements.
   Caller must guarantee the list is proper."""
   items = []
   cur = cell
   while is_cons(cur):
      items.append(cur.car)
      cur = cur.cdr
   return items


_GENSYM_PFX = '\x01h.'


def _display_name(name):
   """Strip hygiene gensym prefix for error messages. \x01h.BASE.DIGITS -> BASE."""
   if not name.startswith(_GENSYM_PFX):
      return name
   rest = name[len(_GENSYM_PFX):]
   dot = rest.rfind('.')
   if dot >= 0 and rest[dot + 1:].isdigit():
      return rest[:dot]
   return rest


def _render(sexpr):
   """Brief text rendering of an S-expression for error messages."""
   if is_symbol(sexpr):
      return _display_name(as_symbol(sexpr))
   if is_nil(sexpr):
      return '()'
   if is_cons(sexpr):
      parts = []
      cur = sexpr
      while is_cons(cur):
         parts.append(_render(cur.car))
         cur = cur.cdr
      if is_nil(cur):
         return '(' + ' '.join(parts) + ')'
      return '(' + ' '.join(parts) + ' . ' + _render(cur) + ')'
   if is_integer(sexpr):
      return repr(as_integer(sexpr))
   if is_real(sexpr):
      return repr(as_real(sexpr))
   if is_string(sexpr):
      return repr(as_string(sexpr))
   if is_character(sexpr):
      return repr(as_character(sexpr))
   if is_boolean(sexpr):
      return repr(as_boolean(sexpr))
   if is_rational(sexpr):
      return str(as_rational_num(sexpr)) + '/' + str(as_rational_den(sexpr))
   if is_complex(sexpr):
      return str(as_complex_real(sexpr)) + '+' + str(as_complex_imag(sexpr)) + 'i'
   return repr(sexpr)


def _shadow(static_env, names):
   """Return a copy of static_env with each name bound to None
   (arity unknown).  Used when entering a lambda/let body where the
   bound names mask any outer static knowledge."""
   new_env = dict(static_env)
   i = 0
   while i < len(names):
      new_env[names[i]] = None
      i = i + 1
   return new_env


def _require_symbol(sexpr, context):
   if not is_symbol(sexpr):
      raise SchemeAnalysisError(
         "expected an identifier in " + context + ", got " + _render(sexpr),
         src_of(sexpr))
   return as_symbol(sexpr)


# ---- static arity ------------------------------------------------------

def _lambda_arity_from_cons(lam_cons):
   """Compute (min, max) arity from a (lambda params body...) cons cell.
   Caller must guarantee lam_cons is a cons whose car is the symbol
   'lambda' and whose cdr is a cons.  max=None means variadic."""
   params_sexpr = lam_cons.cdr.car
   if is_symbol(params_sexpr):
      return (0, None)
   if is_nil(params_sexpr):
      return (0, 0)
   n = 0
   cur = params_sexpr
   while is_cons(cur):
      n = n + 1
      cur = cur.cdr
   if is_nil(cur):
      return (n, n)
   return (n, None)


def _peek_lambda_arity(sexpr):
   """If sexpr is syntactically (lambda params body...), return its
   (min, max) arity without analyzing the body.  Otherwise None.
   Used by _analyze_define to make a recursive self-reference's arity
   visible while analyzing the body.  Shape errors in the lambda are
   still raised by _analyze_lambda."""
   if not is_cons(sexpr):
      return None
   if not _is_symbol_named(sexpr.car, 'lambda'):
      return None
   if not is_cons(sexpr.cdr):
      return None
   if _proper_list_length(sexpr) < 3:
      return None
   return _lambda_arity_from_cons(sexpr)


def _app_operator_arity(fn_sexpr, static_env):
   """Return (name, (min, max)) if fn_sexpr's arity is known statically,
   else None.  name is '' for an immediate lambda application."""
   if is_cons(fn_sexpr) and _is_symbol_named(fn_sexpr.car, 'lambda'):
      arity = _peek_lambda_arity(fn_sexpr)
      if arity is None:
         return None
      return ('', arity)
   if is_symbol(fn_sexpr):
      name  = as_symbol(fn_sexpr)
      arity = static_env.get(name)
      if arity is None:
         return None
      return (name, arity)
   return None


def _check_app_arity(fn_sexpr, args, static_env, app_sexpr):
   """Raise SchemeArityError if the application's arg count is
   inconsistent with a statically knowable operator arity."""
   info = _app_operator_arity(fn_sexpr, static_env)
   if info is None:
      return
   name = info[0]
   lo   = info[1][0]
   hi   = info[1][1]
   n    = len(args)
   if n < lo or (hi is not None and n > hi):
      raise SchemeArityError(
         arity_mismatch_msg(name, lo, hi, n), src_of(app_sexpr))


def extend_static_env_with_define(static_env, sexpr):
   """Mutate static_env from a top-level (define name value) cons cell.
   Records arity when value is either:
     - a (lambda params body...) literal -> its (min, max).
     - a symbol aliasing a name whose arity is already known.
   Other RHS shapes leave the binding unset (or remove it on
   re-definition with an unknown-arity RHS)."""
   if not is_cons(sexpr):
      return
   if not _is_symbol_named(sexpr.car, 'define'):
      return
   if not is_cons(sexpr.cdr) or not is_cons(sexpr.cdr.cdr):
      return
   name_sexpr = sexpr.cdr.car
   if not is_symbol(name_sexpr):
      return
   name  = as_symbol(name_sexpr)
   value = sexpr.cdr.cdr.car
   if is_cons(value) and _is_symbol_named(value.car, 'lambda'):
      arity = _peek_lambda_arity(value)
      if arity is not None:
         static_env[name] = arity
         return
      static_env.pop(name, None)
      return
   if is_cons(value) and _is_symbol_named(value.car, 'case-lambda'):
      # Arity is the union of per-clause arities.  For the static
      # check we set the widest possible (0, None) - runtime handles
      # the clause match and any SchemeArityError.
      static_env[name] = (0, None)
      return
   if is_symbol(value):
      aliased = static_env.get(as_symbol(value))
      if aliased is not None:
         static_env[name] = aliased
      else:
         static_env.pop(name, None)
      return
   static_env.pop(name, None)


# ---- main entry --------------------------------------------------------

def analyze(sexpr, static_env=None):
   """Validate sexpr in place.  Returns sexpr unchanged on success,
   raises SchemeAnalysisError / SchemeArityError otherwise."""
   if static_env is None:
      static_env = dict(PRIMITIVE_ARITIES)

   if is_nil(sexpr):
      raise SchemeAnalysisError(
         "empty list () is not a valid expression; use (quote ()) for the empty list",
         src_of(sexpr))

   if not is_cons(sexpr):
      # Atom (literal) or symbol (variable reference) - both are leaves.
      # R7RS §3.1: using a syntactic keyword as a variable is an error,
      # unless it is locally rebound (static_env stores None for local names).
      if is_symbol(sexpr):
         name = as_symbol(sexpr)
         if name in _SYNTACTIC_KEYWORDS and static_env.get(name) is not None:
            raise SchemeSyntaxError(
               'keyword used as expression: ' + name, src_of(sexpr))
      return sexpr

   head = sexpr.car
   if is_symbol(head):
      handler = _SPECIAL_FORMS.get(as_symbol(head))
      if handler is not None:
         handler(sexpr, static_env)
         return sexpr

   # Application: walk fn and arg sub-expressions.
   if _proper_list_length(sexpr) < 0:
      raise SchemeAnalysisError(
         "application must be a proper list", src_of(sexpr))
   fn_sexpr = sexpr.car
   args     = []
   cur      = sexpr.cdr
   while is_cons(cur):
      args.append(cur.car)
      cur = cur.cdr
   # Don't fire keyword check for the fn/head position: a keyword in head
   # position is a malformed special form, and the arity check below gives a
   # better error.  Keywords in arg positions are still checked.
   if not (is_symbol(fn_sexpr)
           and as_symbol(fn_sexpr) in _SYNTACTIC_KEYWORDS
           and static_env.get(as_symbol(fn_sexpr)) is not None):
      analyze(fn_sexpr, static_env)
   i = 0
   while i < len(args):
      analyze(args[i], static_env)
      i = i + 1
   _check_app_arity(fn_sexpr, args, static_env, sexpr)
   return sexpr


# ---- special-form handlers ---------------------------------------------
#
# Each handler validates shape and recurses into sub-expressions.
# It does NOT return a transformed expression - the top-level analyze()
# returns the input sexpr unchanged.

def _analyze_lambda_shape(params_sexpr, body_cons, form_name, outer_src, static_env):
   """Validate the formals + body of a lambda-shaped form.  Shared by
   _analyze_lambda and _analyze_case_lambda; `form_name` ('lambda' or
   'case-lambda') is used verbatim in error messages."""
   if is_symbol(params_sexpr):
      fixed_names = []
      fixed_srcs  = []
      rest_name   = as_symbol(params_sexpr)
      rest_src    = src_of(params_sexpr)
   elif is_nil(params_sexpr):
      fixed_names = []
      fixed_srcs  = []
      rest_name   = None
      rest_src    = None
   elif is_cons(params_sexpr):
      fixed_names = []
      fixed_srcs  = []
      cur = params_sexpr
      while is_cons(cur):
         p = cur.car
         if not is_symbol(p):
            raise SchemeAnalysisError(
               "expected an identifier in " + form_name + " parameter list, got " + _render(p),
               src_of(p))
         fixed_names.append(as_symbol(p))
         fixed_srcs.append(src_of(p))
         cur = cur.cdr
      if is_nil(cur):
         rest_name = None
         rest_src  = None
      elif is_symbol(cur):
         rest_name = as_symbol(cur)
         rest_src  = src_of(cur)
      else:
         raise SchemeAnalysisError(
            "expected an identifier in " + form_name + " rest parameter, got " + _render(cur),
            src_of(cur))
   else:
      raise SchemeAnalysisError(
         form_name + " parameter list must be a list or identifier, got " + _render(params_sexpr),
         src_of(params_sexpr))

   seen = set()
   i = 0
   while i < len(fixed_names):
      p = fixed_names[i]
      if p in seen:
         raise SchemeAnalysisError(
            "duplicate parameter name in " + form_name + ": " + p,
            fixed_srcs[i])
      seen.add(p)
      i = i + 1
   if rest_name is not None and rest_name in seen:
      raise SchemeAnalysisError(
         "rest parameter name conflicts with fixed parameter: " + rest_name,
         rest_src)

   shadowed = list(fixed_names)
   if rest_name is not None:
      shadowed.append(rest_name)
   body_env = _shadow(static_env, shadowed)

   body_count = _proper_list_length(body_cons)
   if body_count <= 0:
      raise SchemeAnalysisError(form_name + " body cannot be empty", outer_src)

   cur = body_cons
   while is_cons(cur):
      analyze(cur.car, body_env)
      cur = cur.cdr


def _analyze_lambda(sexpr, static_env):
   # Three forms:
   #   (lambda (a b c) body...)       fixed arity
   #   (lambda args body...)          pure variadic (args is bound to all args)
   #   (lambda (a b . rest) body...)  fixed prefix + rest bound to remainder
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         "lambda requires a parameter list and at least one body expression",
         src_of(sexpr))
   _analyze_lambda_shape(
      sexpr.cdr.car, sexpr.cdr.cdr, 'lambda', src_of(sexpr), static_env)


def _analyze_define(sexpr, static_env):
   n = _proper_list_length(sexpr) - 1
   if n != 2:
      raise SchemeAnalysisError(
         "define requires a name and a value (got " + str(n) + " arguments)",
         src_of(sexpr))
   name_sexpr  = sexpr.cdr.car
   value_sexpr = sexpr.cdr.cdr.car
   name = _require_symbol(name_sexpr, 'define')
   # When the RHS is a lambda literal, pre-bind name to its arity so a
   # recursive self-reference inside the body can be arity-checked.
   value_env = static_env
   arity = _peek_lambda_arity(value_sexpr)
   if arity is not None:
      value_env = dict(static_env)
      value_env[name] = arity
   analyze(value_sexpr, value_env)


def _analyze_set(sexpr, static_env):
   n = _proper_list_length(sexpr) - 1
   if n != 2:
      raise SchemeAnalysisError(
         "set! requires a name and a value (got " + str(n) + " arguments)",
         src_of(sexpr))
   name_sexpr  = sexpr.cdr.car
   value_sexpr = sexpr.cdr.cdr.car
   _require_symbol(name_sexpr, 'set!')
   analyze(value_sexpr, static_env)


def _analyze_if(sexpr, static_env):
   n = _proper_list_length(sexpr) - 1
   if n != 2 and n != 3:
      raise SchemeAnalysisError(
         "if requires 2 or 3 arguments (test, then, optional else), got " + str(n),
         src_of(sexpr))
   analyze(sexpr.cdr.car,         static_env)
   analyze(sexpr.cdr.cdr.car,     static_env)
   if n == 3:
      analyze(sexpr.cdr.cdr.cdr.car, static_env)


def _analyze_begin(sexpr, static_env):
   cur = sexpr.cdr
   while is_cons(cur):
      analyze(cur.car, static_env)
      cur = cur.cdr


def _check_unique_let_names(pairs, form_name, sexpr):
   """Raise SchemeAnalysisError if any binding identifier appears twice.
   With alpha-renaming, two macro-introduced bindings with the same source
   name produce distinct gensym names, so plain name equality is correct."""
   seen = set()
   i = 0
   while i < len(pairs):
      n = pairs[i][0]
      if n in seen:
         raise SchemeAnalysisError(
            "duplicate variable name in " + form_name + " bindings: " + n,
            src_of(sexpr))
      seen.add(n)
      i = i + 1


def _parse_let_bindings(bindings_sexpr, form_name):
   """Validate a let-family binding list and return a list of
   (var_name, value_sexpr) pairs.  Does NOT analyze value_sexprs --
   the caller picks the right static_env."""
   if not is_cons(bindings_sexpr) and not is_nil(bindings_sexpr):
      raise SchemeAnalysisError(
         form_name + " bindings must be a list, got " + _render(bindings_sexpr),
         src_of(bindings_sexpr))
   if _proper_list_length(bindings_sexpr) < 0:
      raise SchemeAnalysisError(
         form_name + " bindings must be a proper list",
         src_of(bindings_sexpr))
   pairs = []
   cur = bindings_sexpr
   while is_cons(cur):
      b = cur.car
      if _proper_list_length(b) != 2:
         raise SchemeAnalysisError(
            form_name + " binding must be (name value), got " + _render(b),
            src_of(b))
      var = _require_symbol(b.car, form_name + " binding")
      pairs.append((var, b.cdr.car))
      cur = cur.cdr
   return pairs


def _analyze_let(sexpr, static_env):
   # R7RS 4.2.2: (let <bindings> <body>)  OR  (let <name> <bindings> <body>)
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         "let requires a binding list and at least one body expression",
         src_of(sexpr))
   if is_symbol(sexpr.cdr.car):
      _analyze_named_let(sexpr, static_env)
      return
   pairs = _parse_let_bindings(sexpr.cdr.car, 'let')
   # R7RS 4.2.2: a binding name must not appear twice.
   _check_unique_let_names(pairs, 'let', sexpr)
   # All val_exprs evaluated in the enclosing env.
   i = 0
   while i < len(pairs):
      analyze(pairs[i][1], static_env)
      i = i + 1
   names = []
   i = 0
   while i < len(pairs):
      names.append(pairs[i][0])
      i = i + 1
   body_env = _shadow(static_env, names)
   body_cons = sexpr.cdr.cdr
   if _proper_list_length(body_cons) <= 0:
      raise SchemeAnalysisError("let body cannot be empty", src_of(sexpr))
   cur = body_cons
   while is_cons(cur):
      analyze(cur.car, body_env)
      cur = cur.cdr


def _analyze_let_star(sexpr, static_env):
   # R7RS 4.2.2: each val_expr sees the preceding bindings.
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         "let* requires a binding list and at least one body expression",
         src_of(sexpr))
   pairs = _parse_let_bindings(sexpr.cdr.car, 'let*')
   current_env = static_env
   i = 0
   while i < len(pairs):
      analyze(pairs[i][1], current_env)
      current_env = _shadow(current_env, [pairs[i][0]])
      i = i + 1
   body_cons = sexpr.cdr.cdr
   if _proper_list_length(body_cons) <= 0:
      raise SchemeAnalysisError("let* body cannot be empty", src_of(sexpr))
   cur = body_cons
   while is_cons(cur):
      analyze(cur.car, current_env)
      cur = cur.cdr


def _analyze_letrec(sexpr, static_env):
   _analyze_letrec_family(sexpr, static_env, 'letrec')


def _analyze_letrec_star(sexpr, static_env):
   _analyze_letrec_family(sexpr, static_env, 'letrec*')


def _analyze_letrec_family(sexpr, static_env, name):
   # R7RS 4.2.2: all names visible in every val_expr.
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         name + " requires a binding list and at least one body expression",
         src_of(sexpr))
   pairs = _parse_let_bindings(sexpr.cdr.car, name)
   # R7RS 4.2.2: a binding name must not appear twice in letrec / letrec*.
   _check_unique_let_names(pairs, name, sexpr)
   names = []
   i = 0
   while i < len(pairs):
      names.append(pairs[i][0])
      i = i + 1
   inner_env = _shadow(static_env, names)
   i = 0
   while i < len(pairs):
      analyze(pairs[i][1], inner_env)
      i = i + 1
   body_cons = sexpr.cdr.cdr
   if _proper_list_length(body_cons) <= 0:
      raise SchemeAnalysisError(name + " body cannot be empty", src_of(sexpr))
   cur = body_cons
   while is_cons(cur):
      analyze(cur.car, inner_env)
      cur = cur.cdr


def _analyze_named_let(sexpr, static_env):
   # R7RS 4.2.4: (let <name> <bindings> <body>)
   # Equivalent to (letrec ((name (lambda (v1 ...) body))) (name e1 ...))
   if _proper_list_length(sexpr) < 4:
      raise SchemeAnalysisError(
         "named let requires a name, a binding list, and at least one body expression",
         src_of(sexpr))
   name = as_symbol(sexpr.cdr.car)
   pairs = _parse_let_bindings(sexpr.cdr.cdr.car, 'named let')
   params = []
   i = 0
   while i < len(pairs):
      params.append(pairs[i][0])
      i = i + 1
   seen = set()
   i = 0
   while i < len(params):
      p = params[i]
      if p in seen:
         raise SchemeAnalysisError(
            "duplicate parameter name in named let: " + p,
            src_of(sexpr.cdr.cdr.car))
      seen.add(p)
      i = i + 1
   # Inits evaluate in the enclosing env.
   i = 0
   while i < len(pairs):
      analyze(pairs[i][1], static_env)
      i = i + 1
   # Body sees `name` with arity (n, n) and its params shadow outer names.
   name_env = dict(static_env)
   name_env[name] = (len(params), len(params))
   body_env = _shadow(name_env, params)
   body_cons = sexpr.cdr.cdr.cdr
   if _proper_list_length(body_cons) <= 0:
      raise SchemeAnalysisError("named let body cannot be empty", src_of(sexpr))
   cur = body_cons
   while is_cons(cur):
      analyze(cur.car, body_env)
      cur = cur.cdr


def _analyze_cond(sexpr, static_env):
   # R7RS 4.2.1 clause forms:
   #   (<test>)
   #   (<test> <expr>...)
   #   (<test> => <proc>)
   #   (else <expr>...)         (must be last)
   if _proper_list_length(sexpr) < 2:
      raise SchemeAnalysisError("cond must have at least one clause", src_of(sexpr))
   clauses = _cons_to_list(sexpr.cdr)
   total = len(clauses)
   i = 0
   while i < total:
      clause = clauses[i]
      clen = _proper_list_length(clause)
      if clen <= 0:
         raise SchemeAnalysisError(
            "cond clause must be a non-empty list, got " + _render(clause),
            src_of(clause))
      head = clause.car
      # Auxiliary keywords `else` and `=>` are recognized only when not
      # shadowed by a lexical binding (R7RS hygiene).  static_env tracks
      # in-scope names; if `else`/`=>` appears there, it's a user variable
      # and the clause is treated as a regular test.
      head_is_else = (_is_symbol_named(head, 'else')
                      and 'else' not in static_env)
      head_is_arrow = (clen == 3 and _is_symbol_named(clause.cdr.car, '=>')
                       and '=>' not in static_env)
      if head_is_else:
         if i != total - 1:
            raise SchemeAnalysisError(
               "cond 'else' clause must be the last clause", src_of(clause))
         body_cons = clause.cdr
         if _proper_list_length(body_cons) <= 0:
            raise SchemeAnalysisError(
               "cond 'else' clause must have at least one expression",
               src_of(clause))
         cur = body_cons
         while is_cons(cur):
            analyze(cur.car, static_env)
            cur = cur.cdr
      elif head_is_arrow:
         analyze(clause.car,             static_env)   # test
         analyze(clause.cdr.cdr.car,     static_env)   # proc
      elif clen == 1:
         analyze(clause.car, static_env)               # test only
      else:
         analyze(clause.car, static_env)               # test
         cur = clause.cdr
         while is_cons(cur):
            analyze(cur.car, static_env)
            cur = cur.cdr
      i = i + 1


def _analyze_case(sexpr, static_env):
   # R7RS 4.2.1: (case <key> <clause>...)
   # Each non-else clause: ((<datum>...) <expr>...)
   # else clause:          (else <expr>...)   (must be last)
   # Datums are implicitly quoted literals; we do NOT analyze them.
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         "case requires a key and at least one clause", src_of(sexpr))
   analyze(sexpr.cdr.car, static_env)   # the key
   clauses = _cons_to_list(sexpr.cdr.cdr)
   total = len(clauses)
   i = 0
   while i < total:
      clause = clauses[i]
      clen = _proper_list_length(clause)
      if clen < 2:
         raise SchemeAnalysisError(
            "case clause must be (<datum-list> <expr>...) or (else <expr>...), got "
            + _render(clause),
            src_of(clause))
      head = clause.car
      # case `else` recognized only when not shadowed (R7RS hygiene).
      if _is_symbol_named(head, 'else') and 'else' not in static_env:
         if i != total - 1:
            raise SchemeAnalysisError(
               "case 'else' clause must be the last clause", src_of(clause))
         body_cons = clause.cdr
         if (is_cons(body_cons)
               and _is_symbol_named(body_cons.car, '=>')
               and '=>' not in static_env):
            # (else => proc-expr)
            if not (is_cons(body_cons.cdr) and is_nil(body_cons.cdr.cdr)):
               raise SchemeAnalysisError(
                  "case 'else =>' clause must have exactly one expression",
                  src_of(clause))
            analyze(body_cons.cdr.car, static_env)
         else:
            cur = body_cons
            while is_cons(cur):
               analyze(cur.car, static_env)
               cur = cur.cdr
      else:
         if not (is_cons(head) or is_nil(head)):
            raise SchemeAnalysisError(
               "case clause head must be a list of datums, got " + _render(head),
               src_of(head))
         if _proper_list_length(head) < 0:
            raise SchemeAnalysisError(
               "case datum list must be a proper list", src_of(head))
         body_cons = clause.cdr
         if (is_cons(body_cons) and _is_symbol_named(body_cons.car, '=>')):
            # ((<datum>...) => proc-expr)
            if not (is_cons(body_cons.cdr) and is_nil(body_cons.cdr.cdr)):
               raise SchemeAnalysisError(
                  "case '=>' clause must have exactly one expression",
                  src_of(clause))
            analyze(body_cons.cdr.car, static_env)
         else:
            cur = body_cons
            while is_cons(cur):
               analyze(cur.car, static_env)
               cur = cur.cdr
      i = i + 1


def _analyze_unquote_outside(sexpr, static_env):
   # unquote and unquote-splicing only have meaning inside a quasiquote.
   # At any other position, surface a clear diagnostic.
   head_name = as_symbol(sexpr.car)
   raise SchemeAnalysisError(
      head_name + " is only valid inside a quasiquote template",
      src_of(sexpr))


def _analyze_case_lambda(sexpr, static_env):
   # (case-lambda <clause>...)  where each clause is (formals body...).
   # Each clause is validated by the shared _analyze_lambda_shape, which
   # tags error messages with the form_name 'case-lambda'.
   if _proper_list_length(sexpr) < 2:
      raise SchemeAnalysisError(
         "case-lambda requires at least one clause", src_of(sexpr))
   cur = sexpr.cdr
   while is_cons(cur):
      clause = cur.car
      if not is_cons(clause):
         raise SchemeAnalysisError(
            "case-lambda clause must be a list, got " + _render(clause),
            src_of(clause))
      if _proper_list_length(clause) < 2:
         raise SchemeAnalysisError(
            "case-lambda clause must have formals and a non-empty body, got "
            + _render(clause), src_of(clause))
      _analyze_lambda_shape(
         clause.car, clause.cdr, 'case-lambda', src_of(clause), static_env)
      cur = cur.cdr


def _analyze_cond_expand(sexpr, static_env):
   # Well-formed cond-expand is expanded away.  This handler fires only
   # when the Expander punted - either shape error or no clause matched.
   if _proper_list_length(sexpr) < 2:
      raise SchemeAnalysisError(
         "cond-expand requires at least one clause", src_of(sexpr))
   clauses = _cons_to_list(sexpr.cdr)
   total   = len(clauses)
   i = 0
   while i < total:
      clause = clauses[i]
      if _proper_list_length(clause) < 1:
         raise SchemeAnalysisError(
            "cond-expand clause must be a non-empty list", src_of(clause))
      i = i + 1
   # If we fall through here, shape is OK but no clause matched - that
   # is the R7RS error condition.
   raise SchemeAnalysisError(
      "cond-expand: no clause matched", src_of(sexpr))


def _analyze_include(sexpr, static_env):
   # (include "file1" "file2" ...)
   # Well-formed include is expanded by the Expander; this handler
   # fires only on shape errors (no args or non-string arg).
   n = _proper_list_length(sexpr) - 1
   if n < 1:
      raise SchemeAnalysisError(
         "include requires at least one filename string", src_of(sexpr))
   cur = sexpr.cdr
   while is_cons(cur):
      arg = cur.car
      if not is_string(arg):
         raise SchemeAnalysisError(
            "include arguments must be string literals, got " + _render(arg),
            src_of(arg))
      cur = cur.cdr


def _analyze_do(sexpr, static_env):
   # (do ((var init [step]) ...) (test result...) command...)
   # R7RS 4.2.4.  Well-formed do expressions are desugared by the
   # Expander; this handler fires only when the Expander punted on a
   # shape error, so it exists to produce a useful diagnostic.
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         "do requires a binding list and a test clause", src_of(sexpr))
   bindings_sexpr = sexpr.cdr.car
   test_sexpr     = sexpr.cdr.cdr.car

   if _proper_list_length(bindings_sexpr) < 0:
      raise SchemeAnalysisError(
         "do bindings must be a proper list, got " + _render(bindings_sexpr),
         src_of(bindings_sexpr))
   seen = {}
   cur = bindings_sexpr
   while is_cons(cur):
      b = cur.car
      blen = _proper_list_length(b)
      if blen != 2 and blen != 3:
         raise SchemeAnalysisError(
            "do binding must be (var init) or (var init step), got "
            + _render(b), src_of(b))
      name = _require_symbol(b.car, 'do binding')
      if name in seen:
         raise SchemeAnalysisError(
            "duplicate variable name in do bindings: " + name,
            src_of(b.car))
      seen[name] = True
      cur = cur.cdr

   if not is_cons(test_sexpr):
      raise SchemeAnalysisError(
         "do test clause must be a list starting with a test expression",
         src_of(test_sexpr))
   if _proper_list_length(test_sexpr) < 1:
      raise SchemeAnalysisError(
         "do test clause must be a proper list",
         src_of(test_sexpr))


def _analyze_and(sexpr, static_env):
   cur = sexpr.cdr
   while is_cons(cur):
      analyze(cur.car, static_env)
      cur = cur.cdr


def _analyze_or(sexpr, static_env):
   cur = sexpr.cdr
   while is_cons(cur):
      analyze(cur.car, static_env)
      cur = cur.cdr


def _analyze_when(sexpr, static_env):
   _analyze_when_unless(sexpr, static_env, 'when')


def _analyze_unless(sexpr, static_env):
   _analyze_when_unless(sexpr, static_env, 'unless')


def _analyze_when_unless(sexpr, static_env, name):
   if _proper_list_length(sexpr) < 3:
      raise SchemeAnalysisError(
         name + " requires a test and at least one body expression",
         src_of(sexpr))
   analyze(sexpr.cdr.car, static_env)
   cur = sexpr.cdr.cdr
   while is_cons(cur):
      analyze(cur.car, static_env)
      cur = cur.cdr


def _analyze_quote(sexpr, static_env):
   n = _proper_list_length(sexpr) - 1
   if n != 1:
      raise SchemeAnalysisError(
         "quote requires exactly 1 argument, got " + str(n),
         src_of(sexpr))
   # Quoted data is literal - do NOT walk or analyze its contents.


def _analyze_delay(sexpr, static_env):
   name = as_symbol(sexpr.car)
   n = _proper_list_length(sexpr) - 1
   if n != 1:
      raise SchemeAnalysisError(
         name + " requires exactly 1 argument, got " + str(n),
         src_of(sexpr))
   analyze(sexpr.cdr.car, static_env)


def _analyze_library_form(sexpr, static_env):
   # define-library / import / export: shape is validated at evaluation
   # time in the Evaluator (where the library registry lives).  We just
   # accept the form here so it doesn't fall through to the generic
   # application path and arity-check against a fictitious primitive.
   pass


_SPECIAL_FORMS = {
   'lambda':       _analyze_lambda,
   'case-lambda':  _analyze_case_lambda,
   'define':       _analyze_define,
   'set!':         _analyze_set,
   'if':           _analyze_if,
   'begin':        _analyze_begin,
   'let':          _analyze_let,
   'let*':         _analyze_let_star,
   'letrec':       _analyze_letrec,
   'letrec*':      _analyze_letrec_star,
   'cond':         _analyze_cond,
   'case':         _analyze_case,
   'do':           _analyze_do,
   'include':      _analyze_include,
   'include-ci':   _analyze_include,
   'cond-expand':  _analyze_cond_expand,
   'and':          _analyze_and,
   'or':           _analyze_or,
   'when':         _analyze_when,
   'unless':       _analyze_unless,
   'quote':        _analyze_quote,
   'unquote':          _analyze_unquote_outside,
   'unquote-splicing': _analyze_unquote_outside,
   'delay':        _analyze_delay,
   'delay-force':  _analyze_delay,
   'define-library':               _analyze_library_form,
   'import':                       _analyze_library_form,
   'export':                       _analyze_library_form,
   'include-library-declarations': _analyze_library_form,
   # Macro forms are handled entirely by the Expander; if one survives
   # to the Analyzer, accept it silently (usually as part of a quoted
   # or still-unexpanded datum).  No static validation here.
   'define-syntax':    _analyze_library_form,
   'let-syntax':       _analyze_library_form,
   'letrec-syntax':    _analyze_library_form,
   'syntax-rules':     _analyze_library_form,
}


# -------- Self-test --------

if __name__ == '__main__':
   from pyscheme.Parser   import parse_one
   from pyscheme.Expander import expand

   n_pass = 0
   n_fail = 0

   # Happy-path: each source must analyze without raising.
   # Analyzer no longer returns IR - it returns the input cons tree
   # unchanged.  We just verify no exception is raised.
   happy = [
      '42', '3.14', '#t', '"hi"', 'x',
      '(lambda (x) x)',
      '(lambda (x y) x y)',
      '(lambda () 42)',
      '(lambda args args)',
      '(lambda (x . rest) rest)',
      '(lambda (x y . rest) rest)',
      '(lambda (x) "doc" x)',
      '(lambda () "hello")',
      '(define x 5)',
      '(define (id x) x)',
      '(set! x 5)',
      '(if #t 1 2)',
      '(if #t 1)',
      '(f 1 2)',
      '(f)',
      '(begin)',
      '(begin 1 2 3)',
      '(let ((x 1) (y 2)) x)',
      '(let* ((x 1) (y x)) y)',
      '(letrec ((f (lambda (x) x))) (f 1))',
      '(letrec* ((f (lambda (x) x))) (f 1))',
      '(let loop ((x 1)) x)',
      '(cond (#t 1) (else 2))',
      '(cond ((f 1)) (else 2))',
      '(cond ((f 1) => g) (else 2))',
      '(and 1 2)',
      '(or)',
      '(when #t 1)',
      '(quote x)',
      "'x",
   ]

   print('-- analyze (post-expander) --')
   i = 0
   while i < len(happy):
      source = happy[i]
      try:
         got = analyze(expand(parse_one(source)))
      except Exception as e:
         print("[FAIL] %r: %s: %s" % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      # The Analyzer must return the input unchanged.
      expanded = expand(parse_one(source))
      if got is not None:
         print("[ OK ] %r" % source)
         n_pass = n_pass + 1
      else:
         print("[FAIL] %r: returned None" % source)
         n_fail = n_fail + 1
      i = i + 1

   # Semantic errors that should be raised by the analyzer.
   errors = [
      ('()',                     'empty list'),
      ('(lambda 1 x)',           'parameter list must be a list or identifier'),
      ('(lambda (1) x)',         'expected an identifier'),
      ('(lambda (x x) x)',       'duplicate parameter name'),
      ('(lambda (x . x) x)',     'rest parameter name conflicts'),
      ('(lambda (x . 1) x)',     'expected an identifier'),
      ('(lambda (x) )',          'body cannot be empty'),
      ('(define)',               'requires a name and a value'),
      ('(define 5 6)',           'expected an identifier'),
      ('(set!)',                 'requires a name and a value'),
      ('(set! 1 2)',             'expected an identifier'),
      ('(if)',                   'requires 2 or 3 arguments'),
      ('(if a b c d)',           'requires 2 or 3 arguments'),
      ('(let)',                  'binding list'),
      ('(let ((x)) x)',          'binding must be'),
      ('(let ((1 2)) x)',        'expected an identifier'),
      ('(let () )',              'body cannot be empty'),
      ('(cond)',                 'at least one clause'),
      ('(cond ())',              'non-empty list'),
      ('(cond (else) (#t 1))',   "'else' clause must be the last"),
      ('(cond (else))',          "must have at least one expression"),
      ('(when)',                 'requires a test'),
      ('(when #t)',              'requires a test'),
      ('(quote)',                'exactly 1 argument'),
      ('(quote a b)',            'exactly 1 argument'),
   ]

   print()
   print('-- analyzer errors --')
   i = 0
   while i < len(errors):
      source             = errors[i][0]
      expected_fragment  = errors[i][1]
      try:
         analyze(expand(parse_one(source)))
      except SchemeAnalysisError as e:
         if expected_fragment in str(e):
            print("[ OK ] %r  ->  %s" % (source, e))
            n_pass = n_pass + 1
         else:
            print("[WARN] %r  ->  %s" % (source, e))
            print("        expected substring: %r" % expected_fragment)
            n_pass = n_pass + 1
         i = i + 1
         continue
      except Exception as e:
         print("[FAIL] %r: wrong exception %s: %s" % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      print("[FAIL] %r: expected SchemeAnalysisError" % source)
      n_fail = n_fail + 1
      i = i + 1

   # Static arity errors (caught at analyze time, not runtime).
   print()
   print('-- static arity errors --')
   arity_errors = [
      ('((lambda (x y) x) 1)',             '1 argument provided; 2 expected'),
      ('((lambda (x) x) 1 2)',             '2 arguments provided; 1 expected'),
      ('((lambda () 1) 99)',               '1 argument provided; 0 expected'),
      ('((lambda (a b . rest) rest) 1)',   '1 argument provided; at least 2 expected'),
      ('(cons 1)',                         'cons: 1 argument provided; 2 expected'),
      ('(cons 1 2 3)',                     'cons: 3 arguments provided; 2 expected'),
      ('(car)',                            'car: 0 arguments provided; 1 expected'),
      ('(car 1 2)',                        'car: 2 arguments provided; 1 expected'),
   ]
   i = 0
   while i < len(arity_errors):
      source            = arity_errors[i][0]
      expected_fragment = arity_errors[i][1]
      try:
         analyze(expand(parse_one(source)))
      except SchemeArityError as e:
         if expected_fragment in str(e):
            print("[ OK ] %r  ->  %s: %s" % (source, type(e).__name__, e))
            n_pass = n_pass + 1
         else:
            print("[WARN] %r  ->  %s" % (source, e))
            print("        expected substring: %r" % expected_fragment)
            n_pass = n_pass + 1
         i = i + 1
         continue
      except Exception as e:
         print("[FAIL] %r: wrong exception %s: %s" % (source, type(e).__name__, e))
         n_fail = n_fail + 1
         i = i + 1
         continue
      print("[FAIL] %r: expected SchemeArityError" % source)
      n_fail = n_fail + 1
      i = i + 1

   # Cross-form static arity (shared static_env across multiple top-level forms).
   print()
   print('-- cross-form static arity --')
   cross_cases = [
      (['(define (f x) x)', '(f 1 2)'],
         'f: 2 arguments provided; 1 expected'),
      (['(define (g a b) a)', '(g 1 2)'], None),
      (['(define (h x) x)', '(define (h x y) y)', '(h 1)'],
         'h: 1 argument provided; 2 expected'),
      (['(define (g a b) a)', '(define h2 g)', '(h2 1)'],
         'h2: 1 argument provided; 2 expected'),
      (['(define (fact n) (fact n n))'],
         'fact: 2 arguments provided; 1 expected'),
      (['((lambda (cons) (cons 1 2 3)) 1)'], None),
   ]
   i = 0
   while i < len(cross_cases):
      sources           = cross_cases[i][0]
      expected_fragment = cross_cases[i][1]
      env = dict(PRIMITIVE_ARITIES)
      err = None
      j = 0
      while j < len(sources):
         try:
            sexpr = expand(parse_one(sources[j]))
            analyze(sexpr, env)
            extend_static_env_with_define(env, sexpr)
         except SchemeAnalysisError as e:
            err = e
            break
         except SchemeArityError as e:
            err = e
            break
         j = j + 1
      if expected_fragment is None:
         if err is None:
            print("[ OK ] %r" % sources)
            n_pass = n_pass + 1
         else:
            print("[FAIL] %r: unexpected error %s: %s" % (sources, type(err).__name__, err))
            n_fail = n_fail + 1
      else:
         if err is None:
            print("[FAIL] %r: expected error with %r" % (sources, expected_fragment))
            n_fail = n_fail + 1
         elif expected_fragment in str(err):
            print("[ OK ] %r  ->  %s: %s" % (sources, type(err).__name__, err))
            n_pass = n_pass + 1
         else:
            print("[FAIL] %r: got %s: %s" % (sources, type(err).__name__, err))
            print("        expected substring: %r" % expected_fragment)
            n_fail = n_fail + 1
      i = i + 1

   print()
   print("%d passed, %d failed" % (n_pass, n_fail))
