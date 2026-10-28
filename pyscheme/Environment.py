"""Environment - Lisp lexical environment with variable binding.

Also holds the Scheme runtime-error hierarchy.  The errors live here
(not in Evaluator) because Environment.lookup / Environment.set need
to raise SchemeUnboundError directly, and Evaluator already imports
from Environment - putting the errors here avoids a circular import.

The scope classes, for lexical addressing:

  Environment       abstract base: parent pointer, cached global pointer,
                    and the virtual interface
  LocalEnvironment  procedure call, let, let* step, letrec, named let:
                    values in a list
  GlobalEnvironment global / library, and the dict scopes chained under
                    them (Expander macro scopes, (environment ...),
                    make-environment): a name -> value dict

Every lookup / set / bind takes the same argument, an LA key (lexical
addressing key): a tuple (depth, index, name) built by the Analyzer:

  depth  hops up the chain to the owning local scope; -1 = a global
  index  slot within that local scope
  name   symbol id

LocalEnvironment reads only depth and index; GlobalEnvironment reads only
name.  A global LA key (depth -1) met by a local scope is passed to the
nearest dict scope above it, which falls through by name to its parents,
so the Evaluator calls E.lookup(la_key) uniformly.

By-name callers (the Expander, macro alias cells, aux-keyword checks,
library / help / environment code) build a global LA key with
global_la_key(sid): they only ever reach dict scopes, or -- for the
aux-keyword check -- a name alpha-renaming guarantees no local carries.
A name met in code running in a scope becomes an LA key with
E.resolve(sid), which answers exactly as the Analyzer would for code in
that scope; the debugger uses it, and so does the Evaluator until the
Analyzer emits LA keys itself.
"""

from pyscheme.AST import (
    SourceInfo, ConsCell, format_with_caret,
    make_error_object, make_file_error_object,
    intern_symbol, symbol_name, gensym_display_name, UNASSIGNED_VALUE,
)


# --- Non-error control escape ------------------------------------------
# ReplExit unwinds an interactive evaluation back to the REPL top level
# when (exit) is called from the prompt rather than from a batch file.  It
# is deliberately a BaseException -- NOT a Scheme error -- so guard /
# with-exception-handler (which only catch Exception subclasses) never
# intercept it.  Batch-mode (exit) still calls sys.exit(); the mode is read
# from Context.interactive.  See primitives/meta.py:_prim_exit.

class ReplExit(BaseException):
    """Raised by (exit [code]) in interactive mode to abort the current
    evaluation and return to the '>>> ' prompt.  Carries the requested exit
    code for completeness (it is ignored at the REPL)."""

    def __init__(self, code=0):
        self.code = code
        super().__init__('repl exit')


# --- Scheme runtime error hierarchy ------------------------------------
# Python exceptions with a fixed hierarchy; map to setjmp/longjmp in C
# and to try/catch in C++.  Every subclass carries optional SourceInfo
# so diagnostics can include a line, column, and caret.

class _PositionedSchemeError(Exception):
    """Base for runtime errors that may carry a source position.
    Accepts: SourceInfo, ConsCell (uses .src), atom tagged tuple
    (uses last element if it is a SourceInfo), or None."""

    def __init__(self, msg, source=None):
        self.msg = msg
        self.src = _extract_src(source)
        self.call_stack = None
        super().__init__(msg)

    def __str__(self):
        return format_with_caret(self.msg, self.src)


def _extract_src(source):
    if source is None:
        return None
    if isinstance(source, SourceInfo):
        return source
    if isinstance(source, ConsCell):
        return source.src
    if isinstance(source, tuple) and len(source) > 0:
        last = source[-1]
        if isinstance(last, SourceInfo):
            return last
    return None


class SchemeArityError(_PositionedSchemeError):
    """Raised when a function is called with the wrong number of arguments."""
    pass


class SchemeUnboundError(_PositionedSchemeError):
    """Raised on variable reference or set! targeting an unbound name."""
    pass


class SchemeTypeError(_PositionedSchemeError):
    """Raised by primitives when an argument is the wrong type."""
    pass


