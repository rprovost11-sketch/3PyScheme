"""environment_test.py -- white-box unit tests for the lexical-addressing
scope classes in pyscheme/Environment.py.

Scheme is not involved: these drive Environment / LocalEnvironment /
GlobalEnvironment directly with LA keys, the way the Evaluator and the
by-name callers will after integration.  The last test is differential: it
builds random scope chains both ways and checks that resolve + lookup on
the new classes answers exactly as a by-name walk does.  The walk is a
small reference model (_RefScope) of today's pyscheme/Environment.py
lookup_id / set_id, so the test outlives the old class; before it was
written the same check passed against the old class itself.

Cross-port partner: 4CPPScheme2/undercarriage-tests/environment_test.cpp
(same test names, same shape as rat_test.cpp).

Run from the 3PyScheme directory:
    python undercarriage-tests/environment_test.py [--filter=SUBSTR]
Exit status 0 = all passed.
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyscheme.AST import intern_symbol, UNASSIGNED_VALUE
from pyscheme.Environment import (
    SchemeUnboundError, SchemeTypeError, _AliasCell, _display_name,
    GLOBAL_DEPTH, global_la_key, LocalEnvironment, GlobalEnvironment,
)

# -- Tiny test framework (same shape as the C++ undercarriage tests) ---------

_tests = []
_failures = [0]
_current = [None]


def TEST(fn):
    _tests.append(fn)
    return fn


def CHECK(cond, what=''):
    if not cond:
        line = sys._getframe(1).f_lineno
        sys.stderr.write('  FAIL [%s] line %d: %s\n' % (_current[0], line, what))
        _failures[0] = _failures[0] + 1


def CHECK_RAISES(exc_type, message, fn):
    """fn() must raise exc_type whose text is exactly message."""
    line = sys._getframe(1).f_lineno
    try:
        fn()
    except exc_type as e:
        if str(e) != message:
            sys.stderr.write('  FAIL [%s] line %d: message %r, expected %r\n'
                             % (_current[0], line, str(e), message))
            _failures[0] = _failures[0] + 1
        return
    except Exception as e:
        sys.stderr.write('  FAIL [%s] line %d: raised %s(%r), expected %s\n'
                         % (_current[0], line, type(e).__name__, str(e), exc_type.__name__))
        _failures[0] = _failures[0] + 1
        return
    sys.stderr.write('  FAIL [%s] line %d: nothing raised, expected %s\n'
                     % (_current[0], line, exc_type.__name__))
    _failures[0] = _failures[0] + 1


def S(name):
    return intern_symbol(name)


def G(name):
    return global_la_key(S(name))


# -- GlobalEnvironment -------------------------------------------------------

@TEST
def global_bind_and_lookup():
    g = GlobalEnvironment()
    CHECK(g.bind(G('x'), 10) == 10, 'bind returns the value')
    CHECK(g.lookup(G('x')) == 10)
    g.bind(G('x'), 11)
    CHECK(g.lookup(G('x')) == 11, 'bind overwrites')


@TEST
def global_ignores_depth_and_index():
    g = GlobalEnvironment()
    g.bind(G('x'), 10)
    CHECK(g.lookup((5, 7, S('x'))) == 10, 'a global scope reads only the name')


@TEST
def global_unbound_lookup_raises():
    g = GlobalEnvironment()
    CHECK_RAISES(SchemeUnboundError, 'unbound variable: nosuch',
                 lambda: g.lookup(G('nosuch')))


@TEST
def global_lookup_optional_unbound_is_none():
    g = GlobalEnvironment()
    CHECK(g.lookup_optional(G('nosuch')) is None)
    g.bind(G('x'), 0)
    CHECK(g.lookup_optional(G('x')) == 0, 'a falsy value is not "unbound"')


@TEST
def global_set():
    g = GlobalEnvironment()
    g.bind(G('x'), 1)
    CHECK(g.set(G('x'), 2) == 2, 'set returns the value')
    CHECK(g.lookup(G('x')) == 2)
    CHECK_RAISES(SchemeUnboundError, 'set! on unbound variable: nosuch',
                 lambda: g.set(G('nosuch'), 0))


@TEST
def global_unassigned_lookup_raises():
    g = GlobalEnvironment()
    g.bind(G('u'), UNASSIGNED_VALUE)
    CHECK_RAISES(SchemeUnboundError, 'variable used before its initialization: u',
                 lambda: g.lookup(G('u')))


@TEST
def global_child_falls_through_to_parent():
    root = GlobalEnvironment()
    root.bind(G('x'), 1)
    child = GlobalEnvironment(root)
    CHECK(child.getParent() is root)
    CHECK(child.getGlobalEnv() is root, 'a child dict scope caches the root')
    CHECK(child.lookup(G('x')) == 1, 'lookup falls through')
    CHECK(child.lookup_optional(G('x')) == 1, 'lookup_optional falls through')
    child.set(G('x'), 2)
    CHECK(root.lookup(G('x')) == 2, 'set falls through to the owner')
    child.bind(G('x'), 3)
    CHECK(child.lookup(G('x')) == 3 and root.lookup(G('x')) == 2, 'bind shadows')
    CHECK_RAISES(SchemeUnboundError, 'unbound variable: nosuch',
                 lambda: child.lookup(G('nosuch')))
    CHECK_RAISES(SchemeUnboundError, 'set! on unbound variable: nosuch',
                 lambda: child.set(G('nosuch'), 0))


@TEST
def global_freeze():
    g = GlobalEnvironment()
    g.bind(G('x'), 1)
    g.freeze()
    g.freeze()  # idempotent
    CHECK(g.lookup(G('x')) == 1, 'a frozen scope still reads')
    CHECK_RAISES(SchemeTypeError, "cannot define 'y' in a frozen environment",
                 lambda: g.bind(G('y'), 0))
    CHECK_RAISES(SchemeTypeError, "set! on 'x' in a frozen environment",
                 lambda: g.set(G('x'), 0))
    CHECK_RAISES(SchemeUnboundError, 'set! on unbound variable: nosuch',
                 lambda: g.set(G('nosuch'), 0))


@TEST
def global_child_of_frozen():
    frozen = GlobalEnvironment()
    frozen.bind(G('x'), 1)
    frozen.freeze()
    child = GlobalEnvironment(frozen)
    child.bind(G('y'), 2)
    CHECK(child.lookup(G('y')) == 2, 'a child of a frozen scope can bind')
    CHECK_RAISES(SchemeTypeError, "set! on 'x' in a frozen environment",
                 lambda: child.set(G('x'), 0))
    child.bind(G('x'), 5)
    child.set(G('x'), 6)
    CHECK(child.lookup(G('x')) == 6 and frozen.lookup(G('x')) == 1,
          'a shadow of a frozen binding is writable')


@TEST
def global_reroot_as_global():
    root = GlobalEnvironment()
    top = GlobalEnvironment(root)
    top.reroot_as_global()
    CHECK(top.getGlobalEnv() is top)
    CHECK(top.getParent() is root, 'the parent chain is kept')
    loc = LocalEnvironment(top, (), [])
    CHECK(loc.getGlobalEnv() is top, 'a scope built later caches the new root')


# -- Alias cells -------------------------------------------------------------

@TEST
def alias_reads_live_target():
    g = GlobalEnvironment()
    d = GlobalEnvironment()
    d.bind(G('target'), 1)
    g.register_alias(S('gs1'), S('target'), d, 99)
    CHECK(g.lookup(G('gs1')) == 1)
    d.set(G('target'), 2)
    CHECK(g.lookup(G('gs1')) == 2, 'later mutation of the target is seen')
    CHECK(g.lookup_optional(G('gs1')) == 2)


@TEST
def alias_writes_through():
    g = GlobalEnvironment()
    d = GlobalEnvironment()
    d.bind(G('target'), 1)
    g.register_alias(S('gs2'), S('target'), d, 99)
    CHECK(g.set(G('gs2'), 7) == 7)
    CHECK(d.lookup(G('target')) == 7, 'set! through the alias reaches the target')


@TEST
def alias_falls_back_to_copy():
    g = GlobalEnvironment()
    d = GlobalEnvironment()  # does not bind the target: a transient scope
    g.register_alias(S('gs3'), S('target'), d, 99)
    CHECK(g.lookup(G('gs3')) == 99, 'reads the def-time copy')
    g.set(G('gs3'), 100)
    CHECK(g.lookup(G('gs3')) == 100, 'a write lands in the copy')
    CHECK(d.lookup_optional(G('target')) is None, 'and not in def_env')


@TEST
def register_alias_binds_in_global_env():
    root = GlobalEnvironment()
    child = GlobalEnvironment(root)
    d = GlobalEnvironment()
    d.bind(G('target'), 1)
    child.register_alias(S('gs4'), S('target'), d, 0)
    seen = []
    root.for_each_binding(lambda sid, v: seen.append((sid, v)))
    CHECK(len(seen) == 1 and seen[0][0] == S('gs4'), 'the cell lives in the root')
    CHECK(type(seen[0][1]) is _AliasCell, 'for_each_binding passes the cell as stored')
    child.for_each_binding(lambda sid, v: CHECK(False, 'child holds nothing'))


# -- LocalEnvironment --------------------------------------------------------

def _chain():
    """root (x=G) <- outer (a, b) <- inner (c)"""
    root = GlobalEnvironment()
    root.bind(G('x'), 'gx')
    outer = LocalEnvironment(root, (S('a'), S('b')), ['va', 'vb'])
    inner = LocalEnvironment(outer, (S('c'),), ['vc'])
    return root, outer, inner


@TEST
def local_lookup_by_depth_and_index():
    root, outer, inner = _chain()
    CHECK(inner.lookup((0, 0, S('c'))) == 'vc')
    CHECK(inner.lookup((1, 0, S('a'))) == 'va')
    CHECK(inner.lookup((1, 1, S('b'))) == 'vb')
    CHECK(inner.lookup((1, 1, S('zzz'))) == 'vb', 'a local scope ignores the name')
    CHECK(inner.lookup_optional((1, 0, S('a'))) == 'va')


@TEST
def local_global_key_forwards_to_global():
    root, outer, inner = _chain()
    CHECK(inner.getGlobalEnv() is root)
    CHECK(inner.lookup(G('x')) == 'gx')
    CHECK(inner.lookup_optional(G('x')) == 'gx')
    CHECK(inner.lookup_optional(G('nosuch')) is None)
    CHECK_RAISES(SchemeUnboundError, 'unbound variable: nosuch',
                 lambda: inner.lookup(G('nosuch')))
    inner.set(G('x'), 'gx2')
    CHECK(root.lookup(G('x')) == 'gx2', 'a global set from a local scope')


@TEST
def local_set_writes_the_owner_only():
    root, outer, inner = _chain()
    CHECK(inner.set((1, 1, S('b')), 'vb2') == 'vb2')
    CHECK(outer.lookup((0, 1, S('b'))) == 'vb2')
    CHECK(inner.lookup((0, 0, S('c'))) == 'vc', 'inner slot 0 untouched')
    CHECK(outer.lookup((0, 0, S('a'))) == 'va', 'outer slot 0 untouched')


@TEST
def local_unassigned():
    root = GlobalEnvironment()
    loc = LocalEnvironment(root, (S('u'),), [UNASSIGNED_VALUE])
    CHECK_RAISES(SchemeUnboundError, 'variable used before its initialization: u',
                 lambda: loc.lookup((0, 0, S('u'))))
    # Same as today's Environment.lookup_optional_id: the marker is returned
    # as stored (see the open question in the report).
    CHECK(loc.lookup_optional((0, 0, S('u'))) is UNASSIGNED_VALUE)
    loc.set((0, 0, S('u')), 5)
    CHECK(loc.lookup((0, 0, S('u'))) == 5, 'letrec init clears it')


@TEST
def local_bind():
    root = GlobalEnvironment()
    loc = LocalEnvironment(root, (S('p'), S('q')), [1, UNASSIGNED_VALUE])
    CHECK(loc.bind((0, 1, S('q')), 2) == 2)
    CHECK(loc.lookup((0, 1, S('q'))) == 2)
    CHECK_RAISES(SchemeTypeError, "cannot define 'newname' in a local scope",
                 lambda: loc.bind(G('newname'), 0))
    CHECK(root.lookup_optional(G('newname')) is None, 'nothing leaked to the global')


@TEST
def local_bind_asserts_depth_zero():
    if not __debug__:
        return  # python -O strips the assert
    root = GlobalEnvironment()
    outer = LocalEnvironment(root, (S('p'),), [1])
    inner = LocalEnvironment(outer, (S('q'),), [2])
    CHECK_RAISES(AssertionError, 'bind is always into this scope',
                 lambda: inner.bind((1, 0, S('p')), 9))
    CHECK(inner.lookup((0, 0, S('q'))) == 2, 'the wrong slot was not written')


@TEST
def local_shared_names_independent_values():
    root = GlobalEnvironment()
    names = (S('n'),)
    one = LocalEnvironment(root, names, [1])
    two = LocalEnvironment(root, names, [2])
    one.set((0, 0, S('n')), 10)
    CHECK(two.lookup((0, 0, S('n'))) == 2, 'two calls of one lambda share names only')


@TEST
def resolve():
    root, outer, inner = _chain()
    CHECK(inner.resolve(S('c')) == (0, 0, S('c')))
    CHECK(inner.resolve(S('a')) == (1, 0, S('a')))
    CHECK(inner.resolve(S('b')) == (1, 1, S('b')))
    CHECK(inner.resolve(S('x')) == (GLOBAL_DEPTH, 0, S('x')), 'a global name')
    CHECK(inner.resolve(S('nosuch')) == (GLOBAL_DEPTH, 0, S('nosuch')),
          'an unbound name resolves as a global, as the Analyzer would emit')
    CHECK(root.resolve(S('a')) == global_la_key(S('a')))


@TEST
def resolve_innermost_shadow_wins():
    root = GlobalEnvironment()
    root.bind(G('v'), 'global')
    outer = LocalEnvironment(root, (S('v'),), ['outer'])
    inner = LocalEnvironment(outer, (S('w'), S('v')), ['w', 'inner'])
    k = inner.resolve(S('v'))
    CHECK(k == (0, 1, S('v')))
    CHECK(inner.lookup(k) == 'inner')
    CHECK(outer.lookup(outer.resolve(S('v'))) == 'outer')


@TEST
def for_each_binding():
    root, outer, inner = _chain()
    seen = []
    outer.for_each_binding(lambda sid, v: seen.append((sid, v)))
    CHECK(seen == [(S('a'), 'va'), (S('b'), 'vb')], 'local: in slot order')
    seen = []
    root.for_each_binding(lambda sid, v: seen.append((sid, v)))
    CHECK(seen == [(S('x'), 'gx')])
    empty = LocalEnvironment(root, (), [])
    empty.for_each_binding(lambda sid, v: CHECK(False, 'empty scope'))


@TEST
def local_under_child_dict_scope_finds_it():
    # A global LA key in a local scope goes to the nearest dict scope above
    # it, not straight to the root, so a dict scope BETWEEN the local and
    # the root (make-environment, an Expander macro scope) is consulted, as
    # a by-name walk would.
    root = GlobalEnvironment()
    root.bind(G('r'), 'in-root')
    macro_scope = GlobalEnvironment(root)
    macro_scope.bind(G('m'), 'in-macro-scope')
    outer = LocalEnvironment(macro_scope, (S('a'),), ['va'])
    loc = LocalEnvironment(outer, (), [])
    CHECK(loc.getGlobalEnv() is root, 'getGlobalEnv is still the root')
    CHECK(loc.lookup(loc.resolve(S('m'))) == 'in-macro-scope', 'found two scopes up')
    CHECK(loc.lookup(loc.resolve(S('r'))) == 'in-root', 'the root through it')
    loc.set(loc.resolve(S('m')), 'm2')
    CHECK(macro_scope.lookup(G('m')) == 'm2', 'set reaches it')
    ref_root = _RefScope(None)
    ref_macro = _RefScope(ref_root)
    ref_macro.bind(S('m'), 'in-macro-scope')
    CHECK(_RefScope(ref_macro).lookup_optional(S('m')) == 'in-macro-scope',
          'a by-name walk agrees')


@TEST
def make_environment_shape():
    # (define e (make-environment)) (eval '(define z 1) e)
    # (eval '((lambda () z)) e)  -- the lambda's call scope is under e.
    root = GlobalEnvironment()
    e = GlobalEnvironment(root)
    e.bind(G('z'), 1)
    call = LocalEnvironment(e, (), [])
    CHECK(call.lookup(call.resolve(S('z'))) == 1)
    CHECK(root.lookup_optional(G('z')) is None, 'z stayed in e')


# -- Differential: new classes vs a by-name walk -----------------------------

class _RefScope:
    """Reference model: today's Environment lookup_id / lookup_optional_id /
    set_id, a dict per scope walked by name."""

    def __init__(self, parent):
        self.parent = parent
        self.b = {}

    def bind(self, sid, v):
        self.b[sid] = v

    def _owner(self, sid):
        scope = self
        while scope is not None:
            if sid in scope.b:
                return scope
            scope = scope.parent
        return None

    def lookup(self, sid):
        scope = self._owner(sid)
        if scope is None:
            raise SchemeUnboundError('unbound variable: ' + _display_name(sid))
        if scope.b[sid] is UNASSIGNED_VALUE:
            raise SchemeUnboundError(
                'variable used before its initialization: ' + _display_name(sid))
        return scope.b[sid]

    def lookup_optional(self, sid):
        scope = self._owner(sid)
        return None if scope is None else scope.b[sid]

    def set(self, sid, v):
        scope = self._owner(sid)
        if scope is None:
            raise SchemeUnboundError('set! on unbound variable: ' + _display_name(sid))
        scope.b[sid] = v


@TEST
def differential_against_by_name_walk():
    rng = random.Random(20261028)
    pool = [S('d%d' % i) for i in range(12)]
    trials = 0
    while trials < 2000:
        trials = trials + 1
        old = _RefScope(None)
        new = GlobalEnvironment()
        for sid in rng.sample(pool, rng.randint(0, 6)):
            v = rng.randint(0, 999)
            old.bind(sid, v)
            new.bind(global_la_key(sid), v)
        if rng.randint(0, 1):
            # a dict scope under the root (make-environment, a macro scope)
            old = _RefScope(old)
            new = GlobalEnvironment(new)
            for sid in rng.sample(pool, rng.randint(0, 4)):
                v = rng.randint(0, 999)
                old.bind(sid, v)
                new.bind(global_la_key(sid), v)
        depth = rng.randint(0, 5)
        level = 0
        while level < depth:
            level = level + 1
            names = tuple(rng.sample(pool, rng.randint(0, 4)))
            values = [rng.choice([rng.randint(0, 999), UNASSIGNED_VALUE]) for _ in names]
            old = _RefScope(old)
            i = 0
            while i < len(names):
                old.bind(names[i], values[i])
                i = i + 1
            new = LocalEnvironment(new, names, values)
        for sid in pool:
            k = new.resolve(sid)
            o = _outcome(lambda: old.lookup(sid))
            n = _outcome(lambda: new.lookup(k))
            CHECK(o == n, 'lookup %s: old %r new %r' % (k, o, n))
            CHECK(old.lookup_optional(sid) == new.lookup_optional(k), 'lookup_optional')
            if o[0] == 'ok' or o[1].startswith('variable used'):
                w = rng.randint(1000, 1999)
                old.set(sid, w)
                new.set(k, w)
                CHECK(old.lookup(sid) == new.lookup(k) == w, 'set')


def _outcome(fn):
    try:
        return ('ok', fn())
    except SchemeUnboundError as e:
        return ('err', str(e))


def main():
    filt = None
    for arg in sys.argv[1:]:
        if arg.startswith('--filter='):
            filt = arg[len('--filter='):]
    print('Running %d environment tests%s...' % (len(_tests),
          (' filtered by ' + filt) if filt else ''))
    for fn in _tests:
        if filt and filt not in fn.__name__:
            continue
        _current[0] = fn.__name__
        before = _failures[0]
        try:
            fn()
        except Exception as e:
            sys.stderr.write('  FAIL [%s] uncaught %s: %s\n' % (fn.__name__, type(e).__name__, e))
            _failures[0] = _failures[0] + 1
        print('  [%s] %s' % ('ok' if _failures[0] == before else 'FAIL', fn.__name__))
    print('\n%d failure(s)' % _failures[0])
    return 0 if _failures[0] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
