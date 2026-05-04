"""Lazy evaluation: delay, delay-force, force, make-promise, promise?.

Special forms delay and delay-force are handled by the evaluator; their
stubs make them visible in (help).  force, make-promise, and promise? are
real primitives.
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import is_promise, make_boolean, make_promise_done
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'lazy'
_SPECIAL = 'special'


def _stub(form_name):
   raise RuntimeError(
      repr(form_name) + ' is a special form, not a procedure; it cannot be '
      'applied as a first-class value.  This stub exists only to carry '
      'documentation into the help system.')


def _form_delay(ctx, env, args, app_node):       _stub('delay')
def _form_delay_force(ctx, env, args, app_node): _stub('delay-force')


def _prim_force_unreached(ctx, env, args, app_node):
   raise SchemeTypeError(
      'force: cannot be called through a re-entering path in this '
      'implementation', app_node)


def _prim_make_promise(ctx, env, args, app_node):
   return make_promise_done(args[0])


def _prim_promise_p(ctx, env, args, app_node):
   return make_boolean(is_promise(args[0]))


def register():
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

   register_primitive('force', (1, 1), _prim_force_unreached,
      doc=(
         "Force a promise, returning its value.  The promise's thunk runs\n"
         "at most once; subsequent forces return the cached value.  If the\n"
         "thunk yields another promise, force follows the chain iteratively,\n"
         "so (delay-force ...) promise chains run in constant stack.  A\n"
         "non-promise argument is returned unchanged (R7RS-small 6.10 leaves\n"
         "this implementation-defined; we follow SRFI 155)."),
      category=CATEGORY)

   register_primitive('make-promise', (1, 1), _prim_make_promise,
      doc=(
         "Return a promise whose forced value is obj.  Unlike delay,\n"
         "make-promise is a procedure, not a special form: its argument\n"
         "is evaluated eagerly and the resulting promise is already forced."),
      category=CATEGORY)

   register_primitive('promise?', (1, 1), _prim_promise_p,
      doc='Return #t if a is a promise (created by delay, delay-force, or make-promise).',
      category=CATEGORY)