class SchemeRaised(_PositionedSchemeError):
    """Exception raised by (raise obj) or (raise-continuable obj).
    Marker catchable by with-exception-handler.  Carries the raised Scheme
    value and a continuable flag."""

    def __init__(self, value, src=None, continuable=False):
        # Late import so Environment stays independent of PrettyPrinter at
        # module-load time.
        from pyscheme.PrettyPrinter import pretty_print
        _PositionedSchemeError.__init__(self, pretty_print(value), src)
        self.value = value
        self.continuable = continuable


class SchemeRuntimeError(_PositionedSchemeError):
    """Raised when a Python-level runtime error (e.g. RecursionError) occurs
    inside the interpreter pipeline.  Carries no source position."""
    pass


class SchemeFileError(SchemeRaised):
    """Raised by file I/O primitives when an OS-level error occurs (file not
    found, permission denied, etc.).  Value is a file-kind ErrorObject so
    (file-error? obj) returns #t when caught by with-exception-handler."""

    def __init__(self, message, src=None):
        error_obj = make_file_error_object(message, [])
        _PositionedSchemeError.__init__(self, message, src)
        self.value = error_obj
        self.continuable = False


class SchemeUserError(SchemeRaised):
    """Raised by the `error` primitive (R7RS 6.11).  Value is an ErrorObject
    carrying the message string and irritants list."""

    def __init__(self, message, irritants, src=None):
        from pyscheme.PrettyPrinter import pretty_print
        parts = [message]
        i = 0
        while i < len(irritants):
            parts.append(pretty_print(irritants[i]))
            i = i + 1
        display = ' '.join(parts)
        error_obj = make_error_object(message, irritants)
        # Bypass SchemeRaised.__init__ to avoid re-formatting; we build the
        # display string ourselves because it depends on irritants.
        _PositionedSchemeError.__init__(self, display, src)
        self.value = error_obj
        self.continuable = False


def arity_mismatch_msg(name, lo, hi, n_provided):
    """Build 'N argument(s) provided; M expected.' style text.
    name='' omits the leading 'name: ' prefix (used for immediate-lambda
    applications that have no name to report).
    hi is None     -> 'at least {lo} expected'
    hi == lo       -> '{lo} expected'
    hi == lo + 1   -> '{lo} or {hi} expected'
    else           -> '{lo} to {hi} expected'"""
    if n_provided == 1:
        provided = '1 argument provided'
    else:
        provided = str(n_provided) + ' arguments provided'
    if hi is None:
        expected = 'at least ' + str(lo) + ' expected'
    elif hi == lo:
        expected = str(lo) + ' expected'
    elif hi == lo + 1:
        expected = str(lo) + ' or ' + str(hi) + ' expected'
    else:
        expected = str(lo) + ' to ' + str(hi) + ' expected'
    if name:
        return name + ': ' + provided + '; ' + expected
    return provided + '; ' + expected


# --- Environment -------------------------------------------------------
# Binding storage: _bindings maps name -> value (plain dict).
# Alpha-renaming in the Expander ensures distinct bindings have distinct names,
# so a simple name -> value map is sufficient for resolution.

def _display_name(sid: int) -> str:
    """Gensym-stripped name for error messages (see AST.gensym_display_name)."""
    return gensym_display_name(symbol_name(sid))


class _AliasCell:
    """A macro free-identifier alias, stored as the *value* of a gensym binding.

    A syntax-rules template's free reference to a binding that existed at macro
    definition time is emitted as a fresh gensym (so a same-named use-site
    binding cannot capture it -- hygiene).  That gensym is bound to one of these
    instead of to a copy of the referent's value: resolving it prefers the LIVE
    def-site binding (target in def_env), so set! through the macro writes
    through and later mutations are seen (the A5 hygiene bug a value copy had);
    if def_env no longer resolves target at eval time -- it was a transient
    body-scan scope, as for library-internal helpers -- it falls back to `copy`,
    the def-time snapshot that keeps such references reachable.

    Stored as a binding's value (not in a side table) so it rides through the
    library export/import machinery, which copies bindings as stored, with no
    special handling:
    `lookup` resolves it to a plain value, so an exported macro's alias is
    carried as the def-time value exactly as before."""

    __slots__ = ('target', 'def_env', 'copy')

    def __init__(self, target: int, def_env, copy):
        self.target = target
        self.def_env = def_env
        self.copy = copy


GLOBAL_DEPTH = -1


