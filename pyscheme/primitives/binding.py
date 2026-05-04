"""Documentation stubs for definition and binding special forms.

All forms here are handled by the evaluator, not as primitives.  Stubs make
them visible in (help) and (apropos).  A stub would only be reached by
pathological code like `(apply define ...)`.
"""

from pyscheme.primitives import register_primitive


CATEGORY = 'binding'
_SPECIAL = 'special'


def _stub(form_name):
   raise RuntimeError(
      repr(form_name) + ' is a special form, not a procedure; it cannot be '
      'applied as a first-class value.  This stub exists only to carry '
      'documentation into the help system.')


def _form_lambda(ctx, env, args, app_node):             _stub('lambda')
def _form_case_lambda(ctx, env, args, app_node):        _stub('case-lambda')
def _form_define(ctx, env, args, app_node):             _stub('define')
def _form_set(ctx, env, args, app_node):                _stub('set!')
def _form_let(ctx, env, args, app_node):                _stub('let')
def _form_let_star(ctx, env, args, app_node):           _stub('let*')
def _form_letrec(ctx, env, args, app_node):             _stub('letrec')
def _form_letrec_star(ctx, env, args, app_node):        _stub('letrec*')
def _form_let_values(ctx, env, args, app_node):         _stub('let-values')
def _form_let_star_values(ctx, env, args, app_node):    _stub('let*-values')
def _form_define_values(ctx, env, args, app_node):      _stub('define-values')
def _form_define_record_type(ctx, env, args, app_node): _stub('define-record-type')


def register():
   register_primitive('lambda', (2, None), _form_lambda,
      usage='(lambda <formals> <body>...)',
      doc=(
         "Create a procedure.  <formals> is (v1 v2 ...) for fixed arity,\n"
         "<var> alone for pure variadic (all arguments collected into <var> as a\n"
         "list), or (v1 ... . <rest>) for a fixed prefix followed by the remaining\n"
         "arguments collected into <rest>.  The <body> may begin with a string\n"
         "literal, which is taken as the procedure's documentation."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('case-lambda', (1, None), _form_case_lambda,
      usage='(case-lambda (<formals> <body>...)...)',
      doc=(
         "Create an arity-dispatched procedure.  Each clause is shaped\n"
         "like a lambda: (<formals> <body>...).  When the procedure is\n"
         "called, the first clause whose <formals> accept the argument\n"
         "count is selected and its body is run.  <formals> follows the\n"
         "same shape rules as lambda (fixed list, symbol, or fixed+rest).\n"
         "R7RS 4.2.9."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('define', (2, 2), _form_define,
      usage='(define <name> <value>)',
      doc=(
         "Bind <name> to the value of <value> in the current environment.\n"
         "The shorthand (define (<name> <formals>...) <body>...) is equivalent to\n"
         "(define <name> (lambda (<formals>...) <body>...))."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('set!', (2, 2), _form_set,
      usage='(set! <name> <value>)',
      doc=(
         "Update the existing binding for <name> in the nearest enclosing\n"
         "scope to the value of <value>.  If <name> has no binding, an error is\n"
         "raised (unlike Common Lisp's setq which would create a top-level binding)."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('let', (2, None), _form_let,
      usage='(let [<name>] ((<var> <init>)...) <body>...)',
      doc=(
         "Evaluate every <init> in the enclosing env, then bind each <var>\n"
         "to its corresponding value and evaluate <body>.  With an optional <name>\n"
         "before the binding list, creates a named let: a local recursive procedure\n"
         "<name> bound to (lambda (<var>...) <body>...), immediately applied to the\n"
         "inits.  Useful for iteration."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('let*', (2, None), _form_let_star,
      usage='(let* ((<var> <init>)...) <body>...)',
      doc=(
         "Like let, but each <init> is evaluated in an environment where\n"
         "the earlier bindings are in scope.  Equivalent to nested single-binding\n"
         "lets."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('letrec', (2, None), _form_letrec,
      usage='(letrec ((<var> <init>)...) <body>...)',
      doc=(
         "Like let, but each <init> is evaluated in an environment where\n"
         "all the <var>s are already in scope (initially bound to unspecified\n"
         "values).  Used for mutual recursion.  R7RS does not specify the order\n"
         "in which inits evaluate; this implementation is sequential."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('letrec*', (2, None), _form_letrec_star,
      usage='(letrec* ((<var> <init>)...) <body>...)',
      doc=(
         "Like letrec, but evaluates the inits strictly left to right and\n"
         "guarantees each prior binding is set before the next init runs."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('let-values', (2, None), _form_let_values,
      usage='(let-values ((<formals> <init>)...) <body>...)',
      doc=(
         "Parallel multi-value bindings.  Each <init> is evaluated in the\n"
         "outer environment; its return values are bound to the variables\n"
         "in the matching <formals>.  All bindings become visible together\n"
         "when <body> runs.  <formals> follows lambda's shape (proper list,\n"
         "dotted tail, or a single identifier).  R7RS 4.2.2."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('let*-values', (2, None), _form_let_star_values,
      usage='(let*-values ((<formals> <init>)...) <body>...)',
      doc=(
         "Sequential multi-value bindings.  Like let-values, but each\n"
         "<init> sees the variables bound by earlier clauses.  R7RS 4.2.2."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('define-values', (2, 2), _form_define_values,
      usage='(define-values <formals> <expr>)',
      doc=(
         "Bind each identifier in <formals> to the corresponding value\n"
         "produced by <expr>.  <formals> follows lambda's shape: a proper\n"
         "list, a dotted tail, or a single identifier that receives all\n"
         "values as a list.  Useful for destructuring a multi-value\n"
         "producer at top level.  R7RS 5.3.3."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('define-record-type', (4, None), _form_define_record_type,
      usage=(
         '(define-record-type <name> '
         '(<ctor> <ctor-field>...) <predicate> <field-spec>...)'),
      doc=(
         "Define a record type.  <name> is the type's symbol.  The\n"
         "constructor clause (<ctor> <ctor-field>...) names the constructor\n"
         "procedure and lists the fields it initializes (other fields get\n"
         "#<void>).  <predicate> names the type predicate.  Each <field-spec>\n"
         "is (<field-name> <accessor>) or (<field-name> <accessor> <mutator>);\n"
         "the order of field specs defines field indices.  Records are a\n"
         "disjoint type - a record is never eq? to any other Scheme value.\n"
         "R7RS 5.5."),
      category=CATEGORY, kind=_SPECIAL)
