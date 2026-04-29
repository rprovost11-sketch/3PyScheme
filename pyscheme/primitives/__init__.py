"""Built-in primitive procedures for pyscheme.

Each primitive module (lists.py, arithmetic.py, ...) exposes a
register() function that calls register_primitive() once for each
primitive it defines.  __init__.py imports every module and then
invokes each module's register(), populating the tables.

Primitive signature: (ctx, env, args, app_node) where
    ctx       is the interpreter Context (for ctx.outStrm)
    env       is the caller's local env (chain up to global via parents)
    args      is the already-arity-checked list of tagged values
    app_node  is the application cons for source-position reporting

Arity: a (min, max) tuple; max=None means variadic.

Kind: 'primitive' (default) or 'special' for the stub entries in
control.py that document special forms the evaluator handles directly.
Stubs are bound into env alongside real primitives so (help <form>)
works without quoting; they never execute in practice because the
evaluator dispatches special forms before they become an application.

Public exports:
    register_primitive  - called by each module's register() function
    PRIMITIVE_ARITIES   - { name: (min, max) } for the Analyzer's static arity check
    PRIMITIVE_HELP      - { name: (kind, usage, doc, category) } for help
    CATEGORY_TITLES     - { category_key: display_title }
    CATEGORY_ORDER      - display order of categories in the help listing
    install_primitives  - bind every registered primitive into an env
"""

from pyscheme.AST import make_primitive
from pyscheme.Environment import SchemeArityError, arity_mismatch_msg


# Registration tables: populated by register_primitive() calls from each
# primitive module's register() function at package-import time.

_REGISTRY         = []   # list of (name, arity, fn_wrapped, usage, doc, kind, category)
PRIMITIVE_ARITIES = {}   # name -> (min, max)
PRIMITIVE_HELP    = {}   # name -> (kind, usage, doc, category)


def _default_usage(name, arity):
   """Auto-generate a usage string like '(cons a b)' from (min, max).

   Fixed arity uses single-letter names (a, b, c, ...) up through eight
   params; beyond that falls back to arg1, arg2, ...  Variadic uses
   '. args' for pure variadics or '. rest' following the fixed names.
   Optional arguments (finite hi > lo) appear in square brackets."""
   lo = arity[0]
   hi = arity[1]
   stock = 'abcdefgh'
   fixed = []
   i = 0
   while i < lo:
      if i < len(stock):
         fixed.append(stock[i])
      else:
         fixed.append('arg' + str(i + 1))
      i = i + 1
   if hi is None:
      if lo == 0:
         return '(' + name + ' . args)'
      return '(' + name + ' ' + ' '.join(fixed) + ' . rest)'
   if lo == hi:
      if lo == 0:
         return '(' + name + ')'
      return '(' + name + ' ' + ' '.join(fixed) + ')'
   parts = []
   i = 0
   while i < len(fixed):
      parts.append(fixed[i])
      i = i + 1
   i = lo
   while i < hi:
      parts.append('[arg' + str(i + 1) + ']')
      i = i + 1
   return '(' + name + ' ' + ' '.join(parts) + ')'


def _wrap_arity(name, arity, fn):
   """Wrap fn with an arity check.  Returns a (ctx, env, args, app_node)
   callable that raises SchemeArityError on argument count mismatch."""
   lo = arity[0]
   hi = arity[1]
   def checked(ctx, env, args, app_node):
      n = len(args)
      if n < lo:
         raise SchemeArityError(arity_mismatch_msg(name, lo, hi, n), app_node)
      if hi is not None and n > hi:
         raise SchemeArityError(arity_mismatch_msg(name, lo, hi, n), app_node)
      return fn(ctx, env, args, app_node)
   return checked


def register_primitive(name, arity, fn, usage=None, doc='', category='', kind='primitive'):
   """Register a primitive.  Called from each module's register() function.

   name      - Scheme-visible name bound into env
   arity     - (min, max) tuple; max=None for variadic
   fn        - Python callable with signature (ctx, env, args, app_node)
   usage     - usage string; auto-derived from name+arity if None
   doc       - documentation string (may be multi-line)
   category  - help grouping (e.g. 'arithmetic')
   kind      - 'primitive' (default) or 'special' for special-form stubs
   """
   if usage is None:
      usage = _default_usage(name, arity)
   wrapped = _wrap_arity(name, arity, fn)
   _REGISTRY.append((name, arity, wrapped, usage, doc, kind, category))
   PRIMITIVE_ARITIES[name] = arity
   PRIMITIVE_HELP[name]    = (kind, usage, doc, category)


# Category display titles and ordering.  Categories not named in the
# ordering list appear after these in alphabetical order.
CATEGORY_TITLES = {
   'control':     'Control',
   'lists':       'Lists',
   'arithmetic':  'Arithmetic',
   'comparison':  'Comparison',
   'predicates':  'Predicates',
   'equivalence': 'Equivalence',
   'logical':     'Logical',
   'meta':        'Meta',
   'help_sys':    'Help',
   'debug':       'Debugging',
}
CATEGORY_ORDER = [
   'control',
   'lists', 'arithmetic', 'comparison', 'predicates',
   'equivalence', 'logical', 'meta', 'ports',
   'strings', 'chars', 'vectors', 'bytevectors',
   'help_sys', 'debug',
]


# Import each primitive module (defines its functions) then call its
# register() to populate the tables.  Order here controls per-category
# listing order; CATEGORY_ORDER above controls cross-category ordering.

from pyscheme.primitives import control
from pyscheme.primitives import lists
from pyscheme.primitives import arithmetic
from pyscheme.primitives import comparison
from pyscheme.primitives import predicates
from pyscheme.primitives import equivalence
from pyscheme.primitives import logical
from pyscheme.primitives import meta
from pyscheme.primitives import ports
from pyscheme.primitives import strings
from pyscheme.primitives import chars
from pyscheme.primitives import vectors
from pyscheme.primitives import bytevectors
from pyscheme.primitives import help_sys
from pyscheme.primitives import debug

control.register()
lists.register()
arithmetic.register()
comparison.register()
predicates.register()
equivalence.register()
logical.register()
meta.register()
ports.register()
strings.register()
chars.register()
vectors.register()
bytevectors.register()
help_sys.register()
debug.register()


def install_primitives(env):
   """Bind every registered primitive into env as (PRIMITIVE, name, fn).

   Stubs (kind='special') are bound the same way as real primitives, so
   (help <form>) works without quoting.  Their bodies raise if ever
   actually called; the evaluator dispatches special forms before they
   become an application, so stubs only execute in edge cases like
   (apply if ...)."""
   i = 0
   while i < len(_REGISTRY):
      entry = _REGISTRY[i]
      env.bind(entry[0], make_primitive(entry[0], entry[2]))
      i = i + 1
