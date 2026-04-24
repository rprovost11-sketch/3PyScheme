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
- Testing: `]test [file]` runs a single log file or every log under the
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

from pyscheme.Environment import SchemeUnboundError
from pyscheme.Parser      import SchemeSyntaxError
from pyscheme.Analyzer    import SchemeAnalysisError
from pyscheme.Utils       import columnize, retrieveFileList, writeln_multiFile, paren_state


_DEFAULT_TEST_DIR = 'testing'
_HIST_FILE        = os.path.expanduser('~/.pyscheme_history')


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


class ListenerCommandError(Exception):
   """Raised by listener-command bodies to signal a user-level error."""
   pass


class TestResult:
   """Return container for sessionLog_test: pass/fail counts.  POD."""
   def __init__(self, n_pass, n_fail):
      self.n_pass = n_pass
      self.n_fail = n_fail


# ---- module-level helpers ---------------------------------------------


def _format_error(exc):
   """Produce the user-visible text for an exception.  Same text at
   REPL and test harness so expected/actual error strings compare."""
   if isinstance(exc, NotImplementedError):
      return 'Not implemented: ' + str(exc)
   if isinstance(exc, (SchemeSyntaxError, SchemeAnalysisError,
                       SchemeUnboundError, ListenerCommandError)):
      return str(exc)
   msg = str(exc)
   if msg:
      return type(exc).__name__ + ': ' + msg
   return type(exc).__name__


def _compute_indent(lines):
   """Return whitespace to auto-indent the next continuation line.
   Indents 3 spaces per unclosed paren depth."""
   depth     = 0
   in_string = False
   escape    = False
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
            elif ch == '(':
               depth = depth + 1
            elif ch == ')':
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


def _parse_log(text):
   """Parse a session log into a list of 4-tuples (expr, output, retval, error).

   Each entry begins with a `>>> ` line.  Continuation lines (`... `)
   belong to the same expression.  Lines between the expression and
   `==> ` (with no marker) are output (from display/print/help).  A
   `==> ` line gives the return value; a `%%% ` line gives the error
   message.  Comment lines starting with `;;` outside an entry are
   skipped."""
   lines   = text.splitlines(keepends=True)
   entries = []
   idx     = 0
   n       = len(lines)
   while idx < n:
      # Skip forward to the next '>>> ' line.
      while idx < n and not lines[idx].startswith('>>> '):
         idx = idx + 1
      if idx >= n:
         break
      expr   = _substring(lines[idx], 4, len(lines[idx]))
      output = ''
      retval = ''
      error  = ''
      idx = idx + 1
      # Continuation lines '... '.
      while idx < n and lines[idx].startswith('... '):
         expr = expr + _substring(lines[idx], 4, len(lines[idx]))
         idx = idx + 1
      # Pre-retval output: lines that aren't a known marker.
      while idx < n:
         line = lines[idx]
         if line.startswith('==> ') or line.rstrip() == '==>':
            break
         if line.startswith('... ') or line.startswith('>>> ') or line.startswith('%%% '):
            break
         output = output + line
         idx = idx + 1
      # '==> ' retval block (may extend over several non-marker lines).
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
            if line.startswith(';'):
               expr = expr + line
            else:
               retval = retval + line
            idx = idx + 1
      # '%%% ' error block.
      if idx < n and lines[idx].startswith('%%% '):
         error = _substring(lines[idx], 4, len(lines[idx]))
         idx = idx + 1
         while idx < n and lines[idx].startswith('%%% '):
            error = error + _substring(lines[idx], 4, len(lines[idx]))
            idx = idx + 1
      if expr:
         entries.append((expr, output.rstrip(), retval.rstrip(), error.rstrip()))
   return entries


def _print_welcome_banner():
   """Short welcome banner printed by __init__ and ]reboot."""
   useColor = sys.stdout.isatty()
   if useColor:
      BOLD_GREEN = '\033[1;92m'
      CYAN       = '\033[96m'
      RESET      = '\033[0m'
   else:
      BOLD_GREEN = ''
      CYAN       = ''
      RESET      = ''
   print('Enter any expression to have it evaluated by the interpreter.')
   print("Evaluate '" + CYAN + '(help)' + RESET + "' for online help.")
   print("Type  '" + CYAN + ']help' + RESET + "' to list Listener commands.")
   print(BOLD_GREEN + 'Welcome!' + RESET)


# ---- Listener ---------------------------------------------------------


