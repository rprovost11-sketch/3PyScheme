"""Scheme REPL listener.

Ported from pythonslisp/Listener.py.  Features:

- Read-eval-print loop with multi-line input (parens balance before submit).
- Super-bracket: a line ending with ']' closes all currently open parens
  (unless it starts a listener command).
- Listener commands prefixed with ']'.  Dispatch goes through an
  explicit command-table dict; the method's docstring is the help text.
- Readline integration for history navigation (up/down) and persistent
  history.  On Windows the project-local readline_win shim is used
  (msvcrt-based, since the stdlib readline is unavailable).  On Unix /
  WSL the stdlib readline module is used.  History persists in
  ~/.pyscheme_history.
- Auto-indent continuation lines: the '... ' prompt is prefilled with
  3 spaces per unclosed paren depth.
- Session logging (dribble): `]log file` writes every prompt, output,
  return, and error into `file`.  `]close` stops.  `]resume file` reopens
  an existing log for append and replays it (restoring interpreter state).
- Log playback: `]readlog file [v]` replays a log without comparison.
- Source loading: `]readsrc file` (alias `]load`).
- Testing: `]feature [file]` runs a single log file or every log under the
  testing directory.  Output is compared verbatim with the log's
  expectation.  Full runs produce a timestamped report in testing/runs/.
- Color output when stdout is a TTY.

Interpreter contract: the Listener expects an object satisfying
InterpreterBase - eval, evalFile, and reboot methods taking an optional
outStrm.  The existing Interpreter class meets this contract.
"""

import atexit
import datetime
import io
import os
import sys
import time

from pyscheme.Environment import (
    SchemeUnboundError, SchemeRuntimeError,
    SchemeArityError, SchemeTypeError, SchemeRaised,
    ReplExit,
)
from pyscheme.Parser import SchemeSyntaxError
from pyscheme.Analyzer import SchemeAnalysisError
from pyscheme.Utils import columnize, retrieveFileList, writeln_multiFile, paren_state
import pyscheme.Expander as _expander_mod


# Suite subdirectories relative to the scheme-tests root.  Hardcoded for now;
# a configuration file is the eventual home for these.  The root itself is NOT
# hardcoded -- it comes from -T/--scheme-tests, $SCHEME_TESTS_DIR, or the
# ]scheme-tests command (see Listener._set_scheme_tests_dir).
_FEATURE_SUBDIR = os.path.join('log-tests', 'feature-tests')
_COMPLIANCE_SUBDIR = os.path.join('log-tests', 'R7RS-Compliance-Tests')
_REGRESSION_SUBDIR = os.path.join('log-tests', 'regression-tests')
_RUNS_SUBDIR = 'runs'
_HIST_FILE = os.path.expanduser('~/.pyscheme_history')

# Default value the test runner binds to %MAX_TCO_ITER_COUNT% before each test
# file (after the per-file reboot).  Compliance test 3.05 reads it to size its
# proper-tail-recursion soak loops; ]compliance -I:<count> overrides it.  Kept
# modest so routine runs stay fast -- a big count is a per-machine memory soak,
# not a portable TCO proof (3.05 proves TCO with %continuation-depth instead).
_TCO_ITER_DEFAULT = 100000
_TCO_ITER_VAR = '%MAX_TCO_ITER_COUNT%'


# ANSI SGR escapes for the listener's colorized output, defined in one place.
# The raw escape strings appear only here; _ansi() (and Listener._colors())
# hand them out -- or '' when color is off -- so call sites never repeat them.
_ANSI_CODES = {
    'bold':       '\033[1;97m',   # bold white
    'bold_green': '\033[1;92m',
    'green':      '\033[92m',
    'red':        '\033[91m',
    'dim':        '\033[2m',
    'cyan':       '\033[96m',
    'reset':      '\033[0m',
}


def _ansi(on, *names):
    """Return the ANSI escapes named in *names (keys of _ANSI_CODES), or '' for
    each when `on` is false.  Lets a call site write
        BOLD, GREEN, RESET = _ansi(color, 'bold', 'green', 'reset')
    instead of repeating an if/else escape block."""
    if not on:
        return ('',) * len(names)
    return tuple(_ANSI_CODES[n] for n in names)


def _substring(s, start, end):
    """Return s[start:end] as an explicit char-by-char copy.  Ports to
    strncpy in C, std::string::substr in C++."""
    result = ''
    i = start
    while i < end:
        result = result + s[i]
        i = i + 1
    return result


# ---- InterpreterBase --------------------------------------------------


class InterpreterBase:
    """Interface the Listener expects its interpreter object to provide.
    In the C++ port, these become pure virtual methods."""

    def reboot(self, outStrm=None):
        """Reset to a fresh global environment."""
        raise NotImplementedError

    def eval(self, source, outStrm=None):
        """Evaluate a source string.  Returns a pretty-printed result."""
        raise NotImplementedError

    def evalFile(self, filename, outStrm=None):
        """Read and evaluate an entire source file."""
        raise NotImplementedError

    def set_debug_input_fn(self, fn, rl=None):
        """Register the Listener's prompt function for debug REPLs.  No-op stub."""
        pass


class ListenerCommandError(Exception):
    """Raised by listener-command bodies to signal a user-level error."""
    pass


class TestResult:
    """Return container for sessionLog_test: pass/fail counts.  POD."""

    def __init__(self, n_pass, n_fail):
        self.n_pass = n_pass
        self.n_fail = n_fail


# ---- Listener ---------------------------------------------------------


