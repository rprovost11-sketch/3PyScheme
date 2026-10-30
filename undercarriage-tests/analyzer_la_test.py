"""analyzer_la_test.py -- white-box unit tests for the Analyzer's lexical
addressing (pyscheme/Analyzer.py, "Lexical addressing").

Each test analyzes a small program and checks the LA address -- (depth,
index), or GLOBAL -- that every variable reference carries, in tree order.
The addresses must match the scopes the Evaluator builds at run time; that
is proved end to end by running the battery under SCHEME_LA_CHECK=1 (see
Environment.py _install_la_check).  These tests pin the frame model one
form at a time, and check the rewrite's other promises: the input tree is
never edited, unchanged subtrees are reused, a shared subtree gets each
scope's own keys, re-analysis is stable, scope_of_env reads a live chain,
and printers do not show the scope-names node.

Cross-port partner: 4CPPScheme2/undercarriage-tests/analyzer_la_test.cpp
(same test names).

Run from the 3PyScheme directory:
    python undercarriage-tests/analyzer_la_test.py [--filter=SUBSTR]
Exit status 0 = all passed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from pyscheme.AST import (
    is_cons, is_symbol, as_symbol, as_symbol_id, symbol_la_key, intern_symbol,
    is_scope_names, as_scope_names, gensym_display_name, make_symbol, alloc_cons,
    NIL_VALUE, symbol_name,
)
from pyscheme.Parser import parse_one
from pyscheme.Expander import expand
from pyscheme.Analyzer import analyze, scope_of_env
from pyscheme.Environment import GLOBAL_DEPTH, LocalEnvironment, GlobalEnvironment
from pyscheme.PrettyPrinter import pretty_print

# -- Tiny test framework (same shape as environment_test.py) -----------------

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


G = 'G'   # a global reference, in an expected-address list


def _addresses(tree):
    """(name, address) for every resolved symbol in tree, in tree order.
    address is (depth, index), or G for a global."""
    out = []

    def walk(x):
        if is_cons(x):
            walk(x.car)
            walk(x.cdr)
        elif is_symbol(x):
            k = symbol_la_key(x)
            if k is not None:
                name = gensym_display_name(as_symbol(x))
                out.append((name, G if k[0] == GLOBAL_DEPTH else (k[0], k[1])))
    walk(tree)
    return out


def _analyze(src):
    return analyze(expand(parse_one(src)))


def CHECK_ADDRESSES(src, expected):
    got = _addresses(_analyze(src))
    if got != expected:
        line = sys._getframe(1).f_lineno
        sys.stderr.write('  FAIL [%s] line %d: %s\n      got      %r\n      expected %r\n'
                         % (_current[0], line, src, got, expected))
        _failures[0] = _failures[0] + 1


def _scope_names_node(form):
    """The scope-names node of an analyzed let-family form, as tuples of names."""
    node = form.cdr.car
    if not is_scope_names(node):
        return None
    return tuple(tuple(gensym_display_name(symbol_name(sid)) for sid in scope)
                 for scope in as_scope_names(node))


# -- Tests ---------------------------------------------------------------------

@TEST
def top_level_reference_is_global():
    CHECK_ADDRESSES('(car x)', [('car', G), ('x', G)])


@TEST
def lambda_params_then_rest():
    CHECK_ADDRESSES('(lambda (a b . r) (list r b a))',
                    [('list', G), ('r', (0, 2)), ('b', (0, 1)), ('a', (0, 0))])
    CHECK_ADDRESSES('(lambda args args)', [('args', (0, 0))])


@TEST
def nested_lambda_counts_depth():
    CHECK_ADDRESSES('(lambda (a) (lambda (b) (lambda () (a b))))',
                    [('a', (2, 0)), ('b', (1, 0))])


@TEST
def case_lambda_clause_is_one_scope():
    CHECK_ADDRESSES('(case-lambda ((x) x) ((x y . z) z))',
                    [('x', (0, 0)), ('z', (0, 2))])


@TEST
def let_inits_outside_body_inside():
    CHECK_ADDRESSES('(lambda (x) (let ((y x) (z 1)) (list x y z)))',
                    [('x', (0, 0)),
                     ('list', G), ('x', (1, 0)), ('y', (0, 0)), ('z', (0, 1))])


@TEST
def empty_let_builds_no_scope():
    CHECK_ADDRESSES('(lambda (x) (let () x))', [('x', (0, 0))])
    CHECK_ADDRESSES('(lambda (x) (let* () x))', [('x', (0, 0))])
    CHECK_ADDRESSES('(lambda (x) (letrec () x))', [('x', (0, 0))])


@TEST
def let_star_one_scope_per_binding():
    CHECK_ADDRESSES('(lambda (x) (let* ((a x) (b a) (c b)) (list a b c x)))',
                    [('x', (0, 0)), ('a', (0, 0)), ('b', (0, 0)),
                     ('list', G), ('a', (2, 0)), ('b', (1, 0)), ('c', (0, 0)),
                     ('x', (3, 0))])


@TEST
def letrec_inits_inside():
    CHECK_ADDRESSES('(letrec ((f (lambda () g)) (g 1)) (f))',
                    [('g', (1, 1)), ('f', (0, 0))])


@TEST
def named_let_inits_run_in_the_loop_scope():
    # The Evaluator runs the inits inside the scope that holds the loop name,
    # so n is one scope further out.  They still cannot see the loop name
    # (R7RS 4.2.4): the Expander renames the binding, so the init's `loop`
    # is another identifier and resolves outside -- here, to a global.
    CHECK_ADDRESSES('(lambda (n) (let loop ((i n) (acc loop)) (loop i acc)))',
                    [('n', (1, 0)), ('loop', G),
                     ('loop', (1, 0)), ('i', (0, 0)), ('acc', (0, 1))])


@TEST
def delay_runs_in_an_empty_scope():
    CHECK_ADDRESSES('(lambda (x) (delay x))', [('x', (1, 0))])
    CHECK_ADDRESSES('(lambda (x) (delay-force x))', [('x', (1, 0))])


@TEST
def set_target_is_resolved():
    CHECK_ADDRESSES('(lambda (x) (set! x 1))', [('x', (0, 0))])
    CHECK_ADDRESSES('(lambda (x) (set! y x))', [('y', G), ('x', (0, 0))])


@TEST
def define_name_stays_plain():
    tree = _analyze('(define f (lambda (x) x))')
    CHECK(symbol_la_key(tree.cdr.car) is None, 'define name resolved')
    CHECK(_addresses(tree) == [('x', (0, 0))], repr(_addresses(tree)))


@TEST
def quote_and_case_datums_stay_plain():
    CHECK_ADDRESSES("(lambda (x) '(x y))", [])
    CHECK_ADDRESSES('(lambda (x) (case x ((x) x) (else x)))',
                    [('x', (0, 0)), ('x', (0, 0)), ('x', (0, 0))])


@TEST
def cond_arrow_and_else():
    CHECK_ADDRESSES('(lambda (x f) (cond (x => f) (else x)))',
                    [('x', (0, 0)), ('f', (0, 1)), ('x', (0, 0))])


@TEST
def scope_names_node_holds_the_scopes():
    CHECK(_scope_names_node(_analyze('(let ((a 1) (b 2)) a)')) == (('a', 'b'),),
          'let')
    CHECK(_scope_names_node(_analyze('(let* ((a 1) (b 2)) a)')) == (('a',), ('b',)),
          'let*')
    CHECK(_scope_names_node(_analyze('(letrec ((a 1)) a)')) == (('a',),), 'letrec')
    CHECK(_scope_names_node(_analyze('(let lp ((a 1)) a)')) == (('lp',), ('a',)),
          'named let')
    CHECK(_scope_names_node(_analyze('(let () 1)')) == (), 'empty let')


@TEST
def input_tree_is_not_edited():
    src = '(lambda (x) (let ((y x)) (if y (quote (a b)) x)))'
    tree = expand(parse_one(src))
    before = pretty_print(tree)
    analyze(tree)
    CHECK(pretty_print(tree) == before, 'printed form changed')
    CHECK(_addresses(tree) == [], 'input carries keys: %r' % _addresses(tree))


@TEST
def unchanged_subtree_is_reused():
    tree = expand(parse_one("(lambda (x) (quote (a b)) x)"))
    out = analyze(tree)
    quote_in = tree.cdr.cdr.car
    quote_out = out.cdr.cdr.car
    CHECK(quote_out is quote_in, 'quote form copied')
    CHECK(out is not tree, 'changed form not copied')
    CHECK(out.src is tree.src, 'src lost')


@TEST
def shared_subtree_gets_each_scopes_keys():
    # One cons (x), placed by hand in two lambdas whose x sits in different
    # slots -- the way macro expansion can share a subtree.
    x = make_symbol('x')
    shared = alloc_cons(x, NIL_VALUE)
    lam1 = parse_one('(lambda (x) 0)')
    lam2 = parse_one('(lambda (y x) 0)')
    lam1.cdr.cdr = alloc_cons(shared, NIL_VALUE)
    lam2.cdr.cdr = alloc_cons(shared, NIL_VALUE)
    prog = alloc_cons(make_symbol('list'),
                      alloc_cons(lam1, alloc_cons(lam2, NIL_VALUE)))
    got = _addresses(analyze(prog))
    CHECK(got == [('list', G), ('x', (0, 0)), ('x', (0, 1))], repr(got))
    CHECK(symbol_la_key(shared.car) is None, 'shared subtree edited')


@TEST
def reanalysis_is_stable():
    src = '(lambda (n) (let loop ((i n)) (let* ((a i)) (letrec ((b a)) (loop b)))))'
    once = _analyze(src)
    twice = analyze(once)
    CHECK(_addresses(twice) == _addresses(once),
          '%r != %r' % (_addresses(twice), _addresses(once)))
    CHECK(pretty_print(twice) == pretty_print(once), 'printed form changed')


@TEST
def scope_of_env_reads_the_live_chain():
    g = GlobalEnvironment()
    a, b, c = intern_symbol('a'), intern_symbol('b'), intern_symbol('c')
    outer = LocalEnvironment(g, (a, b), [1, 2])
    inner = LocalEnvironment(outer, (c,), [3])
    CHECK(scope_of_env(g) is None, 'dict scope')
    scope = scope_of_env(inner)
    CHECK(scope == ((c,), ((a, b), None)), repr(scope))
    got = _addresses(analyze(expand(parse_one('(list a b c d)')), None, scope))
    CHECK(got == [('list', G), ('a', (1, 0)), ('b', (1, 1)), ('c', (0, 0)), ('d', G)],
          repr(got))


@TEST
def printers_skip_the_scope_names_node():
    for src in ['(let ((a 1)) a)', '(let* ((a 1) (b a)) b)',
                '(letrec ((a 1)) a)', '(let lp ((a 1)) a)', '(let () 1)']:
        tree = expand(parse_one(src))
        CHECK(pretty_print(analyze(tree)) == pretty_print(tree), src)


def main():
    filt = None
    for arg in sys.argv[1:]:
        if arg.startswith('--filter='):
            filt = arg[len('--filter='):]
    print('Running %d analyzer LA tests%s...' % (len(_tests),
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