class Listener:
   """Interactive REPL with session logging and log-based testing.

   Partly modelled on Python's cmd module.  Each `]foo bar baz` input
   dispatches through self._commands to the _cmd_foo method; the
   method's docstring is shown by `]help foo`."""

   # Readline state - class-level so history is shared across listeners
   # in the same process.
   _rl         = None
   _historyMax = 500

   def __init__(self, anInterpreter, testdir=_DEFAULT_TEST_DIR,
                language='pyscheme', version='0.1',
                author='pyscheme authors',
                project='https://example/pyscheme'):
      self._interp   = anInterpreter
      # Absolutify so ]cd doesn't break ]test.
      self._testdir  = os.path.abspath(testdir)
      self._logFile  = None
      self._language = language
      self._version  = version
      self._author   = author
      self._project  = project
      self._init_readline()
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
         'test':     self._cmd_test,
         'cd':       self._cmd_cd,
         'pwd':      self._cmd_pwd,
         'lhistory': self._cmd_lhistory,
      }
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
      return sys.stdout.isatty()

   def _banner(self):
      color = self._use_color()
      if color:
         BOLD_WHITE = '\033[1;97m'
         DIM        = '\033[2m'
         RESET      = '\033[0m'
      else:
         BOLD_WHITE = ''
         DIM        = ''
         RESET      = ''
      print(BOLD_WHITE + self._language + ' ' + self._version
            + ' by ' + self._author + RESET)
      print(DIM + 'Project home ' + self._project + RESET)
      print()
      print(DIM + '- Interpreter Initialized' + RESET, flush=True)
      print(DIM + '- Listener Initialized' + RESET, flush=True)
      print()
      _print_welcome_banner()
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
      if color:
         GREEN = '\033[92m'
         BOLD  = '\033[1;97m'
         RESET = '\033[0m'
      else:
         GREEN = ''
         BOLD  = ''
         RESET = ''
      lines = text.splitlines()
      if not lines:
         lines = ['']
      i = 0
      while i < len(lines):
         line = lines[i]
         plain    = '==> ' + line
         colorLn  = GREEN + '==>' + RESET + ' ' + BOLD + line + RESET
         if color:
            self._writeLn(colorLn, file=None, flush=True)
         else:
            self._writeLn(plain, file=None, flush=True)
         i = i + 1

   def _writeErrorMsg(self, errMsg):
      """Render an error with the `%%% ` prefix and mirror to the log."""
      color = self._use_color()
      if color:
         RED   = '\033[91m'
         RESET = '\033[0m'
      else:
         RED   = ''
         RESET = ''
      lines = errMsg.splitlines()
      if not lines:
         lines = [errMsg]
      i = 0
      while i < len(lines):
         line  = lines[i]
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
               indent    = _compute_indent(inputExprLineList)
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
               if ps.depth > 0 and not ps.in_string:
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

         inputExprStr      = '\n'.join(inputExprLineList).strip()
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
         except KeyboardInterrupt:
            self._writeErrorMsg('Interrupted.')
         except Exception as e:
            self._writeErrorMsg(_format_error(e))

         print()

   # ---- session-log parser and runner ----

   def sessionLog_restore(self, filename, verbosity=0):
      """Read a session log and evaluate every expression it contains,
      WITHOUT comparing against the recorded return / output / errors.
      Used by `]readlog` and `]resume` to replay state."""
      try:
         f = open(filename, 'r')
      except FileNotFoundError:
         raise ListenerCommandError('File not found: ' + filename)
      text = f.read()
      f.close()

      entries = _parse_log(text)
      k = 0
      while k < len(entries):
         entry = entries[k]
         expr  = entry[0]
         if verbosity > 0:
            exp_lines = expr.splitlines()
            j = 0
            while j < len(exp_lines):
               if j == 0:
                  print('\n>>> ' + exp_lines[j])
               else:
                  print('... ' + exp_lines[j])
               j = j + 1
         try:
            resultStr = self._interp.eval(expr)
         except Exception:
            resultStr = ''
         if verbosity >= 3:
            print('\n==> ' + resultStr)
         k = k + 1

   def sessionLog_test(self, filename, verbosity=3):
      """Run a single log file through the test harness.

      Returns a TestResult.  Each entry is compared on three axes:
      return value, printed output, and error message.  All three must
      match the log's expectation for the entry to pass.  verbosity 2
      prints a running counter, 3 prints each entry line."""
      try:
         f = open(filename, 'r')
      except FileNotFoundError:
         raise ListenerCommandError('File not found: ' + filename)
      text = f.read()
      f.close()

      color = self._use_color()
      if color:
         BOLD  = '\033[1;97m'
         DIM   = '\033[2m'
         GREEN = '\033[92m'
         RED   = '\033[91m'
         RESET = '\033[0m'
      else:
         BOLD  = ''
         DIM   = ''
         GREEN = ''
         RED   = ''
         RESET = ''

      entries = _parse_log(text)
      n_pass  = 0
      n_fail  = 0

      print()
      print(BOLD + 'Test file:' + RESET + ' ' + filename)
      print(BOLD + ('-' * (11 + len(filename))) + RESET)

      k = 0
      while k < len(entries):
         entry = entries[k]
         expr_src        = entry[0]
         expected_output = entry[1]
         expected_retval = entry[2]
         expected_error  = entry[3]
         i               = k + 1

         actual_retval = ''
         actual_error  = ''
         out_capture   = io.StringIO()
         try:
            actual_retval = self._interp.eval(expr_src.strip(),
                                              outStrm=out_capture)
         except KeyboardInterrupt:
            actual_error = 'Interrupted.'
         except Exception as e:
            actual_error = _format_error(e)

         actual_output   = out_capture.getvalue().rstrip()
         expected_output = expected_output.rstrip()

         retval_ok = actual_retval == expected_retval
         error_ok  = actual_error  == expected_error
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
            print(RED + '  %3d. FAIL  %s' % (i, label) + RESET)
            if not retval_ok:
               print('         expected return: [' + expected_retval + ']')
               print('         actual return:   [' + actual_retval + ']')
            if not output_ok:
               print('         expected output: [' + expected_output + ']')
               print('         actual output:   [' + actual_output + ']')
            if not error_ok:
               print('         expected error:  [' + expected_error + ']')
               print('         actual error:    [' + actual_error + ']')
         k = k + 1

      total = n_pass + n_fail
      print()
      if n_fail == 0:
         print(GREEN + str(total) + ' TESTS PASSED' + RESET)
      else:
         print(RED + ('%d of %d FAILED' % (n_fail, total)) + RESET)
      return TestResult(n_pass, n_fail)

   # ---- listener commands ----

   def _runListenerCommand(self, source):
      body  = _substring(source, 1, len(source))
      parts = body.split()
      if not parts:
         raise ListenerCommandError("expected a command after ']'")
      cmd  = parts[0]
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
         fn   = self._commands.get(name)
         if fn is None or fn.__doc__ is None:
            raise ListenerCommandError('No help on "' + name + '".')
         print(fn.__doc__.strip())
         return
      color = self._use_color()
      if color:
         BOLD  = '\033[1;97m'
         CYAN  = '\033[96m'
         RESET = '\033[0m'
      else:
         BOLD  = ''
         CYAN  = ''
         RESET = ''
      header = 'Listener Commands'
      names  = []
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
      color = self._use_color()
      if color:
         DIM   = '\033[2m'
         RESET = '\033[0m'
      else:
         DIM   = ''
         RESET = ''
      print(DIM + '- Initializing interpreter' + RESET)
      self._interp.reboot()
      print()
      _print_welcome_banner()
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
      color = self._use_color()
      if color:
         GREEN = '\033[92m'
         RESET = '\033[0m'
      else:
         GREEN = ''
         RESET = ''
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
      color = self._use_color()
      if color:
         GREEN = '\033[92m'
         RESET = '\033[0m'
      else:
         GREEN = ''
         RESET = ''
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

   def _cmd_test(self, args):
      """Usage: ]test [<filename>]

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
         raise ListenerCommandError('Usage: ]test [<filename>]')
      if self._logFile:
         raise ListenerCommandError(
            'Please close the log before running tests (]close).')

      if len(args) == 1:
         filenames = [args[0]]
      else:
         if not os.path.isdir(self._testdir):
            raise ListenerCommandError(
               'No test directory: ' + repr(self._testdir))
         filenames = retrieveFileList(self._testdir)
         if not filenames:
            raise ListenerCommandError(
               'No .log files in ' + repr(self._testdir))

      self._runTestFiles(filenames)

   def _runTestFiles(self, filenames):
      """Run each file through sessionLog_test, reboot between files,
      write a run report to testing/runs/, print a grand total."""
      color = self._use_color()
      if color:
         BOLD  = '\033[1;97m'
         GREEN = '\033[92m'
         RED   = '\033[91m'
         RESET = '\033[0m'
      else:
         BOLD  = ''
         GREEN = ''
         RED   = ''
         RESET = ''

      # Prepare a run report file when more than one log is being run.
      runFile     = None
      runFilename = ''
      if len(filenames) > 1:
         runsDir = os.path.join(self._testdir, 'runs')
         try:
            os.makedirs(runsDir, exist_ok=True)
            timestamp   = datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')
            runFilename = os.path.join(runsDir, 'test-' + timestamp + '.run')
            runFile     = open(runFilename, 'w')
         except OSError:
            runFile     = None
            runFilename = ''

      grand_pass = 0
      grand_fail = 0
      per_file   = []   # list of (name, pass, fail) tuples (positional access only)

      savedStdout = sys.stdout
      try:
         k = 0
         while k < len(filenames):
            filename = filenames[k]
            self._interp.reboot()
            base   = os.path.basename(filename)
            padded = base.ljust(40)
            print(padded + ' ', end='', flush=True, file=savedStdout)
            if runFile is not None:
               sys.stdout = runFile
            r = self.sessionLog_test(filename, verbosity=3)
            if runFile is not None:
               sys.stdout = savedStdout
            grand_pass = grand_pass + r.n_pass
            grand_fail = grand_fail + r.n_fail
            per_file.append((filename, r.n_pass, r.n_fail))
            if r.n_fail == 0:
               status = GREEN + str(r.n_pass) + ' passed' + RESET
            else:
               total  = r.n_pass + r.n_fail
               status = RED + str(r.n_fail) + ' of ' + str(total) + ' failed' + RESET
            print(status, file=savedStdout, flush=True)
            k = k + 1
      finally:
         sys.stdout = savedStdout

      self._interp.reboot()

      # Grand-total screen summary.
      if len(filenames) > 1:
         print()
         print(BOLD + 'Grand Total' + RESET)
         print(BOLD + ('-' * 11) + RESET)
         k = 0
         while k < len(per_file):
            entry = per_file[k]
            name  = entry[0]
            p     = entry[1]
            f     = entry[2]
            short = os.path.basename(name)
            if f == 0:
               status = GREEN + str(p) + ' passed' + RESET
            else:
               total  = p + f
               status = RED + str(f) + ' of ' + str(total) + ' failed' + RESET
            print('  ' + short.ljust(40) + ' ' + status)
            k = k + 1
         print()
         total = grand_pass + grand_fail
         if grand_fail == 0:
            print(GREEN + str(total) + ' TESTS PASSED across '
                  + str(len(filenames)) + ' files' + RESET)
         else:
            print(RED + str(grand_fail) + ' of ' + str(total)
                  + ' FAILED across ' + str(len(filenames)) + ' files' + RESET)

         # Write the tail of the report file.
         if runFile is not None:
            report = []
            report.append('')
            report.append('')
            report.append('Test Report')
            report.append('===========')
            k = 0
            while k < len(per_file):
               entry = per_file[k]
               name  = entry[0]
               p     = entry[1]
               f     = entry[2]
               short = os.path.basename(name)
               if f == 0:
                  msg = str(p) + ' TESTS PASSED!'
               else:
                  total = p + f
                  msg   = '(' + str(f) + '/' + str(total) + ') Failed.'
               report.append(short.ljust(40) + ' ' + msg)
               k = k + 1
            report.append('')
            report.append('Total test files: ' + str(len(filenames)) + '.')
            report.append('Total test cases: '
                          + str(grand_pass + grand_fail) + '.')
            k = 0
            while k < len(report):
               print(report[k], file=runFile)
               k = k + 1
            runFile.close()
            print()
            print('Test output: ' + runFilename)

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
      color = self._use_color()
      if color:
         DIM   = '\033[2m'
         RESET = '\033[0m'
      else:
         DIM   = ''
         RESET = ''
      print(DIM + os.getcwd() + RESET)

   def _cmd_pwd(self, args):
      """Usage: ]pwd
      Print the current working directory.
      """
      if args:
         raise ListenerCommandError('Usage: ]pwd')
      print(os.getcwd())

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
         raise ListenerCommandError('History size must be a positive integer.')
      Listener._historyMax = n
      if Listener._rl is not None:
         Listener._rl.set_history_length(n)
      print('New history size: ' + str(n))