def global_la_key(sid: int):
    """The LA key of a global variable: what the Analyzer emits for a name
    no local scope binds, and what by-name callers use."""
    return (GLOBAL_DEPTH, 0, sid)


def _unassigned_error(sid: int):
    return SchemeUnboundError(
        'variable used before its initialization: ' + _display_name(sid))


def _alias_read(cell):
    """Live value of an alias cell's target in its def_env (always a dict
    scope), else the def-time copy."""
    v = cell.def_env.lookup_optional(global_la_key(cell.target))
    return v if v is not None else cell.copy


def _alias_write(cell, value):
    la_key = global_la_key(cell.target)
    if cell.def_env.lookup_optional(la_key) is not None:
        cell.def_env.set(la_key, value)
    else:
        cell.copy = value


class Environment:
    """Abstract base of the two scope classes.  The virtual interface takes
    the same arguments in both subclasses:

      lookup(la_key)            value of the variable la_key names; raises
                                SchemeUnboundError if it is unbound
      lookup_optional(la_key)   that value, or None if it is unbound
      set(la_key, value)        update that variable
      bind(la_key, value)       create or overwrite it in THIS scope
      resolve(sid, depth=0)     the LA key the Analyzer would give sid in
                                code running in this scope (depth counts
                                the local scopes already passed)
      for_each_binding(fn)      call fn(sid, value) for each binding in
                                THIS scope; values are passed as stored
                                (an alias cell as the cell)"""

    def __init__(self, parent):
        self._parent = parent
        self._global_env = self if parent is None else parent._global_env
        # The nearest dict scope at or above this one, where a local scope
        # looks up a global LA key.  It is the root global env unless code
        # runs in a dict scope chained under the root (make-environment, an
        # Expander macro scope): a variable defined there must still be found.
        self._dict_env = self if parent is None else parent._dict_env

    def getGlobalEnv(self):
        return self._global_env

    def getParent(self):
        return self._parent

    def lookup(self, la_key):
        raise NotImplementedError

    def lookup_optional(self, la_key):
        raise NotImplementedError

    def set(self, la_key, value):
        raise NotImplementedError

    def bind(self, la_key, value):
        raise NotImplementedError

    def resolve(self, sid: int, depth: int = 0):
        raise NotImplementedError

    def for_each_binding(self, fn):
        raise NotImplementedError


class LocalEnvironment(Environment):
    """A local scope: one per procedure call, let, let* step, letrec and
    named let.  Its values sit in a list at the indexes the Analyzer
    assigned; _names is the Analyzer's tuple of symbol ids for the scope
    (static, shared, index-aligned with _values), used only by resolve and
    for_each_binding.  A local scope always has a parent, has a fixed size,
    is never frozen, and never holds an alias cell (those live in the
    global env)."""

    def __init__(self, parent, names, values):
        Environment.__init__(self, parent)
        self._names = names
        self._values = values

    def _owner(self, depth):
        scope = self
        while depth:
            scope = scope._parent
            depth = depth - 1
        return scope

    def lookup(self, la_key):
        depth, index, name = la_key
        if depth == GLOBAL_DEPTH:
            return self._dict_env.lookup(la_key)
        v = self._owner(depth)._values[index]
        if v is UNASSIGNED_VALUE:
            raise _unassigned_error(name)
        return v

    def lookup_optional(self, la_key):
        depth, index, name = la_key
        if depth == GLOBAL_DEPTH:
            return self._dict_env.lookup_optional(la_key)
        return self._owner(depth)._values[index]

    def set(self, la_key, value):
        depth, index, name = la_key
        if depth == GLOBAL_DEPTH:
            return self._dict_env.set(la_key, value)
        self._owner(depth)._values[index] = value
        return value

    def bind(self, la_key, value):
        """Store into a slot of this scope.  A global LA key here can only
        come from a define typed at a debugger prompt paused in this scope:
        a local scope has no room for a new name, so that is an error."""
        depth, index, name = la_key
        if depth == GLOBAL_DEPTH:
            raise SchemeTypeError(
                "cannot define '" + _display_name(name) + "' in a local scope")
        assert depth == 0, 'bind is always into this scope'
        self._values[index] = value
        return value

    def resolve(self, sid: int, depth: int = 0):
        names = self._names
        if sid in names:
            return (depth, names.index(sid), sid)
        return self._parent.resolve(sid, depth + 1)

    def for_each_binding(self, fn):
        i = 0
        while i < len(self._names):
            fn(self._names[i], self._values[i])
            i = i + 1


