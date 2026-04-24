"""Documentation stubs for the special forms.

Special forms (lambda, if, set!, define, begin, ...) are handled in the
evaluator, not as primitives.  But to make them visible in (help),
(apropos), and auto-completion, each is registered here with a stub
function whose body should never execute.  register_primitive records
the stub's arity, usage string, and docstring in PRIMITIVE_HELP, and
install_primitives binds it into the global env so `(help if)` works
without quoting.

A stub would only be reached by pathological code like `(apply if ...)`
where the form is applied as a first-class procedure; special forms
cannot be applied in R7RS, so raising from the stub is the right thing.
"""

from pyscheme.primitives import register_primitive


CATEGORY = 'control'
_SPECIAL = 'special'


def _stub(form_name):
   raise RuntimeError(
      repr(form_name) + ' is a special form, not a procedure; it cannot be '
      'applied as a first-class value.  This stub exists only to carry '
      'documentation into the help system.')


def _form_lambda(ctx, env, args, app_node):   _stub('lambda')
def _form_case_lambda(ctx, env, args, app_node): _stub('case-lambda')
def _form_define(ctx, env, args, app_node):   _stub('define')
def _form_set(ctx, env, args, app_node):      _stub('set!')
def _form_if(ctx, env, args, app_node):       _stub('if')
def _form_when(ctx, env, args, app_node):     _stub('when')
def _form_unless(ctx, env, args, app_node):   _stub('unless')
def _form_cond(ctx, env, args, app_node):     _stub('cond')
def _form_case(ctx, env, args, app_node):     _stub('case')
def _form_do(ctx, env, args, app_node):       _stub('do')
def _form_include(ctx, env, args, app_node):  _stub('include')
def _form_include_ci(ctx, env, args, app_node): _stub('include-ci')
def _form_cond_expand(ctx, env, args, app_node): _stub('cond-expand')
def _form_and(ctx, env, args, app_node):      _stub('and')
def _form_or(ctx, env, args, app_node):       _stub('or')
def _form_begin(ctx, env, args, app_node):    _stub('begin')
def _form_let(ctx, env, args, app_node):      _stub('let')
def _form_let_star(ctx, env, args, app_node): _stub('let*')
def _form_letrec(ctx, env, args, app_node):   _stub('letrec')
def _form_letrec_star(ctx, env, args, app_node): _stub('letrec*')
def _form_quote(ctx, env, args, app_node):    _stub('quote')
def _form_quasiquote(ctx, env, args, app_node): _stub('quasiquote')
def _form_unquote(ctx, env, args, app_node):  _stub('unquote')
def _form_unquote_splicing(ctx, env, args, app_node): _stub('unquote-splicing')
def _form_delay(ctx, env, args, app_node):    _stub('delay')
def _form_delay_force(ctx, env, args, app_node): _stub('delay-force')
def _form_let_values(ctx, env, args, app_node):  _stub('let-values')
def _form_let_star_values(ctx, env, args, app_node): _stub('let*-values')
def _form_define_values(ctx, env, args, app_node):   _stub('define-values')
def _form_define_record_type(ctx, env, args, app_node): _stub('define-record-type')
def _form_parameterize(ctx, env, args, app_node): _stub('parameterize')
def _form_guard(ctx, env, args, app_node):        _stub('guard')
def _form_define_library(ctx, env, args, app_node): _stub('define-library')
def _form_import(ctx, env, args, app_node):         _stub('import')
def _form_export(ctx, env, args, app_node):         _stub('export')
def _form_define_syntax(ctx, env, args, app_node):  _stub('define-syntax')
def _form_let_syntax(ctx, env, args, app_node):     _stub('let-syntax')
def _form_letrec_syntax(ctx, env, args, app_node):  _stub('letrec-syntax')
def _form_syntax_rules(ctx, env, args, app_node):   _stub('syntax-rules')


