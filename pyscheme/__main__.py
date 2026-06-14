"""Entry point for the Scheme interpreter.

Run with:
    python -m pyscheme                    # interactive REPL in CWD
    python -m pyscheme <directory>        # REPL rooted at <directory>
    python -m pyscheme <file.scm>         # evaluate the file, then exit

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
          '[<directory> | <scheme-source-file>]' % os.pathsep)


def _parse_args(argv):
    """Split argv (without argv[0]) into (library_paths, target).

    -L/--library-path takes one OS-pathsep-separated list; -I takes one
    directory and may repeat; both contribute to library_paths in
    command-line order.  At most one positional target (file or directory)
    is allowed.  Exits with status 2 on a malformed option."""
    library_paths = []
    target = None

    def _fail(msg):
        print('pyscheme: ' + msg, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        sys.exit(2)

    def _add_list(val):
        for part in val.split(os.pathsep):
            if part:
                library_paths.append(part)

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
        elif a == '-' or not a.startswith('-'):
            if target is not None:
                _fail('unexpected extra argument: ' + a)
            target = a
            i += 1
        else:
            _fail('unknown option: ' + a)

    return library_paths, target


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

    library_paths, target = _parse_args(sys.argv[1:])

    interp = Interpreter(library_paths=library_paths)

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

    _scheme_tests = os.path.join(
        os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
        'scheme-tests')
    listener = Listener(
        interp,
        testdir=os.path.join(_scheme_tests, 'feature-tests'),
        language='pyscheme',
        version=__version__,
        author='Ron Provost/Longo',
        project='https://github.com/rprovost11/pyscheme',
        compliancedir=os.path.join(_scheme_tests, 'R7RS-Compliance-Tests'),
        regressiondir=os.path.join(_scheme_tests, 'regression-tests'),
        runsdir=os.path.join(_scheme_tests, 'runs'),
    )
    try:
        listener.readEvalPrintLoop()
    except StopIteration:
        pass


if __name__ == '__main__':
    main()
