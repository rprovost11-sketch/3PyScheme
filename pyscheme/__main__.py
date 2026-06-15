"""Entry point for the Scheme interpreter.

Run with:
    python -m pyscheme                    # interactive REPL in CWD
    python -m pyscheme <directory>        # REPL rooted at <directory>
    python -m pyscheme <file.scm>         # evaluate the file, then exit
    python -m pyscheme -e <expr>          # evaluate <expr> like a REPL line,
                                          # print its result, then exit

-e/--evaluate <expr> takes one expression (repeatable); each is evaluated as
a full REPL transcript -- the input is echoed (`>>> ` on the first line, `... `
on continuations), side-effecting output appears in place, then the value is
shown as `==> <value>` -- and the process exits.  Cannot be combined with a
file/directory target.

Library search path options (may precede the file/directory):
    -L <dir;dir;...>  / --library-path <dir;...>   add directories (one
                       argument, split on the OS path separator) to the
                       front of the library search path
    -I <dir>          add a single directory; repeatable

Both prepend to the SCHEME_LIBRARY_PATH environment variable; the current
directory is searched first.  The combined list seeds the global
current-library-path parameter.
"""
import os
import signal
import sys

from pyscheme import __version__
from pyscheme.Interpreter import Interpreter
from pyscheme.Listener import Listener


_USAGE = ('Usage: python -m pyscheme [-L <dir%s...>] [-I <dir>]... '
          '[-e <expr>]... [<directory> | <scheme-source-file>]' % os.pathsep)


def _parse_args(argv):
    """Split argv (without argv[0]) into (library_paths, target, eval_exprs).

    -L/--library-path takes one OS-pathsep-separated list; -I takes one
    directory and may repeat; both contribute to library_paths in
    command-line order.  -e/--evaluate takes one expression string and may
    repeat; each is evaluated as if typed at the REPL, then the process
    exits.  eval_exprs is None when no -e was given (so the REPL/target path
    runs), otherwise the list of expressions in command-line order.  At most
    one positional target (file or directory) is allowed, and a target may
    not be combined with -e.  Exits with status 2 on a malformed option."""
    library_paths = []
    target = None
    eval_exprs = None

    def _fail(msg):
        print('pyscheme: ' + msg, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(2)

    def _add_list(val):
        for part in val.split(os.pathsep):
            if part:
                library_paths.append(part)

    def _add_eval(expr):
        nonlocal eval_exprs
        if eval_exprs is None:
            eval_exprs = []
        eval_exprs.append(expr)

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '-L' or a == '--library-path':
            if i + 1 >= len(argv):
                _fail('option ' + a + ' requires an argument')
            _add_list(argv[i + 1])
            i += 2
        elif a.startswith('-L='):
            _add_list(a[3:])
            i += 1
        elif a.startswith('--library-path='):
            _add_list(a[len('--library-path='):])
            i += 1
        elif a == '-I':
            if i + 1 >= len(argv):
                _fail('option -I requires an argument')
            if argv[i + 1]:
                library_paths.append(argv[i + 1])
            i += 2
        elif a.startswith('-I='):
            if a[3:]:
                library_paths.append(a[3:])
            i += 1
        elif a == '-e' or a == '--evaluate':
            if i + 1 >= len(argv):
                _fail('option ' + a + ' requires an argument')
            _add_eval(argv[i + 1])
            i += 2
        elif a.startswith('-e='):
            _add_eval(a[3:])
            i += 1
        elif a.startswith('--evaluate='):
            _add_eval(a[len('--evaluate='):])
            i += 1
        elif a == '-' or not a.startswith('-'):
            if target is not None:
                _fail('unexpected extra argument: ' + a)
            target = a
            i += 1
        else:
            _fail('unknown option: ' + a)

    if eval_exprs is not None and target is not None:
        _fail('-e/--evaluate cannot be combined with a file or directory')

    return library_paths, target, eval_exprs


def main():
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    # R7RS source and program output are Unicode.  On Windows, stdout/stderr
    # default to the console code page (cp1252), which cannot encode non-Latin-1
    # characters, so (display "λ") raises.  Force UTF-8 on both.
    # line_buffering=True also flushes on every newline even when stdout is a
    # pipe/file (Python block-buffers those by default), so progress from a
    # long-running program is visible as it runs instead of only at exit.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', line_buffering=True)
        except (AttributeError, ValueError):
            pass

    library_paths, target, eval_exprs = _parse_args(sys.argv[1:])

    interp = Interpreter(library_paths=library_paths)

    # -e/--evaluate: evaluate each expression as a REPL transcript, then exit.
    # The banner is suppressed so the first line is the '>>> ' echo.
    if eval_exprs is not None:
        listener = _build_listener(interp, show_banner=False)
        sys.exit(listener.eval_and_exit(eval_exprs))

    if target is not None:
        # A directory: chdir there and drop to the REPL.
        if os.path.isdir(target):
            os.chdir(target)
            # fall through to the REPL block below
        elif os.path.isfile(target):
            try:
                interp.evalFile(target)
            except KeyboardInterrupt:
                print('pyscheme: interrupted', file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print('pyscheme: ' + str(e), file=sys.stderr)
                sys.exit(1)
            return
        else:
            print('pyscheme: no such file or directory: ' + target,
                  file=sys.stderr)
            sys.exit(1)

    listener = _build_listener(interp)
    try:
        listener.readEvalPrintLoop()
    except StopIteration:
        pass


def _build_listener(interp, show_banner=True):
    """Construct the Listener wired to the scheme-tests suite directories."""
    _scheme_tests = os.path.join(
        os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
        'scheme-tests')
    # The .log REPL-transcript suites live under scheme-tests/log-tests/ (they
    # were grouped there to distinguish them from the application-tests suites).
    _log_tests = os.path.join(_scheme_tests, 'log-tests')
    return Listener(
        interp,
        testdir=os.path.join(_log_tests, 'feature-tests'),
        language='pyscheme',
        version=__version__,
        author='Ron Provost/Longo',
        project='https://github.com/rprovost11/pyscheme',
        compliancedir=os.path.join(_log_tests, 'R7RS-Compliance-Tests'),
        regressiondir=os.path.join(_log_tests, 'regression-tests'),
        runsdir=os.path.join(_scheme_tests, 'runs'),
        show_banner=show_banner,
    )


if __name__ == '__main__':
    main()