def register():
   # Procedures and binding
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

   # Conditionals
   register_primitive('if', (2, 3), _form_if,
      usage='(if <test> <then> [<else>])',
      doc=(
         "Evaluate <test>.  If truthy, evaluate and return <then>; otherwise\n"
         "evaluate and return <else> if present, or an unspecified value if not.\n"
         "In Scheme only #f is false; every other value (including 0 and '()) is\n"
         "truthy."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('when', (2, None), _form_when,
      usage='(when <test> <body>...)',
      doc=(
         "If <test> is truthy, evaluate <body> in sequence and return the\n"
         "last value.  Otherwise, return an unspecified value."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('unless', (2, None), _form_unless,
      usage='(unless <test> <body>...)',
      doc=(
         "If <test> is false, evaluate <body> in sequence and return the\n"
         "last value.  Otherwise, return an unspecified value."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('cond', (1, None), _form_cond,
      usage='(cond <clause>...)',
      doc=(
         "Evaluate clauses in order.  Each clause has one of the forms:\n"
         "(<test>)                -> returns the test value if truthy\n"
         "(<test> <expr>...)      -> evaluates the body if the test is truthy\n"
         "(<test> => <proc>)      -> if truthy, applies <proc> to the test value\n"
         "(else <expr>...)        -> unconditional; must be the last clause\n"
         "If no clause matches, returns an unspecified value."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('case', (2, None), _form_case,
      usage='(case <key> <clause>...)',
      doc=(
         "Evaluate <key>, then match its value against each clause's datum\n"
         "list using eqv?.  Each clause has one of the forms:\n"
         "((<datum>...) <expr>...)  -> run body if key matches any datum\n"
         "(else <expr>...)          -> unconditional; must be the last clause\n"
         "Datums are implicitly quoted and are not evaluated.  If no clause\n"
         "matches, returns an unspecified value."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('do', (2, None), _form_do,
      usage='(do ((<var> <init> [<step>])...) (<test> <result>...) <command>...)',
      doc=(
         "Iterative construct.  Bind each <var> to its <init>, then repeatedly:\n"
         "  evaluate <test>; if true, evaluate <result> expressions in order and\n"
         "  return the last value (or an unspecified value if <result> is empty);\n"
         "  otherwise evaluate <command>s for effect, evaluate each <step> in\n"
         "  the current env, rebind each <var> to its step's value, then loop.\n"
         "A binding of the form (<var> <init>) keeps <var> at its current value\n"
         "each iteration (no step).  Implemented as a desugar to named let."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('include', (1, None), _form_include,
      usage='(include <filename>...)',
      doc=(
         "Splice the contents of one or more Scheme source files into the\n"
         "enclosing program.  Each filename must be a string literal.  The\n"
         "included forms are parsed and expanded as if typed in place,\n"
         "wrapped in an implicit (begin ...).  R7RS 5.6.1."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('include-ci', (1, None), _form_include_ci,
      usage='(include-ci <filename>...)',
      doc=(
         "Like include, but symbol names in the included source are\n"
         "case-folded to lowercase (R7RS 5.6.1).  Useful for consuming\n"
         "traditional Lisp source that relies on case-insensitive reads."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('cond-expand', (1, None), _form_cond_expand,
      usage='(cond-expand <clause>...)',
      doc=(
         "Expand-time conditional.  Each clause has the form\n"
         "   (<feature-requirement> <body>...)\n"
         "or (else <body>...).  The first clause whose <feature-requirement>\n"
         "is satisfied by the current implementation is selected; its body\n"
         "is spliced in place of the cond-expand form.  Feature requirements\n"
         "are feature identifiers (r7rs, exact-closed, pyscheme), (and ...),\n"
         "(or ...), (not ...), or (library <name>).  R7RS 5.6.2."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('and', (0, None), _form_and,
      usage='(and <expr>...)',
      doc=(
         "Evaluate expressions left to right.  Short-circuit and return #f\n"
         "on the first false expression; otherwise return the last value.  With no\n"
         "arguments, returns #t."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('or', (0, None), _form_or,
      usage='(or <expr>...)',
      doc=(
         "Evaluate expressions left to right.  Short-circuit and return\n"
         "the first truthy value; otherwise return #f.  With no arguments, returns #f."),
      category=CATEGORY, kind=_SPECIAL)

   # Sequencing
   register_primitive('begin', (1, None), _form_begin,
      usage='(begin <expr>...)',
      doc='Evaluate expressions in sequence; return the value of the last one.',
      category=CATEGORY, kind=_SPECIAL)

   # Binding constructs
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

   # Quotation
   register_primitive('quote', (1, 1), _form_quote,
      usage='(quote <datum>)',
      doc="Return the datum unevaluated.  'x is shorthand for (quote x).",
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('quasiquote', (1, 1), _form_quasiquote,
      usage='(quasiquote <template>)',
      doc=(
         "Return a value shaped like <template>, but with any (unquote e)\n"
         "holes replaced by the value of e, and any (unquote-splicing e)\n"
         "holes expanded by splicing the elements of e's list into the\n"
         "surrounding list.  Reader syntax: `x == (quasiquote x),\n"
         ",e == (unquote e), ,@e == (unquote-splicing e).  R7RS 4.2.8."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('unquote', (1, 1), _form_unquote,
      usage='(unquote <expr>)',
      doc="Unquote marker, valid only inside a quasiquote template.",
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('unquote-splicing', (1, 1), _form_unquote_splicing,
      usage='(unquote-splicing <expr>)',
      doc="Splicing-unquote marker, valid only inside a quasiquote template.",
      category=CATEGORY, kind=_SPECIAL)

   # Promises
   register_primitive('delay', (1, 1), _form_delay,
      usage='(delay <expr>)',
      doc=(
         "Return a promise whose forced value is the value of <expr>.\n"
         "<expr> is not evaluated until the promise is forced; its value is\n"
         "then cached so subsequent forces are O(1).  R7RS 4.2.5.  Use force\n"
         "to retrieve the value."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('delay-force', (1, 1), _form_delay_force,
      usage='(delay-force <expr>)',
      doc=(
         "Like delay, but in a form that permits stack-safe iterative\n"
         "forcing: if <expr> itself evaluates to a promise, force collapses\n"
         "the outer promise into the inner one rather than nesting.  Use\n"
         "this when building long lazy chains that force tail-recursively.\n"
         "R7RS 4.2.5."),
      category=CATEGORY, kind=_SPECIAL)

   # Multi-value binding forms
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

   # Macros (R7RS 4.3)
   register_primitive('define-syntax', (2, 2), _form_define_syntax,
      usage='(define-syntax <name> <transformer>)',
      doc=(
         "Bind <name> to a syntax transformer at expand time.  <transformer>\n"
         "must be a (syntax-rules ...) form.  Subsequent occurrences of\n"
         "<name> in head position will be expanded by the transformer.\n"
         "R7RS 4.3."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('let-syntax', (2, None), _form_let_syntax,
      usage='(let-syntax ((<name> <transformer>)...) <body>...)',
      doc=(
         "Locally bind one or more syntax transformers for the duration\n"
         "of <body>.  Each transformer's definition env is the enclosing\n"
         "scope (siblings are NOT visible to each other).  R7RS 4.3."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('letrec-syntax', (2, None), _form_letrec_syntax,
      usage='(letrec-syntax ((<name> <transformer>)...) <body>...)',
      doc=(
         "Like let-syntax, but each transformer's definition env includes\n"
         "its sibling transformers, so mutually recursive macros can be\n"
         "defined in one form.  R7RS 4.3."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('syntax-rules', (1, None), _form_syntax_rules,
      usage='(syntax-rules [<ellipsis>] (<literal>...) (<pattern> <template>)...)',
      doc=(
         "Create a syntax transformer.  Each <pattern> matches the use-site\n"
         "form structurally; the first matching rule's <template> is\n"
         "substituted with captured pattern variables.  Literals match\n"
         "themselves; '...' repeats the preceding pattern.  R7RS 4.3."),
      category=CATEGORY, kind=_SPECIAL)

   # Library system (R7RS 5.6)
   register_primitive('define-library', (2, None), _form_define_library,
      usage='(define-library <name> <decl>...)',
      doc=(
         "Declare a library named <name> (a list of symbols/integers,\n"
         "e.g. (scheme base) or (my utilities 1)).  Each <decl> is one\n"
         "of:\n"
         "  (import <import-set>...)   - bindings visible inside the library\n"
         "  (export <spec>...)         - names to expose; each spec is a\n"
         "                               symbol or (rename <int> <ext>)\n"
         "  (begin <form>...)          - definitions populating the lib env\n"
         "The library is registered in the global library registry and\n"
         "becomes available to (import ...).  R7RS 5.6."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('import', (1, None), _form_import,
      usage='(import <import-set>...)',
      doc=(
         "Import bindings from one or more libraries into the current\n"
         "environment.  Each <import-set> is either a library name or one\n"
         "of (only ... n...), (except ... n...), (rename ... (o n)...),\n"
         "(prefix ... p).  At top level, bindings are added to the global\n"
         "env; inside (define-library ...) they populate that library's\n"
         "isolated env.  R7RS 5.6."),
      category=CATEGORY, kind=_SPECIAL)

   register_primitive('export', (1, None), _form_export,
      usage='(export <spec>...)',
      doc=(
         "Valid only inside (define-library ...) as a declaration.  Each\n"
         "<spec> is a symbol or (rename <internal> <external>).  R7RS 5.6."),
      category=CATEGORY, kind=_SPECIAL)

   # Exception handling
   register_primitive('guard', (2, None), _form_guard,
      usage='(guard (<var> <clause>...) <body>...)',
      doc=(
         "Install an exception handler for <body>.  If raise or error fires\n"
         "during <body>, the handler binds <var> to the raised value and\n"
         "evaluates the <clause>s in cond-like fashion.  Clauses follow the\n"
         "same grammar as cond.  If no clause matches and no else is given,\n"
         "the raised value is re-raised in the outer scope.  R7RS 4.2.7."),
      category=CATEGORY, kind=_SPECIAL)

   # Parameters
   register_primitive('parameterize', (2, None), _form_parameterize,
      usage='(parameterize ((<param> <val>)...) <body>...)',
      doc=(
         "Dynamically bind parameter objects for the extent of <body>.\n"
         "Each <param> must evaluate to a parameter (made by make-parameter).\n"
         "Each <val> is bound as the parameter's dynamic value (converted\n"
         "if the parameter has a converter).  When <body> returns, the\n"
         "original values are restored - even if <body> raises.  R7RS 4.2.6."),
      category=CATEGORY, kind=_SPECIAL)

   # Records
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