class Listener:
    """Interactive REPL with session logging and log-based testing.

    Partly modelled on Python's cmd module.  Each `]foo bar baz` input
    dispatches through self._commands to the _cmd_foo method; the
    method's docstring is shown by `]help foo`."""

    # Readline state - class-level so history is shared across listeners
    # in the same process.
    _rl = None
    _historyMax = 500

    # ---- static helpers ----

    @staticmethod
    def _format_call_stack(call_stack):
        """Render a shadow call-stack list as a backtrace string.
        Each entry is [label, src, count]."""
        from pyscheme.AST import format_with_caret
        lines = []
        i = 0
        while i < len(call_stack):
            entry = call_stack[i]
            label = entry[0]
            src = entry[1]
            count = entry[2]
            if count > 1:
                label = label + ' [x' + str(count) + ']'
            lines.append('  at ' + format_with_caret(label, src))
            i = i + 1
        return '\n'.join(lines)

    @staticmethod
    def _format_error(exc):
        """Produce the user-visible text for an exception.  Same text at
        REPL and test harness so expected/actual error strings compare."""
        if isinstance(exc, MemoryError):
            return 'out of memory'
        if isinstance(exc, NotImplementedError):
            return 'Not implemented: ' + str(exc)
        if (isinstance(exc, SchemeSyntaxError) or isinstance(exc, SchemeAnalysisError)
            or isinstance(exc, SchemeUnboundError) or isinstance(exc, SchemeRuntimeError)
                or isinstance(exc, ListenerCommandError)):
            return str(exc)
        if (isinstance(exc, SchemeArityError) or isinstance(exc, SchemeTypeError)
                or isinstance(exc, SchemeRaised)):
            msg = type(exc).__name__ + ': ' + str(exc)
            call_stack = exc.call_stack
            if call_stack:
                msg = msg + '\n' + Listener._format_call_stack(call_stack)
            return msg
        msg = str(exc)
        if msg:
            return 'internal error: ' + msg
        return 'internal error'

    @staticmethod
    def _compute_indent(lines):
        """Return whitespace to auto-indent the next continuation line.
        Indents 3 spaces per unclosed paren depth."""
        depth = 0
        in_string = False
        escape = False
        i = 0
        while i < len(lines):
            line = lines[i]
            j = 0
            while j < len(line):
                ch = line[j]
                if escape:
                    escape = False
                elif in_string:
                    if ch == '\\':
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == ';':
                        break
                    elif ch == '(' or ch == '[':
                        depth = depth + 1
                    elif ch == ')' or ch == ']':
                        if depth > 0:
                            depth = depth - 1
                j = j + 1
            i = i + 1
        indent = ''
        k = 0
        total = depth * 3
        while k < total:
            indent = indent + ' '
            k = k + 1
        return indent

    @staticmethod
    def _parse_log(text):
        """Parse a session log into a list of 5-tuples (expr, output, retval, error, fold_case).

        Each entry begins with a `>>> ` line.  Continuation lines (`... `)
        belong to the same expression.  Lines between the expression and
        `==> ` (with no marker) are output (from display/print/help).  A
        `==> ` line gives the return value; a `%%% ` line gives the error
        message.  Comment lines starting with `;;` outside an entry are
        skipped."""
        lines = text.splitlines(keepends=True)
        entries = []
        idx = 0
        n = len(lines)
        fold_case = False
        while idx < n:
            while idx < n and not lines[idx].startswith('>>> '):
                stripped = lines[idx].rstrip()
                if stripped == '#!fold-case':
                    fold_case = True
                elif stripped == '#!no-fold-case':
                    fold_case = False
                idx = idx + 1
            if idx >= n:
                break
            entry_fold_case = fold_case
            expr = _substring(lines[idx], 4, len(lines[idx]))
            output = ''
            retval = ''
            error = ''
            idx = idx + 1
            while idx < n and lines[idx].startswith('... '):
                expr = expr + _substring(lines[idx], 4, len(lines[idx]))
                idx = idx + 1
            if idx < n and lines[idx].rstrip() == '...' and not lines[idx].startswith('... '):
                idx = idx + 1
            while idx < n:
                line = lines[idx]
                if line.startswith('==> ') or line.rstrip() == '==>':
                    break
                if line.startswith('... ') or line.startswith('>>> ') or line.startswith('%%% '):
                    break
                output = output + line
                idx = idx + 1
            if idx < n and (lines[idx].startswith('==> ') or lines[idx].rstrip() == '==>'):
                line = lines[idx]
                if len(line) > 4:
                    retval = _substring(line, 4, len(line))
                idx = idx + 1
                while idx < n:
                    line = lines[idx]
                    if line.startswith('==> ') or line.rstrip() == '==>':
                        break
                    if line.startswith('... ') or line.startswith('>>> ') or line.startswith('%%% '):
                        break
                    if line.startswith('#!'):
                        break  # fold-case directive
                    if line.startswith(';'):
                        expr = expr + line
                    else:
                        retval = retval + line
                    idx = idx + 1
            if idx < n and lines[idx].startswith('%%% '):
                error = _substring(lines[idx], 4, len(lines[idx]))
                idx = idx + 1
                while idx < n and lines[idx].startswith('%%% '):
                    error = error + _substring(lines[idx], 4, len(lines[idx]))
                    idx = idx + 1
            if expr:
                entries.append(
                    (expr, output.rstrip(), retval.rstrip(), error.rstrip(), entry_fold_case))
        return entries

    @staticmethod
    def _match_retval(actual, expected):
        """True if actual matches expected, honouring 'X or ==> Y' alternatives."""
        if ' or ==> ' in expected:
            parts = expected.split(' or ==> ')
            i = 0
            while i < len(parts):
                if actual == parts[i].strip():
                    return True
                i = i + 1
            return False
        return actual == expected

    @staticmethod
    def _print_welcome_banner(use_color):
        """Short welcome banner printed by _banner and ]reboot."""
        BOLD_GREEN, CYAN, RESET = _ansi(use_color, 'bold_green', 'cyan', 'reset')
        print('Enter any expression to have it evaluated by the interpreter.')
        print("Evaluate '" + CYAN + '(help)' + RESET + "' for online help.")
        print("Type  '" + CYAN + ']help' + RESET +
              "' to list Listener commands.")
        print(BOLD_GREEN + 'Welcome!' + RESET)

    def __init__(self, anInterpreter,
                 language='pyscheme', version='0.1',
                 author='pyscheme authors',
                 project='https://example/pyscheme',
                 scheme_tests_dir=None,
                 scheme_tests_source='unset',
                 show_banner=True):
        self._interp = anInterpreter
        # A live Listener means an interactive REPL session: (exit) should abort
        # to the prompt, not terminate the process (batch evalFile never builds a
        # Listener, so its (exit) still exits).  See primitives/meta.py:_prim_exit.
        self._interp.get_ctx().interactive = True
        # The scheme-tests root is not hardcoded -- it is supplied (or left
        # unset) by the caller, who resolved -T/--scheme-tests vs $SCHEME_TESTS_DIR.
        # The per-suite subdirectories are derived from it; ]scheme-tests can
        # change it at runtime.  When unset, the test commands explain how to set it.
        self._scheme_tests_dir = None
        self._scheme_tests_source = 'unset'
        self._testdir = ''
        self._compliancedir = ''
        self._regressiondir = ''
        self._runsdir = ''
        self._set_scheme_tests_dir(scheme_tests_dir, scheme_tests_source)
        self._logFile = None
        self._language = language
        self._version = version
        self._author = author
        self._project = project
        # When True, ANSI color escape codes are emitted even though stdout is
        # not a TTY -- e.g. when the REPL is driven through a pipe by a GUI
        # front-end (cherry) that renders the codes itself.  Toggled with
        # ]toggle-tty-color; queried with ]tty-color.  Default off, so piped
        # output stays plain unless a front-end opts in.
        self._emit_color_codes = False
        # True while a test run is redirecting sys.stdout to a .run report file.
        # Forces color OFF even when _emit_color_codes is set, so report files
        # stay clean text (mirrors cppscheme2's _output_to_file).
        self._output_to_file = False
        # When ]suites runs several suites, it opens ONE shared .run report and
        # parks the handle here; each _runTestFiles appends its section instead
        # of opening (and closing) its own file.  None for individual commands.
        self._shared_run_file = None
        self._shared_run_filename = ''
        self._init_readline()
        # Wire the Listener's prompt function into the interpreter's debugger
        # so debug> prompts use the same readline session as the REPL.
        self._interp.set_debug_input_fn(self._prompt, Listener._rl)
        # Command dispatch table (replaces dir()/getattr() reflection).
        self._commands = {
            'help':     self._cmd_help,
            'quit':     self._cmd_quit,
            'exit':     self._cmd_exit,
            'reboot':   self._cmd_reboot,
            'readsrc':  self._cmd_readsrc,
            'load':     self._cmd_load,
            'readlog':  self._cmd_readlog,
            'log':      self._cmd_log,
            'close':    self._cmd_close,
            'resume':   self._cmd_resume,
            'feature':    self._cmd_feature,
            'compliance': self._cmd_compliance,
            'regression': self._cmd_regression,
            'suites':     self._cmd_suites,
            'scheme-tests': self._cmd_scheme_tests,
            'cd':         self._cmd_cd,
            'pwd':      self._cmd_pwd,
            'lhistory': self._cmd_lhistory,
            'debug':    self._cmd_debug,
            'toggle-tty-color': self._cmd_toggle_tty_color,
            'tty-color':        self._cmd_tty_color,
        }
        # The startup banner is interactive-REPL chrome.  -e/--evaluate builds
        # the Listener only to reuse its REPL transcript formatting, so it
        # suppresses the banner (the first line should be the '>>> ' echo).
        if show_banner:
            self._banner()

    # ---- readline setup ----

    def _init_readline(self):
        """Attempt to load a readline module (Windows shim on win32, stdlib
        readline elsewhere) and wire up persistent history."""
        if Listener._rl is not None:
            return
        if sys.platform == 'win32':
            try:
                from pyscheme import readline_win as _rl_mod
                Listener._rl = _rl_mod
                try:
                    Listener._rl.read_history_file(_HIST_FILE)
                except FileNotFoundError:
                    pass
                Listener._rl.set_history_length(Listener._historyMax)
                atexit.register(Listener._rl.write_history_file, _HIST_FILE)
            except ImportError:
                pass
        else:
            try:
                import readline as _rl_mod
                Listener._rl = _rl_mod
                try:
                    Listener._rl.read_history_file(_HIST_FILE)
                except FileNotFoundError:
                    pass
                Listener._rl.set_history_length(Listener._historyMax)
                Listener._rl.set_auto_history(False)
                atexit.register(Listener._rl.write_history_file, _HIST_FILE)
            except ImportError:
                pass

    # ---- I/O helpers ----

    def _use_color(self):
        return (self._emit_color_codes or sys.stdout.isatty()) and not self._output_to_file

    def _colors(self, *names):
        """Listener shorthand for _ansi(self._use_color(), *names)."""
        return _ansi(self._use_color(), *names)

    def _banner(self):
        BOLD_WHITE, DIM, RESET = self._colors('bold', 'dim', 'reset')
        print(BOLD_WHITE + self._language + ' ' + self._version
              + ' by ' + self._author + RESET)
        print(DIM + 'Project home ' + self._project + RESET)
        print()
        print(DIM + '- Interpreter Initialized' + RESET, flush=True)
        print(DIM + '- Listener Initialized' + RESET, flush=True)
        print()
        Listener._print_welcome_banner(self._use_color())
        print()

    def _writeLn(self, value='', file=None, flush=False):
        """Print value (with newline) to `file` (or stdout) and mirror into
        the dribble log if one is open."""
        if self._logFile:
            writeln_multiFile(value, [file, self._logFile], flush=flush)
        else:
            writeln_multiFile(value, [file], flush=flush)

    def _writeResult(self, text):
        """Render an evaluation result with the `==>` prefix and mirror to
        any active log file."""
        if text is None:
            text = ''
        color = self._use_color()
        GREEN, BOLD, RESET = self._colors('green', 'bold', 'reset')
        lines = text.splitlines()
        if not lines:
            lines = ['']
        i = 0
        while i < len(lines):
            line = lines[i]
            plain = '==> ' + line
            colorLn = GREEN + '==>' + RESET + ' ' + BOLD + line + RESET
            if color:
                self._writeLn(colorLn, file=None, flush=True)
            else:
                self._writeLn(plain, file=None, flush=True)
            i = i + 1

    def _writeErrorMsg(self, errMsg):
        """Render an error with the `%%% ` prefix and mirror to the log."""
        color = self._use_color()
        RED, RESET = self._colors('red', 'reset')
        lines = errMsg.splitlines()
        if not lines:
            lines = [errMsg]
        i = 0
        while i < len(lines):
            line = lines[i]
            plain = '%%% ' + line
            if color:
                self._writeLn(RED + plain + RESET, file=None, flush=True)
            else:
                self._writeLn(plain, file=None, flush=True)
            i = i + 1

    def _prompt(self, prompt='', prefill=''):
        """Read one line of user input at `prompt`.  When a readline module
        is available, `prefill` pre-populates the line (editable) - on
        Windows via readline_win.input_line, on Unix via the startup-hook
        mechanism."""
        if sys.platform == 'win32' and self._rl and sys.stdin.isatty():
            return self._rl.input_line(prompt,
                                       continuation_prompt='... ',
                                       prefill=prefill).rstrip()
        if prefill and self._rl and sys.platform != 'win32':
            def _insert_prefill():
                self._rl.insert_text(prefill)
            self._rl.set_startup_hook(_insert_prefill)
            try:
                return input(prompt).rstrip()
            finally:
                self._rl.set_startup_hook(None)
        return input(prompt).rstrip()

    # ---- main loop ----

    def readEvalPrintLoop(self):
        """Run the REPL until EOF or a ]quit/]exit listener command."""
        inputExprLineList = []

        while True:
            try:
                if not inputExprLineList:
                    lineInput = self._prompt('>>> ')
                else:
                    indent = Listener._compute_indent(inputExprLineList)
                    lineInput = self._prompt('... ', prefill=indent)
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                inputExprLineList = []
                continue

            submit = False
            if lineInput == '':
                if inputExprLineList:
                    submit = True
            else:
                # Super-bracket: trailing ']' closes all open parens, unless
                # the line itself starts a listener command.
                if lineInput.endswith(']') and not (
                        lineInput.startswith(']') and len(lineInput) > 1):
                    tentative = _substring(lineInput, 0, len(lineInput) - 1)
                    if tentative:
                        combined = '\n'.join(inputExprLineList + [tentative])
                    else:
                        combined = '\n'.join(inputExprLineList)
                    ps = paren_state(combined)
                    innermost_is_bracket = len(
                        ps.stack) > 0 and ps.stack[len(ps.stack) - 1] == '['
                    if ps.depth > 0 and not ps.in_string and not innermost_is_bracket:
                        lineInput = tentative + ')' * ps.depth
                    elif lineInput == ']' and ps.depth == 0 and not ps.in_string:
                        continue
                # Mirror (possibly expanded) line into the dribble log,
                # as long as it isn't a listener command.
                if self._logFile and lineInput and lineInput[0] != ']':
                    if not inputExprLineList:
                        logPrompt = '>>> '
                    else:
                        logPrompt = '... '
                    self._logFile.write(logPrompt + lineInput + '\n')
                inputExprLineList.append(lineInput)
                ps = paren_state('\n'.join(inputExprLineList))
                if ps.depth <= 0:
                    submit = True

            if not submit:
                continue

            inputExprStr = '\n'.join(inputExprLineList).strip()
            inputExprLineList = []
            if self._rl and inputExprStr:
                self._rl.add_history(inputExprStr)

            if not inputExprStr:
                continue

            try:
                if inputExprStr[0] == ']':
                    self._runListenerCommand(inputExprStr)
                else:
                    result = self._interp.eval(inputExprStr)
                    self._writeResult(result)
            except StopIteration:
                break
            except ReplExit:
                # (exit) at an interactive prompt: unwind to top level and note it
                # quietly (dim), then carry on at the next '>>> '.
                DIM, RESET = self._colors('dim', 'reset')
                print(DIM + '; (exit) ignored at REPL top level' + RESET)
            except KeyboardInterrupt:
                self._writeErrorMsg('Interrupted.')
            except Exception as e:
                self._writeErrorMsg(Listener._format_error(e))

            print()

    def eval_and_exit(self, expressions):
        """Evaluate each -e/--evaluate expression as a full REPL transcript and
        return a process exit status.  For every expression the input is echoed
        with the REPL prompts ('>>> ' on the first line, '... ' on
        continuations), the expression is evaluated (so display/write output
        appears in place), and the value is shown as '==> <value>' -- or an
        error as '%%% ...'.  Mirrors readEvalPrintLoop's per-entry handling so
        `-e <expr>` looks exactly like typing <expr> at the prompt.  Returns 1
        if any expression raised, else 0."""
        status = 0
        for expr in expressions:
            # Echo the input with the REPL's prompts.
            lines = expr.split('\n')
            i = 0
            while i < len(lines):
                print(('>>> ' if i == 0 else '... ') + lines[i])
                i = i + 1
            try:
                stripped = expr.strip()
                if stripped and stripped[0] == ']':
                    self._runListenerCommand(stripped)
                else:
                    result = self._interp.eval(expr)
                    self._writeResult(result)
            except StopIteration:
                break
            except ReplExit:
                DIM, RESET = self._colors('dim', 'reset')
                print(DIM + '; (exit) ignored at REPL top level' + RESET)
            except KeyboardInterrupt:
                self._writeErrorMsg('Interrupted.')
                status = 1
            except Exception as e:
                self._writeErrorMsg(Listener._format_error(e))
                status = 1
            print()
        return status

    # ---- session-log parser and runner ----

    def sessionLog_restore(self, filename, verbosity=0):
        """Read a session log and evaluate every expression it contains,
        WITHOUT comparing against the recorded return / output / errors.
        Used by `]readlog` and `]resume` to replay state."""
        try:
            f = open(filename, 'r', encoding='utf-8')
        except FileNotFoundError:
            raise ListenerCommandError('File not found: ' + filename)
        text = f.read()
        f.close()

        entries = Listener._parse_log(text)
        k = 0
        while k < len(entries):
            entry = entries[k]
            expr = entry[0]
            fold_case = entry[4]
            if verbosity > 0:
                exp_lines = expr.splitlines()
                j = 0
                while j < len(exp_lines):
                    if j == 0:
                        print('\n>>> ' + exp_lines[j])
                    else:
                        print('... ' + exp_lines[j])
                    j = j + 1
            eval_expr = ('#!fold-case\n' if fold_case else '') + expr
            try:
                resultStr = self._interp.eval(eval_expr)
            except ReplExit:
                resultStr = ''
            except Exception:
                resultStr = ''
            if verbosity >= 3:
                print('\n==> ' + resultStr)
            k = k + 1

    def sessionLog_test(self, filename, verbosity=3):
        """Run a single log file through the test harness.

        Returns a TestResult.  Each entry is compared on three axes:
        return value, printed output, and error message.  All three must
        match the log's expectation for the entry to pass.

        Output is FAILURE-ONLY by default: passing entries write nothing, and
        the per-file header is emitted lazily on the first failure, so a clean
        file produces no output at all (this keeps the .run reports small).
        verbosity 3 restores the verbose mode (header up front + a PASS line
        per entry + a 'TESTS PASSED' footer)."""
        try:
            f = open(filename, 'r', encoding='utf-8')
        except FileNotFoundError:
            raise ListenerCommandError('File not found: ' + filename)
        text = f.read()
        f.close()

        BOLD, DIM, GREEN, RED, RESET = self._colors(
            'bold', 'dim', 'green', 'red', 'reset')

        entries = Listener._parse_log(text)
        n_pass = 0
        n_fail = 0
        saved_fallback = _expander_mod._include_fallback_dir
        _expander_mod._include_fallback_dir = os.path.dirname(
            os.path.abspath(filename))

        # Emit the per-file header at most once.  Called lazily on the first
        # failing entry so an all-pass file writes nothing (verbose mode prints
        # it up front instead).
        header_state = [False]

        def emit_header():
            if not header_state[0]:
                print()
                print(BOLD + 'Test file:' + RESET + ' ' + filename)
                print(BOLD + ('-' * (11 + len(filename))) + RESET)
                header_state[0] = True

        if verbosity >= 3:
            emit_header()

        k = 0
        while k < len(entries):
            entry = entries[k]
            expr_src = entry[0]
            expected_output = entry[1]
            expected_retval = entry[2]
            expected_error = entry[3]
            fold_case = entry[4]
            i = k + 1

            actual_retval = ''
            actual_error = ''
            timed_out = False
            out_capture = io.StringIO()
            eval_expr = ('#!fold-case\n' if fold_case else '') + \
                expr_src.strip()
            ctx = self._interp.get_ctx()
            ctx.timeout_at = time.monotonic() + 120.0
            try:
                actual_retval = self._interp.eval(eval_expr,
                                                  outStrm=out_capture)
            except ReplExit:
                # A test that calls (exit): contain the abort so the suite keeps
                # running.  Recorded as an error token so the entry flags rather
                # than silently passing.
                actual_error = '(exit)'
            except KeyboardInterrupt:
                actual_error = 'Interrupted.'
            except Exception as e:
                actual_error = Listener._format_error(e)
                if 'Evaluation timed out.' in actual_error:
                    timed_out = True
            finally:
                ctx.timeout_at = 0.0

            actual_output = out_capture.getvalue().rstrip()
            expected_output = expected_output.rstrip()

            # '%%% *' or '%%% %any-error% <hint>' means any error is acceptable.
            # '%%% %optional-error% <hint>' models R7RS "it is an error" (undefined
            # behavior): the test passes whether an error is signaled OR the form
            # returns normally -- only termination is asserted.  The retval/output
            # checks are bypassed too, since the outcome is unspecified.
            optional_error = expected_error.startswith('%optional-error%')
            if timed_out:
                # A timeout is a hang, never a legitimate "an error is signaled":
                # force a failure regardless of the expected-error marker (otherwise
                # a hang on a '%%% *' / '%any-error%' / '%optional-error%' test would
                # be silently scored as a pass).
                error_ok = False
                retval_ok = False
                output_ok = False
            elif optional_error:
                error_ok = True
                retval_ok = True
                output_ok = True
            else:
                if expected_error == '*' or expected_error.startswith('%any-error%'):
                    error_ok = bool(actual_error)
                else:
                    error_ok = actual_error == expected_error
                retval_ok = Listener._match_retval(
                    actual_retval, expected_retval)
                output_ok = actual_output == expected_output

            stripped = expr_src.strip()
            if stripped:
                label = stripped.splitlines()[0]
            else:
                label = ''
            if len(label) > 56:
                label = _substring(label, 0, 53) + '...'

            if retval_ok and error_ok and output_ok:
                n_pass = n_pass + 1
                if verbosity >= 3:
                    print(DIM + '  %3d. PASS  %s' % (i, label) + RESET)
            else:
                n_fail = n_fail + 1
                emit_header()
                print(RED + '  %3d. FAIL  %s' % (i, label) + RESET)
                if timed_out:
                    print('         *** evaluation timed out (treated as failure) ***')
                if not retval_ok:
                    print(
                        '         expected return: [' + expected_retval + ']')
                    print('         actual return:   [' + actual_retval + ']')
                if not output_ok:
                    print(
                        '         expected output: [' + expected_output + ']')
                    print('         actual output:   [' + actual_output + ']')
                if not error_ok:
                    if expected_error == '*' or expected_error.startswith('%any-error%'):
                        print('         expected an error, but none was raised')
                    else:
                        print(
                            '         expected error:  [' + expected_error + ']')
                        print(
                            '         actual error:    [' + actual_error + ']')
            k = k + 1

        _expander_mod._include_fallback_dir = saved_fallback
        total = n_pass + n_fail
        # Failure-only reporting: print the per-file footer only when something
        # failed (the header was already emitted lazily above).  The all-pass
        # 'TESTS PASSED' line is verbose-mode only.
        if n_fail > 0:
            print()
            print(RED + ('%d of %d FAILED' % (n_fail, total)) + RESET)
        elif verbosity >= 3:
            print()
            print(GREEN + str(total) + ' TESTS PASSED' + RESET)
        return TestResult(n_pass, n_fail)

    # ---- listener commands ----

    def _runListenerCommand(self, source):
        body = _substring(source, 1, len(source))
        parts = body.split()
        if not parts:
            raise ListenerCommandError("expected a command after ']'")
        cmd = parts[0]
        args = []
        k = 1
        while k < len(parts):
            args.append(parts[k])
            k = k + 1
        fn = self._commands.get(cmd)
        if fn is None:
            raise ListenerCommandError('Unknown listener command: ' + cmd)
        fn(args)

    def _cmd_help(self, args):
        """Usage: ]help [command]
        List every listener command, or show detailed help for one.
        """
        if args:
            name = args[0]
            fn = self._commands.get(name)
            if fn is None or fn.__doc__ is None:
                raise ListenerCommandError('No help on "' + name + '".')
            print(fn.__doc__.strip())
            return
        BOLD, CYAN, RESET = self._colors('bold', 'cyan', 'reset')
        header = 'Listener Commands'
        names = []
        for name in self._commands:
            names.append(name)
        names.sort()
        print()
        print(BOLD + header + RESET)
        print(BOLD + ('=' * len(header)) + RESET)
        if CYAN:
            columnize(names, 69, itemColor=CYAN)
        else:
            columnize(names, 69)
        print()
        print("Type ']help <command>' for detailed help on a command.")

    def _cmd_quit(self, args):
        """Usage: ]quit
        Exit the listener.
        """
        if args:
            raise ListenerCommandError('Usage: ]quit')
        if self._logFile is not None:
            self._cmd_close([])
        print('Bye.')
        raise StopIteration()

    def _cmd_exit(self, args):
        """Usage: ]exit
        Exit the listener (same as ]quit).
        """
        self._cmd_quit(args)

    def _cmd_reboot(self, args):
        """Usage: ]reboot
        Reset the interpreter to a fresh global environment.  Any
        user-defined bindings are lost.  Cannot reboot while logging.
        """
        if args:
            raise ListenerCommandError('Usage: ]reboot')
        if self._logFile:
            raise ListenerCommandError(
                'Please close the log file before rebooting (]close).')
        DIM, RESET = self._colors('dim', 'reset')
        print(DIM + '- Initializing interpreter' + RESET)
        self._interp.reboot()
        print()
        Listener._print_welcome_banner(self._use_color())
        print()

    def _cmd_readsrc(self, args):
        """Usage: ]readsrc <filename>
        Read and evaluate a Scheme source file.
        """
        if len(args) != 1:
            raise ListenerCommandError('Usage: ]readsrc <filename>')
        filename = args[0].strip()
        try:
            self._interp.evalFile(filename)
        except FileNotFoundError:
            raise ListenerCommandError('File not found: ' + filename)
        GREEN, RESET = self._colors('green', 'reset')
        print(GREEN + 'Source file read successfully:' + RESET + ' ' + filename)

    def _cmd_load(self, args):
        """Usage: ]load <filename>
        Alias for ]readsrc.  Read and evaluate a Scheme source file.
        """
        self._cmd_readsrc(args)

    def _cmd_readlog(self, args):
        """Usage: ]readlog <filename> [v|V]
        Read and evaluate a log file without testing.  Useful for
        replaying a recorded session to restore state.  Append 'v' for a
        verbose echo of each expression and result.
        """
        if len(args) != 1 and len(args) != 2:
            raise ListenerCommandError('Usage: ]readlog <filename> [v|V]')
        if len(args) == 2 and args[1].upper() == 'V':
            verbosity = 3
        else:
            verbosity = 0
        filename = args[0]
        self.sessionLog_restore(filename, verbosity=verbosity)
        GREEN, RESET = self._colors('green', 'reset')
        print(GREEN + 'Log file read successfully:' + RESET + ' ' + filename)

    def _cmd_log(self, args):
        """Usage: ]log <filename>
        Begin a new session-log (dribble) file at <filename>.  Every
        subsequent input line, return value, and error is mirrored into
        the file in the standard log format.  Stop with ]close.
        """
        if len(args) != 1:
            raise ListenerCommandError('Usage: ]log <filename>')
        if self._logFile is not None:
            raise ListenerCommandError(
                'Already logging.  Close the current log first (]close).')
        filename = args[0]
        try:
            self._logFile = open(filename, 'w')
        except OSError:
            raise ListenerCommandError('Unable to open file for writing.')
        ts = datetime.datetime.now().isoformat()
        self._writeLn(';;; Dribble started ' + ts)
        self._writeLn(';;; ' + filename)
        self._writeLn('')

    def _cmd_close(self, args):
        """Usage: ]close
        Close the current logging session.
        """
        if args:
            raise ListenerCommandError('Usage: ]close')
        if self._logFile is None:
            raise ListenerCommandError('Not currently logging.')
        ts = datetime.datetime.now().isoformat()
        self._writeLn('')
        self._writeLn(';;; Dribble stopped ' + ts)
        self._logFile.close()
        self._logFile = None

    def _cmd_resume(self, args):
        """Usage: ]resume <filename>
        Replay an existing log file to restore its state, then reopen it
        for append so further interaction continues to be logged.
        """
        if self._logFile:
            raise ListenerCommandError(
                'A log file is already open.  Close it first (]close).')
        if len(args) != 1:
            raise ListenerCommandError('Usage: ]resume <filename>')
        filename = args[0]
        self.sessionLog_restore(filename)
        try:
            self._logFile = open(filename, 'a')
        except OSError:
            raise ListenerCommandError('Unable to reopen file for append.')
        ts = datetime.datetime.now().isoformat()
        self._writeLn('')
        self._writeLn(';;; Dribble resumed ' + ts)
        self._writeLn('')

    # ---- scheme-tests directory resolution ----

    def _set_scheme_tests_dir(self, path, source):
        """Set (or clear, when `path` is falsy) the scheme-tests root and derive
        the per-suite subdirectories from it.  `source` is a human-readable label
        of where the value came from, shown by ]scheme-tests."""
        if path:
            base = os.path.abspath(os.path.expanduser(path))
            self._scheme_tests_dir = base
            # The .log suite subdirectories come from test-suites.scm (the single
            # source of truth -- the same feature/compliance/regression suites
            # ]suites runs); the _*_SUBDIR constants are only a fallback for a
            # tests root that has no (or an unreadable) registry.
            reg = self._registry_log_paths()
            self._testdir = os.path.join(
                base, reg.get('feature', _FEATURE_SUBDIR))
            self._compliancedir = os.path.join(
                base, reg.get('compliance', _COMPLIANCE_SUBDIR))
            self._regressiondir = os.path.join(
                base, reg.get('regression', _REGRESSION_SUBDIR))
            self._runsdir = os.path.join(base, _RUNS_SUBDIR)
            self._scheme_tests_source = source
        else:
            self._scheme_tests_dir = None
            self._testdir = ''
            self._compliancedir = ''
            self._regressiondir = ''
            self._runsdir = ''
            self._scheme_tests_source = 'unset'

    @staticmethod
    def _no_scheme_tests_message():
        """Explain that the scheme-tests root is unset and how to set it."""
        return ('the scheme-tests directory is not set, so tests cannot run.\n'
                'Point it at the repo\'s scheme-tests folder (the one containing\n'
                'log-tests/) in any of these ways (a later one overrides an earlier):\n'
                '  1. environment variable:  SCHEME_TESTS_DIR=<path>/scheme-tests\n'
                '  2. command-line option:   python -m pyscheme --scheme-tests <path>/scheme-tests\n'
                '  3. listener command:      ]scheme-tests <path>/scheme-tests')

    def _require_scheme_tests(self):
        """Raise a helpful error if no scheme-tests root has been configured."""
        if not self._scheme_tests_dir:
            raise ListenerCommandError(Listener._no_scheme_tests_message())

    def _cmd_scheme_tests(self, args):
        """Usage: ]scheme-tests [<directory>]

        With no argument, show the current scheme-tests root (and where it was
        set from) plus the derived suite directories.  With a directory, set the
        root for this session, overriding the -T/--scheme-tests option and the
        SCHEME_TESTS_DIR environment variable.  No path is hardcoded; this is one
        of the three ways to point the interpreter at the test suites."""
        if args:
            self._set_scheme_tests_dir(' '.join(args), 'listener command')
            note = ('' if os.path.isdir(self._scheme_tests_dir)
                    else '  (warning: directory does not exist)')
            print('scheme-tests set to ' + self._scheme_tests_dir + note)
            return
        if not self._scheme_tests_dir:
            print('scheme-tests: not set')
            print(Listener._no_scheme_tests_message())
            return
        exists = '' if os.path.isdir(self._scheme_tests_dir) else '  (does not exist)'
        print('scheme-tests: ' + self._scheme_tests_dir
              + '  [' + self._scheme_tests_source + ']' + exists)
        print('  feature:    ' + self._testdir)
        print('  compliance: ' + self._compliancedir)
        print('  regression: ' + self._regressiondir)
        print('  runs:       ' + self._runsdir)

    def _cmd_feature(self, args):
        """Usage: ]feature [<filename>]

        With a filename: read a session log file and verify that the
        interpreter produces the same return values, output, and errors
        recorded in the file.

        With no arguments: run every *.log file under 'testing/' in
        alpha order.  Each file starts from a freshly rebooted
        interpreter.  A timestamped run report is written to
        testing/runs/.

        Log file format:
           >>> expression
           ... continuation lines
           ==> expected return value
           %%% expected error message (in place of ==>)
           ;;; comment at top level (ignored)
        """
        if len(args) > 1:
            raise ListenerCommandError('Usage: ]feature [<filename>]')
        if self._logFile:
            raise ListenerCommandError(
                'Please close the log before running tests (]close).')

        if len(args) == 1:
            arg = args[0]
            if os.path.isdir(arg):
                testDir = os.path.abspath(arg)
                filenames = retrieveFileList(arg)
                if not filenames:
                    raise ListenerCommandError('No .log files in ' + repr(arg))
            else:
                testDir = os.path.dirname(os.path.abspath(arg))
                filenames = [arg]
        else:
            self._require_scheme_tests()
            if not os.path.isdir(self._testdir):
                raise ListenerCommandError(
                    'feature test directory not found: ' + self._testdir)
            testDir = self._testdir
            filenames = retrieveFileList(self._testdir)
            if not filenames:
                raise ListenerCommandError(
                    'No .log files in ' + repr(self._testdir))

        return self._runTestFiles(filenames, testDir, 'feature')

    @staticmethod
    def _parse_iter_count(value):
        """Parse the value of an -I: switch: a positive integer with an optional
        metric suffix (k/K = 1e3, m/M = 1e6).  e.g. '100000', '100k', '5M'.
        Raises ListenerCommandError on a malformed value."""
        s = value.strip()
        mult = 1
        if s and s[-1] in 'kK':
            mult = 1000
            s = s[:-1]
        elif s and s[-1] in 'mM':
            mult = 1000000
            s = s[:-1]
        try:
            n = int(s)
        except ValueError:
            raise ListenerCommandError(
                'Invalid -I: iteration count (use e.g. -I:100000, -I:100k, -I:5M)')
        if n <= 0:
            raise ListenerCommandError('-I: iteration count must be positive')
        return n * mult

    def _runTestFiles(self, filenames, testDir, suite, tco_iters=_TCO_ITER_DEFAULT):
        """Run each file through sessionLog_test, reboot between files,
        write a run report to <testDir>/runs/, print a grand total.
        `suite` is 'feature' | 'compliance' | 'regression'; it becomes part
        of the run-report filename: yyyy-mm-dd-hhmmss-<suite>-PyScheme.run.
        `tco_iters` is bound to %MAX_TCO_ITER_COUNT% after each file's reboot."""
        BOLD, GREEN, RED, RESET = self._colors('bold', 'green', 'red', 'reset')

        testDir = os.path.abspath(testDir)

        # Prepare a run report file.  When ]suites has opened a shared report,
        # append this suite's section to it (and leave it open for the caller to
        # close); otherwise open our own file.  The filename carries only the
        # timestamp -- no suite type -- so all suites can share one file.
        owns_run_file = self._shared_run_file is None
        if not owns_run_file:
            runFile = self._shared_run_file
            runFilename = self._shared_run_filename
        else:
            runFile = None
            runFilename = ''
            runsDir = self._runsdir if self._runsdir else os.path.join(
                testDir, 'runs')
            try:
                os.makedirs(runsDir, exist_ok=True)
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
                runFilename = os.path.join(
                    runsDir, timestamp + '-PyScheme.run')
                runFile = open(runFilename, 'w', encoding='utf-8')
            except OSError:
                runFile = None
                runFilename = ''

        # Label this suite's section in the report (the suite type is no longer
        # in the filename, and several sections may share one file).
        if runFile is not None:
            print('========== suite: ' + suite + ' ==========', file=runFile)

        grand_pass = 0
        grand_fail = 0
        # list of (name, pass, fail) tuples (positional access only)
        per_file = []

        # Absolutify so filenames survive the chdir below.
        filenames = [os.path.abspath(f) for f in filenames]

        start_time = time.monotonic()
        savedStdout = sys.stdout
        savedCwd = os.getcwd()
        os.chdir(testDir)
        try:
            for filename in filenames:
                self._interp.reboot(load_rc=False)
                # Bind %MAX_TCO_ITER_COUNT% in the fresh env so 3.05 (and any future
                # iteration-tunable test) can size its loops.  Harmless elsewhere.
                self._interp.rawEval('(define ' + _TCO_ITER_VAR + ' '
                                     + str(tco_iters) + ')')
                base = os.path.basename(filename)
                padded = base.ljust(56)
                # Name and status are intentionally two separate print calls.
                # Name flushes before the test runs so the user sees progress;
                # status completes the same line after.  Do not merge into one print.
                print(padded + ' ', end='', flush=True, file=savedStdout)
                if runFile is not None:
                    sys.stdout = runFile
                    self._output_to_file = True
                # verbosity=1: write only failing entries to the .run report
                # (passing cases produce no output -> small reports).
                r = self.sessionLog_test(filename, verbosity=1)
                if runFile is not None:
                    sys.stdout = savedStdout
                    self._output_to_file = False
                grand_pass = grand_pass + r.n_pass
                grand_fail = grand_fail + r.n_fail
                per_file.append((filename, r.n_pass, r.n_fail))
                if r.n_fail == 0:
                    status = GREEN + str(r.n_pass) + ' passed' + RESET
                else:
                    total = r.n_pass + r.n_fail
                    status = RED + str(r.n_fail) + ' of ' + \
                        str(total) + ' failed' + RESET
                print(status, file=savedStdout, flush=True)
        finally:
            sys.stdout = savedStdout
            self._output_to_file = False
            os.chdir(savedCwd)

        self._interp.reboot(load_rc=False)

        # Total wall-clock time for the whole suite run (all files), formatted
        # as HH:MM:SS.ssssss.
        elapsed = time.monotonic() - start_time
        _h = int(elapsed // 3600)
        _m = int((elapsed % 3600) // 60)
        _s = elapsed - _h * 3600 - _m * 60
        elapsed_str = '%02d:%02d:%09.6f' % (_h, _m, _s)

        # Grand-total screen summary.
        if len(filenames) > 1:
            print()
            total = grand_pass + grand_fail
            nfiles = len(filenames)
            if grand_fail == 0:
                print(GREEN + 'all ' + str(total) + ' test cases passed across '
                      + str(nfiles) + ' files' + RESET)
            else:
                print(RED + str(grand_fail) + ' of ' + str(total)
                      + ' tests failed across ' + str(nfiles) + ' files' + RESET)
            print(BOLD + 'Elapsed: ' + elapsed_str + RESET)

            # Write the tail of the report file.
            if runFile is not None:
                report = []
                report.append('')
                report.append('')
                report.append('Test Report')
                report.append('===========')
                for entry in per_file:
                    name = entry[0]
                    p = entry[1]
                    f = entry[2]
                    short = os.path.basename(name)
                    if f == 0:
                        msg = str(p) + ' TESTS PASSED!'
                    else:
                        total = p + f
                        msg = '(' + str(f) + '/' + str(total) + ') Failed.'
                    report.append(short.ljust(56) + ' ' + msg)
                report.append('')
                report.append('Total test files: ' + str(len(filenames)) + '.')
                report.append('Total test cases: '
                              + str(grand_pass + grand_fail) + '.')
                report.append('Elapsed time: ' + elapsed_str)
                for reportLine in report:
                    print(reportLine, file=runFile)
                # When ]suites owns the file, leave it open (and silent) for the
                # next suite's section; only close/announce our own file.
                if owns_run_file:
                    runFile.close()
                    print()
                    print('Test output: ' + runFilename)
        else:
            # Single-file run: still report how long it took.
            print(BOLD + 'Elapsed: ' + elapsed_str + RESET)
            if runFile is not None:
                print('', file=runFile)
                print('Elapsed time: ' + elapsed_str, file=runFile)
                if owns_run_file:
                    runFile.close()

        return (grand_pass, grand_fail)

    def _cmd_compliance(self, args):
        """Usage: ]compliance [-I:<count>] [<file.log> | <start> [<end>]]

        Run the R7RS compliance test suite against the configured directory.
          ]compliance                    -- run all tests
          ]compliance 3                  -- run tests with filename >= "3"
          ]compliance 3 4                -- run tests with "3" <= filename < "4"
          ]compliance 3.1 - Booleans.log -- run that one file
          ]compliance -I:5M              -- run all, sizing TCO soak loops to 5,000,000
          ]compliance -I:1M 3.05         -- run the 3.05 file with 1,000,000 iters

        -I:<count> sets %MAX_TCO_ITER_COUNT% (default 100000), the upper bound
        compliance test 3.05 uses for its proper-tail-recursion soak loops.
        Accepts a plain integer or a metric suffix: -I:100000, -I:100k, -I:5M.
        A high count is a per-machine memory soak, not a portable TCO proof
        (3.05 proves TCO with %continuation-depth at small N regardless).

        Filename comparison is case-insensitive on the bare filename only.
        The interpreter is rebooted before each file and after the suite.
        A timestamped run report is written to <compliancedir>/runs/.

        Compliance log extras vs plain test logs:
          ==> X or ==> Y       — either alternative is accepted
          %%% <exact>          — actual error must match exactly
          %%% * | %%% %any-error%       — any error, but one MUST be raised
                                 (R7RS "an error is signaled")
          %%% %optional-error%  — R7RS "it is an error" (undefined): passes
                                 whether an error is raised OR the form returns;
                                 asserts only that evaluation terminates
        """
        if self._logFile:
            raise ListenerCommandError(
                'Please close the log before running compliance (]close).')

        self._require_scheme_tests()
        compdir = self._compliancedir
        if not os.path.isdir(compdir):
            raise ListenerCommandError(
                'Compliance directory not found: ' + compdir)

        # Pull out an optional -I:<count> switch; the rest are file/range selectors.
        tco_iters = _TCO_ITER_DEFAULT
        rest = []
        for a in args:
            if a.startswith('-I:'):
                tco_iters = Listener._parse_iter_count(a[3:])
            else:
                rest.append(a)

        return self._run_suite_files(rest, compdir, 'compliance',
                                     tco_iters=tco_iters)

    def _cmd_regression(self, args):
        """Usage: ]regression [<file.log> | <start> [<end>]]

        Run the regression test suite against the configured directory.
          ]regression                  -- run all regression files
          ]regression 03               -- run files with filename >= "03"
          ]regression 03 06            -- run files with "03" <= filename < "06"
          ]regression 03-evaluator.log -- run that one file

        Regression tests are Scheme-observable, non-spec tripwires pinned to
        past bugs; spec deviations are guarded by ]compliance instead.  Files
        are grouped by subsystem (numeric prefix forces fundamental->abstract
        order).  See regression-tests/00-conventions.md.

        The interpreter is rebooted before each file and after the suite.
        A timestamped run report is written to the configured runs/ directory.
        """
        if self._logFile:
            raise ListenerCommandError(
                'Please close the log before running regressions (]close).')

        self._require_scheme_tests()
        regdir = self._regressiondir
        if not os.path.isdir(regdir):
            raise ListenerCommandError(
                'Regression directory not found: ' + regdir)

        return self._run_suite_files(args, regdir, 'regression')

    def _run_suite_files(self, args, suite_dir, suite_label, tco_iters=None):
        """Shared file-selection + dispatch for ]compliance and ]regression.

        suite_dir is already validated and any -I: switch already stripped from
        args.  Single-file mode, range mode (0/1/2 selectors), and the empty /
        out-of-range errors are identical between the two suites; only the suite
        label and the optional tco_iters differ.  Returns the (pass, fail) tuple
        from _runTestFiles so ]suites can tally the counts."""
        kw = {} if tco_iters is None else {'tco_iters': tco_iters}

        # Detect single-file mode: last token ends with ".log".
        # Tokens are rejoined since filenames may contain spaces.
        if args and args[-1].endswith('.log'):
            fname = ' '.join(args)
            fpath = os.path.join(suite_dir, fname)
            if not os.path.isfile(fpath):
                raise ListenerCommandError('File not found: ' + fname)
            return self._runTestFiles([fpath], suite_dir, suite_label, **kw)

        # Range mode: 0 args = all, 1 arg = [start, inf), 2 args = [start, end).
        if len(args) > 2:
            raise ListenerCommandError(
                'Usage: ]' + suite_label + ' [<file.log> | <start> [<end>]]')

        all_files = retrieveFileList(suite_dir)
        if not all_files:
            raise ListenerCommandError('No .log files in ' + suite_dir)

        if not args:
            return self._runTestFiles(all_files, suite_dir, suite_label, **kw)

        start_lc = args[0].lower()
        end_lc = args[1].lower() if len(args) == 2 else None

        filtered = [
            f for f in all_files
            if os.path.basename(f).lower() >= start_lc
            and (end_lc is None or os.path.basename(f).lower() < end_lc)
        ]

        if not filtered:
            if end_lc is not None:
                raise ListenerCommandError(
                    'No .log files in range [' + args[0] + ', ' + args[1] + ')')
            else:
                raise ListenerCommandError(
                    'No .log files at or after "' + args[0] + '"')

        return self._runTestFiles(filtered, suite_dir, suite_label, **kw)

    def _cmd_suites(self, args):
        """Usage: ]suites [list | <name|alias|category> ... | all]

        The registry-driven test runner.  Reads every suite from
        scheme-tests/test-suites.scm (the single source of truth) and runs the
        ones you name -- .log batteries and SRFI-64 .scm suites IN-PROCESS,
        external tools (gc_test, the differential/fuzz harnesses) as spawned
        subprocesses.

          ]suites              show the catalog (same as ]suites list)
          ]suites list         show the catalog
          ]suites <tok> ...    run the suite(s) a token names: a suite name, a
                               short alias (e.g. mc), or a category (e.g.
                               metamorphic).  Selected suites run in registry
                               order, deduped.
          ]suites all          run every suite ('all' is an implicit category)

        Cherry's checklist is rendered from `]suites list`; adding a suite to the
        registry makes it appear here and in Cherry automatically.
        """
        self._require_scheme_tests()
        suites = self._load_suites()
        if not args or (len(args) == 1 and args[0].lower() == 'list'):
            self._print_suite_list(suites)
            return
        if self._logFile:
            raise ListenerCommandError(
                'Please close the log before running suites (]close).')

        pairs = self._resolve_suite_tokens(args, suites)
        port = self._port_tag()
        runnable = []   # effective suite dicts, each carrying a '_label'
        skipped = []    # (label, ports)
        for suite, vname in pairs:
            eff = dict(suite)
            applied = 'quick'
            vd = suite.get('variants', {}).get(vname)
            # Apply the variant only if it exists AND is available on this port;
            # otherwise the suite falls back to its base (quick) run.
            if vname != 'quick' and vd is not None:
                if vd.get('ports', suite['ports']) in ('both', port):
                    eff.update(vd)
                    applied = vname
            label = suite['name'] + ('' if applied == 'quick' else ' (' + applied + ')')
            if eff['ports'] in ('both', port):
                eff['_label'] = label
                runnable.append(eff)
            else:
                skipped.append((label, eff['ports']))

        BOLD, GREEN, RED, RESET = self._colors('bold', 'green', 'red', 'reset')
        print()
        print(BOLD + '; running suites: '
              + ', '.join(s['_label'] for s in runnable) + RESET)
        for label, ports in skipped:
            print('  (skipping ' + label + ' -- ' + ports + '-only on this port)')
        print()

        # A combined .run report is opened only when a .log (log-kind) suite is in
        # the batch; those append their sections to it as before.  scheme/external
        # suites don't use it.
        have_log = any(s['kind'] == 'log' for s in runnable)
        shared = None
        shared_filename = ''
        if have_log:
            tag = 'CPPScheme2' if 'cpp' in self._language else 'PyScheme'
            runsDir = self._runsdir if self._runsdir else os.path.join(
                self._testdir if self._testdir else '.', 'runs')
            try:
                os.makedirs(runsDir, exist_ok=True)
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
                shared_filename = os.path.join(
                    runsDir, timestamp + '-' + tag + '.run')
                self._shared_run_file = open(shared_filename, 'w',
                                             encoding='utf-8')
                self._shared_run_filename = shared_filename
                shared = self._shared_run_file
            except OSError:
                self._shared_run_file = None
                self._shared_run_filename = ''

        results = []   # dicts: name, ok, npass, nfail, nxpass, note
        try:
            for s in runnable:
                print(BOLD + '-- ' + s['_label'] + ' (' + (s['kind'] or '?')
                      + ') --' + RESET)
                if s['kind'] == 'log':
                    results.append(self._run_log_suite(s))
                elif s['kind'] == 'scheme':
                    results.append(self._run_scheme_suite(s))
                elif s['kind'] == 'external':
                    results.append(self._run_external_suite(s))
                else:
                    results.append({'name': s['name'], 'ok': False, 'npass': 0,
                                    'nfail': 1, 'nxpass': 0,
                                    'note': 'unknown kind ' + repr(s['kind'])})
                print()
        finally:
            self._shared_run_file = None
            self._shared_run_filename = ''

        total_fail = sum(r['nfail'] for r in results)
        total_pass = sum(r['npass'] for r in results)
        total_xpass = sum(r['nxpass'] for r in results)

        print(BOLD + '===== SUITES COMPLETE =====' + RESET)
        for r in results:
            if not r['ok']:
                note = ' -- ' + r['note'] if r['note'] else ''
                detail = RED + 'FAILED' + note + RESET
            elif r['nxpass'] > 0:
                detail = (GREEN + 'passed' + RESET + ' ' + BOLD
                          + ('(%d now-passing expect-fail -- promote it)'
                             % r['nxpass']) + RESET)
            elif r['npass']:
                detail = GREEN + str(r['npass']) + ' passed' + RESET
            else:
                detail = GREEN + 'ok' + RESET
            print('  ' + r['name'].ljust(22) + ' ' + detail)
        if total_fail == 0:
            print(BOLD + GREEN + '  ALL SUITES PASSED' + RESET)
        else:
            print(BOLD + RED + '  SUITE FAILURES: ' + str(total_fail) + RESET)
        if total_xpass:
            print(BOLD + '  (' + str(total_xpass) + ' known-open expect-fail '
                  'case(s) now pass -- update the pins)' + RESET)

        if shared is not None:
            print('', file=shared)
            print('===== SUITES COMPLETE =====', file=shared)
            for r in results:
                detail = ('FAILED' if not r['ok']
                          else (str(r['npass']) + ' passed' if r['npass']
                                else 'ok'))
                print('  ' + r['name'].ljust(22) + ' ' + detail, file=shared)
            print('  ALL SUITES PASSED' if total_fail == 0
                  else '  SUITE FAILURES: ' + str(total_fail), file=shared)
            shared.close()
            print()
            print('Test output: ' + shared_filename)
        return (total_pass, total_fail)

    # ---- registry-driven ]suites helpers (backlog #9) ----------------------

    def _port_tag(self):
        """'py' or 'cpp' -- used to filter suites by their (ports ...) field."""
        return 'cpp' if 'cpp' in self._language else 'py'

    def _registry_path(self):
        return os.path.join(self._scheme_tests_dir, 'test-suites.scm')

    def _suite_abspath(self, rel):
        """Resolve a registry-relative path against the scheme-tests root."""
        return os.path.normpath(os.path.join(self._scheme_tests_dir, rel))

    @staticmethod
    def _parse_props(props, into):
        """Fill the suite dict `into` from a list of (key value ...) prop forms.
        Reused for a suite's base props and for a (variant ...) block's overrides
        (which set only the keys they mention)."""
        for prop in props:
            if not isinstance(prop, list) or not prop:
                continue
            key, vals = prop[0], prop[1:]
            if key == 'kind' and vals:
                into['kind'] = vals[0]
            elif key == 'alias':
                into['alias'] = list(vals)
            elif key == 'categories':
                into['categories'] = list(vals)
            elif key == 'ports' and vals:
                into['ports'] = vals[0]
            elif key == 'path' and vals:
                into['path'] = vals[0]
            elif key == 'libs':
                into['libs'] = list(vals)
            elif key == 'run':
                into['run'] = list(vals)
            elif key == 'cwd' and vals:
                into['cwd'] = vals[0]
            elif key == 'pass' and vals:
                into['pass'] = vals[0]      # 'exit-0' or ['grep', 'REGEX']
            elif key == 'desc' and vals:
                into['desc'] = vals[0]
            elif key == 'tco-soak' and vals:
                into['tco-soak'] = ('calibrate' if vals[0] == 'calibrate'
                                    else int(vals[0]))
            elif key == 'variant' and vals:
                vd = {}
                Listener._parse_props(vals[1:], vd)
                into.setdefault('variants', {})[vals[0]] = vd

    def _registry_log_paths(self):
        """{suite-name: relative-path} for the log-kind suites in test-suites.scm,
        or {} when the registry is absent/unreadable.  Tolerant (never raises):
        the test commands fall back to the _*_SUBDIR defaults when it returns {}.
        Lets feature/compliance/regression share the registry's paths instead of
        hardcoding the subdirs in two places."""
        try:
            suites = self._load_suites()
        except Exception:
            return {}
        return {s['name']: s['path'] for s in suites
                if s.get('kind') == 'log' and s.get('path')}

    def _load_suites(self):
        """Parse test-suites.scm into a list of suite dicts, in registry order."""
        path = self._registry_path()
        if not os.path.isfile(path):
            raise ListenerCommandError(']suites: registry not found: ' + path)
        with open(path, 'r', encoding='utf-8') as fh:
            forms = Listener._read_sexprs(fh.read())
        suites = []
        for form in forms:
            if not isinstance(form, list) or len(form) < 2 or form[0] != 'suite':
                continue
            d = {'name': form[1], 'kind': None, 'alias': [], 'categories': [],
                 'ports': 'both', 'path': None, 'libs': [], 'run': None,
                 'cwd': '.', 'pass': 'exit-0', 'tco-soak': None, 'desc': '',
                 'variants': {}}
            Listener._parse_props(form[2:], d)
            suites.append(d)
        if not suites:
            raise ListenerCommandError(']suites: no suites found in ' + path)
        return suites

    @staticmethod
    def _selector_matches(sel, suites):
        """Suite names matched by a bare selector: name / alias / category / all."""
        return [s['name'] for s in suites
                if sel == s['name'] or sel in s['alias']
                or sel in s['categories'] or sel == 'all']

    def _resolve_suite_tokens(self, tokens, suites):
        """Map tokens to (suite, variant) pairs in REGISTRY order, deduped by
        (name, variant).  A token resolves as a bare selector first (variant
        'quick'); if that fails, a trailing -<variant> is stripped and the rest
        re-resolved.  Variant names are 'quick'/'slow' plus any declared in the
        registry.  Raises on an unknown token."""
        known = {'quick', 'slow'}
        for s in suites:
            known.update(s.get('variants', {}).keys())
        seen = set()
        pairs = []
        for tok in tokens:
            variant = 'quick'
            names = Listener._selector_matches(tok, suites)
            if not names:
                for v in known:
                    suf = '-' + v
                    if tok.endswith(suf) and len(tok) > len(suf):
                        cand = Listener._selector_matches(tok[:-len(suf)], suites)
                        if cand:
                            names, variant = cand, v
                            break
            if not names:
                raise ListenerCommandError(
                    'unknown suite/category ' + repr(tok) + ' (try `]suites list`)')
            nameset = set(names)
            for s in suites:        # registry order
                if s['name'] in nameset and (s['name'], variant) not in seen:
                    seen.add((s['name'], variant))
                    pairs.append((s, variant))
        return pairs

    def _print_suite_list(self, suites):
        """Render the catalog for `]suites list` (and for Cherry to parse)."""
        BOLD, RESET = self._colors('bold', 'reset')
        port = self._port_tag()
        print(BOLD + 'Available test suites  (registry: '
              + self._registry_path() + ')' + RESET)
        print('  ' + 'NAME'.ljust(22) + 'ALIASES'.ljust(13)
              + 'KIND'.ljust(10) + 'PORTS'.ljust(7) + 'DESCRIPTION')
        cats = set()
        for s in suites:
            cats.update(s['categories'])
            na = '' if s['ports'] in ('both', port) else '  (n/a here)'
            print('  ' + s['name'].ljust(22)
                  + ', '.join(s['alias']).ljust(13)
                  + (s['kind'] or '?').ljust(10)
                  + s['ports'].ljust(7) + (s['desc'] or '') + na)
        print()
        print('  Categories: ' + ', '.join(sorted(cats)) + '   (+ all)')
        print('  Run:  ]suites <name|alias|category> ...   |   ]suites all')

    def _run_log_suite(self, suite):
        """Run a .log directory suite in-process (reboot per file)."""
        path = self._suite_abspath(suite['path'])
        if not os.path.isdir(path):
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'directory not found: ' + path}
        files = retrieveFileList(path)
        if not files:
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'no .log files in ' + path}
        kw = {}
        if isinstance(suite.get('tco-soak'), int):
            kw['tco_iters'] = suite['tco-soak']
        p, f = self._runTestFiles(files, path, suite['name'], **kw)
        return {'name': suite.get('_label', suite['name']), 'ok': f == 0,
                'npass': p, 'nfail': f, 'nxpass': 0, 'note': ''}

    def _scheme_child_argv(self, libs, fpath):
        """Build the invocation that runs <fpath> in a CHILD interpreter of this
        port with the given -L library paths.  Overridden per port (cppScheme2
        uses its exe); pyScheme re-invokes `python -m pyscheme`."""
        argv = [sys.executable, '-m', 'pyscheme']
        for lib in libs:
            argv += ['-L', lib]
        argv.append(fpath)
        return argv

    def _run_scheme_suite(self, suite):
        """Run a single .scm suite by spawning a CHILD interpreter (-L libs),
        capturing its output and reading the SRFI-64 summary (pass = 0 failed; a
        now-passing expect-fail shows as XPASS).  A child process is used rather
        than the running interpreter because library state (e.g. the (srfi 64)
        pass/fail counters) is process-global -- running several SRFI-64 suites
        in ONE process would accumulate their counts.  Still bash-free and
        cross-platform (it is the interpreter running the test, just a child)."""
        import subprocess
        fpath = self._suite_abspath(suite['path'])
        if not os.path.isfile(fpath):
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'file not found: ' + fpath}
        libs = [self._suite_abspath(l) for l in suite['libs']]
        argv = self._scheme_child_argv(libs, fpath)
        env = dict(os.environ)
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env['PYTHONPATH'] = (pkg_parent + os.pathsep + env['PYTHONPATH']
                             if env.get('PYTHONPATH') else pkg_parent)
        try:
            proc = subprocess.run(argv, cwd=os.path.dirname(fpath), env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        except (FileNotFoundError, OSError) as e:
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'cannot launch: ' + str(e)}
        out = proc.stdout or ''
        for line in out.rstrip('\n').splitlines():
            print('    ' + line)
        npass, nfail, nxpass = Listener._parse_test_output(out)
        if nfail is None:
            return {'name': suite.get('_label', suite['name']), 'ok': False,
                    'npass': npass, 'nfail': 1, 'nxpass': 0, 'note': 'no test summary'}
        return {'name': suite.get('_label', suite['name']), 'ok': nfail == 0,
                'npass': npass, 'nfail': nfail, 'nxpass': nxpass, 'note': ''}

    def _run_external_suite(self, suite):
        """Spawn an external tool (the kind that can't be in-process) and judge
        pass by exit code or a (grep REGEX) of its output."""
        import re
        import subprocess
        if suite['run'] is None:
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'no (run ...) in registry'}
        cwd = self._suite_abspath(suite['cwd'])
        # {interp} = this port's launch invocation.  In the PROGRAM slot (run[0])
        # it must expand to MULTIPLE argv tokens -- argv is spawned as a list, so
        # a single "python -m pyscheme" string is not a findable executable (that
        # was the WinError 2 a suite like `(run "{interp}" ...)` hit).  Splice the
        # real launch there.  In an ARGUMENT slot {interp} is a string handed to
        # another program (e.g. a shell wrapper) that re-splits it itself, so keep
        # it joined as before.
        run = suite['run']
        argv = []
        spliced_program = False
        for idx, tok in enumerate(run):
            if tok == '{interp}' and idx == 0:
                argv.extend([sys.executable, '-m', 'pyscheme'])
                spliced_program = True
            else:
                argv.append(tok.replace('{interp}', 'python -m pyscheme'))
        if not spliced_program and ('/' in argv[0] or '\\' in argv[0]):
            # a relative program path (e.g. a wrapper script) -> resolve vs cwd
            argv[0] = os.path.normpath(os.path.join(cwd, argv[0]))
        env = dict(os.environ)
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env['PYTHONPATH'] = (pkg_parent + os.pathsep + env['PYTHONPATH']
                             if env.get('PYTHONPATH') else pkg_parent)
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        except (FileNotFoundError, OSError) as e:
            return {'name': suite.get('_label', suite['name']), 'ok': False, 'npass': 0, 'nfail': 1,
                    'nxpass': 0, 'note': 'cannot launch: ' + str(e)}
        out = proc.stdout or ''
        cond = suite['pass']
        if isinstance(cond, list) and cond and cond[0] == 'grep':
            ok = re.search(cond[1], out) is not None
        else:
            ok = (proc.returncode == 0)
        for ln in [x for x in out.splitlines() if x.strip()][-3:]:
            print('    ' + ln)
        return {'name': suite.get('_label', suite['name']), 'ok': ok, 'npass': 0,
                'nfail': 0 if ok else 1, 'nxpass': 0,
                'note': '' if ok else 'exit ' + str(proc.returncode)}

    @staticmethod
    def _read_sexprs(text):
        """Minimal S-expression reader for the suite registry: returns a list of
        forms, each a nested Python list whose atoms (symbol/string/number) are
        all plain str -- structure (list vs atom) carries the only distinction
        the registry needs.  Handles ; line comments and "..." strings with \\
        escapes.  Not a general Scheme reader -- just enough for test-suites.scm."""
        i, n = 0, len(text)

        def skip_ws():
            nonlocal i
            while i < n:
                c = text[i]
                if c == ';':
                    while i < n and text[i] != '\n':
                        i += 1
                elif c in ' \t\r\n':
                    i += 1
                else:
                    break

        def read_form():
            nonlocal i
            skip_ws()
            if i >= n:
                return None            # EOF (top level only)
            c = text[i]
            if c == '(':
                i += 1
                lst = []
                while True:
                    skip_ws()
                    if i >= n:
                        raise ListenerCommandError(
                            ']suites: malformed registry (unclosed paren)')
                    if text[i] == ')':
                        i += 1
                        return lst
                    lst.append(read_form())
            if c == ')':
                raise ListenerCommandError(
                    ']suites: malformed registry (unexpected ")")')
            if c == '"':
                i += 1
                buf = []
                while i < n and text[i] != '"':
                    if text[i] == '\\' and i + 1 < n:
                        i += 1
                        buf.append({'n': '\n', 't': '\t', 'r': '\r'}.get(
                            text[i], text[i]))
                    else:
                        buf.append(text[i])
                    i += 1
                i += 1                 # closing quote
                return ''.join(buf)
            start = i                  # bare atom
            while i < n and text[i] not in ' \t\r\n()";':
                i += 1
            return text[start:i]

        forms = []
        while True:
            f = read_form()
            if f is None:
                break
            forms.append(f)
        return forms

    @staticmethod
    def _parse_test_output(out):
        """Pull (npass, nfail, nxpass) from a suite program's stdout.  Works for
        the SRFI-64 summary ('=== N passed, M failed ===' plus an optional
        '(... K unexpected-pass ...)' line) and the older bespoke '<N> checks,
        <M> failed' format.  nfail is None when no summary line was found."""
        import re
        fails = re.findall(r'(\d+)\s+failed', out)
        nfail = int(fails[-1]) if fails else None
        passes = re.findall(r'(\d+)\s+(?:passed|checks|datums)', out)
        npass = int(passes[-1]) if passes else 0
        xps = re.findall(r'(\d+)\s+unexpected-pass', out)
        nxpass = int(xps[-1]) if xps else 0
        return npass, nfail, nxpass

    def _cmd_cd(self, args):
        """Usage: ]cd <directory>
        Change the process working directory.  Relative paths passed to
        include, load, and other file-taking forms resolve from the new
        directory.  `~` is expanded to the user's home.  Use ]pwd to
        print the current directory.
        """
        if len(args) != 1:
            raise ListenerCommandError('Usage: ]cd <directory>')
        target = os.path.expanduser(args[0])
        if not os.path.isdir(target):
            raise ListenerCommandError('Not a directory: ' + target)
        os.chdir(target)
        DIM, RESET = self._colors('dim', 'reset')
        print(DIM + os.getcwd() + RESET)

    def _cmd_pwd(self, args):
        """Usage: ]pwd
        Print the current working directory.
        """
        if args:
            raise ListenerCommandError('Usage: ]pwd')
        print(os.getcwd())

    def _print_tty_color_state(self):
        """Print the current forced-color state as 'tty-color: on|off'."""
        print('tty-color: ' + ('on' if self._emit_color_codes else 'off'))

    def _cmd_toggle_tty_color(self, args):
        """Usage: ]toggle-tty-color
        Toggle forced emission of ANSI color escape codes.  When ON, color
        codes are emitted even when stdout is not a TTY (e.g. when the REPL is
        driven through a pipe by a GUI front-end such as cherry that renders
        the codes itself).  When OFF, color follows the usual rule -- emitted
        only to a real terminal.  Prints the resulting state.
        """
        if args:
            raise ListenerCommandError('Usage: ]toggle-tty-color')
        self._emit_color_codes = not self._emit_color_codes
        self._print_tty_color_state()

    def _cmd_tty_color(self, args):
        """Usage: ]tty-color
        Show whether forced ANSI color-code emission is currently on or off
        (see ]toggle-tty-color).
        """
        if args:
            raise ListenerCommandError('Usage: ]tty-color')
        self._print_tty_color_state()

    def _cmd_lhistory(self, args):
        """Usage: ]lhistory [<n>]
        Query or set the maximum readline history size.  With no
        argument, prints the current value.
        """
        if len(args) > 1:
            raise ListenerCommandError('Usage: ]lhistory [<n>]')
        if not args:
            print('Current history size: ' + str(Listener._historyMax))
            return
        try:
            n = int(args[0])
        except ValueError:
            raise ListenerCommandError('History size must be an integer.')
        if n < 1:
            raise ListenerCommandError(
                'History size must be a positive integer.')
        Listener._historyMax = n
        if Listener._rl is not None:
            Listener._rl.set_history_length(n)
        print('New history size: ' + str(n))

    def _cmd_debug(self, args):
        """Usage: ]debug
        Open the interactive debugger.  Set breakpoints and watches, then
        use rd to run expressions with debugging active.  Type h at the
        debug> prompt for a full command reference.
        """
        if args:
            raise ListenerCommandError('Usage: ]debug')
        ctx = self._interp._ctx
        env = self._interp._env
        ctx.debugger.run_debugger_repl(ctx, env)