class GlobalEnvironment(Environment):
    """Global / library scope: bindings in a name -> value dict.  The root
    of every runtime chain.  Also used with a parent for the Expander's
    compile-time macro scopes and (environment ...) isolation scopes; that
    parent is always another GlobalEnvironment, and a name this scope does
    not hold is looked up there."""

    def __init__(self, parent=None):
        Environment.__init__(self, parent)
        self._dict_env = self
        self._bindings = {}
        self._is_immutable = False

    def lookup(self, la_key):
        depth, index, name = la_key
        if name in self._bindings:
            v = self._bindings[name]
            if v is UNASSIGNED_VALUE:
                raise _unassigned_error(name)
            return _alias_read(v) if type(v) is _AliasCell else v
        if self._parent is not None:
            return self._parent.lookup(la_key)
        raise SchemeUnboundError('unbound variable: ' + _display_name(name))

    def lookup_optional(self, la_key):
        depth, index, name = la_key
        if name in self._bindings:
            v = self._bindings[name]
            return _alias_read(v) if type(v) is _AliasCell else v
        if self._parent is not None:
            return self._parent.lookup_optional(la_key)
        return None

    def set(self, la_key, value):
        depth, index, name = la_key
        if name in self._bindings:
            if self._is_immutable:
                raise SchemeTypeError(
                    "set! on '" + _display_name(name) + "' in a frozen environment")
            v = self._bindings[name]
            if type(v) is _AliasCell:
                _alias_write(v, value)
            else:
                self._bindings[name] = value
            return value
        if self._parent is not None:
            return self._parent.set(la_key, value)
        raise SchemeUnboundError(
            'set! on unbound variable: ' + _display_name(name))

    def bind(self, la_key, value):
        depth, index, name = la_key
        if self._is_immutable:
            raise SchemeTypeError(
                "cannot define '" + _display_name(name) + "' in a frozen environment")
        self._bindings[name] = value
        return value

    def resolve(self, sid: int, depth: int = 0):
        return global_la_key(sid)

    def for_each_binding(self, fn):
        for name in self._bindings:
            fn(name, self._bindings[name])

    def freeze(self):
        """Mark this environment immutable: subsequent bind / set on a binding
        it owns raises SchemeTypeError.  Used for library export tables and
        for environments returned by R7RS (environment ...).  Idempotent."""
        self._is_immutable = True

    def reroot_as_global(self):
        """Make this scope its own global root while keeping its parent chain
        for lookups (make-toplevel-environment)."""
        self._global_env = self

    def register_alias(self, gs: int, target: int, def_env, copy_value):
        """Bind gs (a macro free-identifier gensym) in the global env to an
        alias cell indirecting to target in def_env."""
        self._global_env.bind(global_la_key(gs),
                              _AliasCell(target, def_env, copy_value))


# --- Chain-walk histogram (lexical-addressing feasibility probe) -------
# Set PYSCHEME_CHAIN_HIST=<file> to record, for every successful lookup /
# set, how many parent hops a by-name walk would take and whether the hit
# lands in the global env.  The report is appended to <file> at exit.  When
# the variable is unset the plain methods above are untouched (zero cost).

