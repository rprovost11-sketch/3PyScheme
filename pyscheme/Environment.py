"""Environment - Lisp lexical environment with variable binding.

Also holds the Scheme runtime-error hierarchy.  The errors live here
(not in Evaluator) because Environment.lookup / Environment.set need
to raise SchemeUnboundError directly, and Evaluator already imports
from Environment - putting the errors here avoids a circular import.
"""

from pyscheme.AST import (
    SourceInfo, ConsCell, format_with_caret,
    make_error_object, make_file_error_object,
    intern_symbol, symbol_name,
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

_GENSYM_PFX = '\x01h.'


def _display_name(sid: int) -> str:
    """Strip hygiene gensym prefix for error messages.  \x01h.BASE.DIGITS -> BASE."""
    name = symbol_name(sid)
    if not name.startswith(_GENSYM_PFX):
        return name
    rest = name[len(_GENSYM_PFX):]
    dot = rest.rfind('.')
    if dot >= 0 and rest[dot + 1:].isdigit():
        return rest[:dot]
    return rest


class Environment:
    """Lexical environment: a binding table plus a parent pointer.
    The global scope is the root of the parent chain and every
    child caches a pointer to it for fast global lookups."""

    def __init__(self, parent=None, initialBindings=None):
        self._bindings = {}
        if initialBindings is not None:
            for k in initialBindings:
                self._bindings[intern_symbol(k)] = initialBindings[k]
        self._parent = parent
        self._is_immutable = False
        if parent is None:
            self._global_env = self
        else:
            self._global_env = parent._global_env

    def bind(self, key: str, value):
        sid = intern_symbol(key)
        if self._is_immutable:
            raise SchemeTypeError(
                "cannot define '" + _display_name(sid) + "' in a frozen environment")
        self._bindings[sid] = value
        return value

    def bind_id(self, sid: int, value):
        if self._is_immutable:
            raise SchemeTypeError(
                "cannot define '" + _display_name(sid) + "' in a frozen environment")
        self._bindings[sid] = value
        return value

    def freeze(self):
        """Mark this environment immutable: subsequent bind / set on a binding
        it owns raises SchemeTypeError.  Used for library export tables and
        for environments returned by R7RS (environment ...).  Idempotent."""
        self._is_immutable = True

    def getGlobalEnv(self):
        return self._global_env

    def lookup(self, key: str):
        """Walk the parent chain; return the value of the first binding found.
        Raises SchemeUnboundError if no binding is found."""
        sid = intern_symbol(key)
        scope = self
        while scope:
            if sid in scope._bindings:
                return scope._bindings[sid]
            scope = scope._parent
        raise SchemeUnboundError('unbound variable: ' + _display_name(sid))

    def lookup_id(self, sid: int):
        scope = self
        while scope:
            if sid in scope._bindings:
                return scope._bindings[sid]
            scope = scope._parent
        raise SchemeUnboundError('unbound variable: ' + _display_name(sid))

    def lookup_optional(self, key: str):
        """Walk the parent chain; return value or None if not found."""
        sid = intern_symbol(key)
        scope = self
        while scope:
            if sid in scope._bindings:
                return scope._bindings[sid]
            scope = scope._parent
        return None

    def lookup_optional_id(self, sid: int):
        """Walk the parent chain; return value or None if not found."""
        scope = self
        while scope:
            if sid in scope._bindings:
                return scope._bindings[sid]
            scope = scope._parent
        return None

    def set(self, key: str, value):
        """Update the nearest binding of key.  Raises SchemeUnboundError if
        no binding is found; raises SchemeTypeError if the owning scope is frozen."""
        sid = intern_symbol(key)
        scope = self
        while scope:
            if sid in scope._bindings:
                if scope._is_immutable:
                    raise SchemeTypeError(
                        "set! on '" + _display_name(sid) + "' in a frozen environment")
                scope._bindings[sid] = value
                return value
            scope = scope._parent
        raise SchemeUnboundError(
            'set! on unbound variable: ' + _display_name(sid))

    def set_id(self, sid: int, value):
        scope = self
        while scope:
            if sid in scope._bindings:
                if scope._is_immutable:
                    raise SchemeTypeError(
                        "set! on '" + _display_name(sid) + "' in a frozen environment")
                scope._bindings[sid] = value
                return value
            scope = scope._parent
        raise SchemeUnboundError(
            'set! on unbound variable: ' + _display_name(sid))


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

    # Basic construction.
    root = Environment()
    check('root has no parent',        root._parent is None)
    check('root is its own global',    root.getGlobalEnv() is root)
    check('empty bindings',            root._bindings == {})

    # Initial bindings.
    e0 = Environment(initialBindings={'a': 1, 'b': 2})
    check('initial bind a',            e0.lookup('a') == 1)
    check('initial bind b',            e0.lookup('b') == 2)

    # bind and lookup.
    e0.bind('c', 3)
    check('bind returns value',        e0.bind('d', 4) == 4)
    check('lookup after bind',         e0.lookup('c') == 3)
    check('lookup d',                  e0.lookup('d') == 4)

    # Child scope lookup walks up.
    child = Environment(parent=e0)
    check('child parent is e0',        child._parent is e0)
    check('child global is root-of-e0', child.getGlobalEnv() is e0)
    check('child lookup finds parent', child.lookup('a') == 1)

    # Child shadowing.
    child.bind('a', 100)
    check('child shadows a',           child.lookup('a') == 100)
    check('parent a unchanged',        e0.lookup('a') == 1)

    # set walks up and updates nearest binding.
    child.set('b', 222)
    check('set updates parent b',      e0.lookup('b') == 222)
    check('child has no b binding',    intern_symbol('b') not in child._bindings)

    # Rebind same key overwrites.
    e0.bind('x', 'first')
    e0.bind('x', 'second')
    check('rebind overwrites',         e0.lookup('x') == 'second')

    # Unbound lookup.
    try:
        root.lookup('missing')
        check('lookup missing raises',  False)
    except SchemeUnboundError as e:
        check('lookup missing raises',  True)
        check('lookup missing msg',     e.msg == 'unbound variable: missing')
        check('lookup missing src None', e.src is None)

    # Unbound set.
    try:
        root.set('missing', 42)
        check('set missing raises',     False)
    except SchemeUnboundError as e:
        check('set missing raises',     True)
        check('set missing msg',        e.msg ==
              'set! on unbound variable: missing')

    # Freeze: bind and set raise on a frozen env.
    frozen = Environment()
    frozen.bind('a', 1)
    check('frozen flag default False', frozen._is_immutable is False)
    frozen.freeze()
    check('freeze sets flag',          frozen._is_immutable is True)
    try:
        frozen.bind('b', 2)
        check('bind on frozen raises',  False)
    except SchemeTypeError as e:
        check('bind on frozen raises',  True)
        check('bind frozen msg',        "cannot define 'b'" in e.msg)
    try:
        frozen.set('a', 99)
        check('set on frozen raises',   False)
    except SchemeTypeError as e:
        check('set on frozen raises',   True)
        check('set frozen msg',         "set! on 'a'" in e.msg)
    check('frozen value unchanged',    frozen.lookup('a') == 1)
    # set on a child scope updating a frozen-parent binding also raises.
    child_of_frozen = Environment(parent=frozen)
    try:
        child_of_frozen.set('a', 99)
        check('child set walking to frozen raises', False)
    except SchemeTypeError:
        check('child set walking to frozen raises', True)
    # set on a child's own (unfrozen) binding still works.
    child_of_frozen.bind('z', 7)
    child_of_frozen.set('z', 8)
    check('child unfrozen set works', child_of_frozen.lookup('z') == 8)
    # freeze is idempotent.
    frozen.freeze()
    check('freeze idempotent',         frozen._is_immutable is True)

    # SchemeUnboundError caller can mutate src after construction.
    try:
        child.lookup('nothere')
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
