"""Documentation stubs for module and library special forms.

All forms here are handled by the evaluator or expander, not as primitives.
Stubs make them visible in (help) and (apropos).
"""

from pyscheme.primitives import register_primitive


CATEGORY = 'modules'
_SPECIAL = 'special'


def _stub(form_name):
   raise RuntimeError(
      repr(form_name) + ' is a special form, not a procedure; it cannot be '
      'applied as a first-class value.  This stub exists only to carry '
      'documentation into the help system.')


def _form_include(ctx, env, args, app_node):
   _stub('include')
def _form_include_ci(ctx, env, args, app_node):
   _stub('include-ci')
def _form_cond_expand(ctx, env, args, app_node):
   _stub('cond-expand')
def _form_define_library(ctx, env, args, app_node):
   _stub('define-library')
def _form_import(ctx, env, args, app_node):
   _stub('import')
def _form_export(ctx, env, args, app_node):
   _stub('export')


def register():
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