def _install_chain_histogram(out_path):
    import atexit
    from collections import Counter

    hist = {'lookup': Counter(), 'set': Counter()}
    unbound = Counter()
    busy = [False]  # count the outermost call only, not its forwards

    def walk(scope, la_key, kind):
        depth = la_key[0]
        if depth != GLOBAL_DEPTH:
            hist[kind][(depth, False)] += 1
            return
        hops = 0
        while type(scope) is LocalEnvironment:
            scope = scope._parent
            hops = hops + 1
        while scope is not None:
            if la_key[2] in scope._bindings:
                hist[kind][(hops, scope is scope._global_env)] += 1
                return
            scope = scope._parent
            hops = hops + 1
        unbound[kind] += 1

    def wrap(orig, kind):
        def probe(self, la_key, *value):
            if busy[0]:
                return orig(self, la_key, *value)
            busy[0] = True
            try:
                walk(self, la_key, kind)
                return orig(self, la_key, *value)
            finally:
                busy[0] = False
        return probe

    for cls in (LocalEnvironment, GlobalEnvironment):
        cls.lookup = wrap(cls.lookup, 'lookup')
        cls.set = wrap(cls.set, 'set')

    def report():
        lines = []
        for kind in ('lookup', 'set'):
            c = hist[kind]
            total = sum(c.values())
            lines.append('== %s: %d hits, %d unbound' % (kind, total, unbound[kind]))
            if not total:
                continue
            lines.append('   hops  local        global       cum%')
            cum = 0
            for h in range(max(k[0] for k in c) + 1):
                loc = c[(h, False)]
                glb = c[(h, True)]
                if not (loc or glb):
                    continue
                cum = cum + loc + glb
                lines.append('   %4d  %-11d  %-11d  %5.1f' % (h, loc, glb, 100.0 * cum / total))
            n_glb = sum(v for k, v in c.items() if k[1])
            wasted = sum(k[0] * v for k, v in c.items())
            lines.append('   global hits: %.1f%%   mean hops: %.2f   global mean hops: %.2f'
                         % (100.0 * n_glb / total, wasted / total,
                            sum(k[0] * v for k, v in c.items() if k[1]) / max(n_glb, 1)))
        with open(out_path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    atexit.register(report)


import os as _os
if _os.environ.get('PYSCHEME_CHAIN_HIST'):
    _install_chain_histogram(_os.environ['PYSCHEME_CHAIN_HIST'])


# --- Module self-test --------------------------------------------------

if __name__ == '__main__':
    n_pass = 0
    n_fail = 0

    def check(label, cond):
        global n_pass, n_fail
        if cond:
            print('[ OK ] ' + label)
            n_pass = n_pass + 1
        else:
            print('[FAIL] ' + label)
            n_fail = n_fail + 1

    # The scope classes are tested in undercarriage-tests/environment_test.py.

    # SchemeUnboundError caller can mutate src after construction.
    try:
        GlobalEnvironment().lookup(global_la_key(intern_symbol('nothere')))
    except SchemeUnboundError as e:
        e.src = SourceInfo(2, 5, 'line', '<test>')
        check('e.src mutation',         e.src.line == 2 and e.src.col == 5)
        check('__str__ with src',
              str(e) == '"<test>" line 2, col 5: unbound variable: nothere\nline\n    ^')

    # _extract_src dispatch.
    si = SourceInfo(1, 1, '', None)
    check('extract_src of None',       _extract_src(None) is None)
    check('extract_src of SourceInfo', _extract_src(si) is si)
    check('extract_src of ConsCell',   _extract_src(ConsCell(1, 2, si)) is si)
    check('extract_src of atom',       _extract_src((100, 'x', si)) is si)
    check('extract_src of non-src tuple', _extract_src((0,)) is None)

    # Error hierarchy.
    check('SchemeArityError subclass',   issubclass(
        SchemeArityError,   _PositionedSchemeError))
    check('SchemeUnboundError subclass', issubclass(
        SchemeUnboundError, _PositionedSchemeError))
    check('SchemeTypeError subclass',    issubclass(
        SchemeTypeError,    _PositionedSchemeError))
    check('SchemeRaised subclass',       issubclass(
        SchemeRaised,       _PositionedSchemeError))
    check('SchemeUserError subclass of Raised',
          issubclass(SchemeUserError, SchemeRaised))

    # arity_mismatch_msg formatting.
    check('arity lo==hi',               arity_mismatch_msg(
        'f', 2, 2, 1) == 'f: 1 argument provided; 2 expected')
    check('arity lo+1',                 arity_mismatch_msg('f', 2, 3, 4)
          == 'f: 4 arguments provided; 2 or 3 expected')
    check('arity range',                arity_mismatch_msg(
        'f', 1, 4, 5) == 'f: 5 arguments provided; 1 to 4 expected')
    check('arity unbounded',            arity_mismatch_msg(
        'f', 2, None, 1) == 'f: 1 argument provided; at least 2 expected')
    check('arity no name',              arity_mismatch_msg(
        '', 1, 1, 0) == '0 arguments provided; 1 expected')

    print()
    print('%d passed, %d failed' % (n_pass, n_fail))
