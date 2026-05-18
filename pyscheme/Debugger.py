"""Debugger: interactive step debugger, breakpoints, watches.

Ported from pythonslisp/Debugger.py.  Key differences from the original:
  - AST nodes are ConsCell objects, not Python lists.
  - Symbol names are case-sensitive (no uppercasing).
  - Backtrace reads _shadow_stack from Evaluator (not ArgFrame.call_form).
  - No CL-style restarts (:r command dropped).
  - break_on defaults are Scheme names: error, raise, raise-continuable.
  - Command dispatch uses an explicit dict (no getattr reflection).
  - All code follows C-port discipline (no f-strings, no tuple unpacking,
    no comprehensions, no getattr/setattr on dynamic attrs).
"""
import re
import sys

from pyscheme.AST import (
   NIL_VALUE,
   is_cons, is_nil, is_symbol, is_closure, is_primitive, is_case_closure,
   is_continuation,
   as_symbol, as_closure_params, as_closure_body, as_closure_rest_name,
   symbol_name,
)
from pyscheme.Environment import SchemeUnboundError, SchemeRuntimeError
from pyscheme.PrettyPrinter import pretty_print


# ── Private exception for rd restart ────────────────────────────────────

class _RestartRd(Exception):
   pass


# ── ANSI helpers ─────────────────────────────────────────────────────────

_ANSI  = sys.stdout.isatty()
_BOLD  = '\033[1m' if _ANSI else ''
_DIM   = '\033[2m' if _ANSI else ''
_RESET = '\033[0m' if _ANSI else ''


# ── Default break-on set ─────────────────────────────────────────────────

_DEFAULT_BREAK_ON = frozenset({'error', 'raise', 'raise-continuable'})


# ── Module-level utility helpers ─────────────────────────────────────────

def _substring(s, start, end):
   """Return s[start:end] as an explicit copy.  Maps to strncpy/substr in C."""
   result = ''
   i = start
   while i < end:
      result = result + s[i]
      i = i + 1
   return result


# ── StepHook ─────────────────────────────────────────────────────────────

class StepHook:
   """Step state: tracks the skip-depth for n (step-over) and o (step-out)."""

   def __init__(self):
      self._skip_until_depth = None   # len(K) threshold; None means step every expr
      self._step_over_first  = False  # True when the very next expr should auto-skip


# ── Debugger ─────────────────────────────────────────────────────────────

class Debugger:
   """Holds breakpoints, watch list, step hook, and implements debug REPLs."""

   # ---- static helpers ----

   @staticmethod
   def _cmd_word(line):
      parts = line.split(None, 1)
      if parts:
         return parts[0]
      return ''

   @staticmethod
   def _cmd_rest(line):
      parts = line.split(None, 1)
      if len(parts) > 1:
         return parts[1].strip()
      return ''

   @staticmethod
   def _is_callable(val):
      return is_closure(val) or is_primitive(val) or is_case_closure(val) or is_continuation(val)

   @staticmethod
   def _collect_locals(env):
      result     = {}
      current    = env
      global_env = env._global_env
      while current is not None and current is not global_env:
         for sid, val in current._bindings.items():
            if sid not in result:
               result[sid] = val
         current = current._parent
      return result

   @staticmethod
   def _print_scoped_locals(env, max_depth=None):
      current    = env
      global_env = env._global_env
      scope_num  = 0
      while current is not None and current is not global_env:
         if max_depth is not None and scope_num >= max_depth:
            break
         names_in_scope = []
         for sid in current._bindings:
            val = current._bindings[sid]
            if not Debugger._is_callable(val):
               names_in_scope.append(sid)
         if names_in_scope:
            names_in_scope.sort(key=symbol_name)
            print('--- scope ' + str(scope_num) + ' ---')
            i = 0
            while i < len(names_in_scope):
               sid = names_in_scope[i]
               print(symbol_name(sid) + ':   ' + pretty_print(current._bindings[sid]))
               i = i + 1
            scope_num = scope_num + 1
         current = current._parent

   @staticmethod
   def _print_named_vars(env, names):
      i = 0
      while i < len(names):
         name = names[i]
         try:
            val = env.lookup(name)
            print(name + ':   ' + pretty_print(val))
         except SchemeUnboundError:
            print(name + ':   <unbound>')
         i = i + 1

   @staticmethod
   def _parse_bp_args(rest):
      pairs = []
      i = 0
      n = len(rest)
      while i < n:
         while i < n and rest[i] == ' ':
            i = i + 1
         if i >= n:
            break
         j = i
         while j < n and rest[j] != ' ' and rest[j] != '(':
            j = j + 1
         name = _substring(rest, i, j)
         i = j
         while i < n and rest[i] == ' ':
            i = i + 1
         if i < n and rest[i] == '(':
            depth = 0
            j = i
            while j < n:
               if rest[j] == '(':
                  depth = depth + 1
               elif rest[j] == ')':
                  depth = depth - 1
                  if depth == 0:
                     j = j + 1
                     break
               j = j + 1
            cond = _substring(rest, i, j)
            i = j
         else:
            cond = None
         pairs.append([name, cond])
      return pairs

   @staticmethod
   def _walk_form(cell, callable_name, count, target_n):
      if not is_cons(cell):
         return None
      if is_symbol(cell.car) and as_symbol(cell.car) == callable_name:
         count[0] = count[0] + 1
         if count[0] == target_n:
            return cell
      arg = cell.cdr
      while is_cons(arg):
         result = Debugger._walk_form(arg.car, callable_name, count, target_n)
         if result is not None:
            return result
         arg = arg.cdr
      return None

   @staticmethod
   def _find_nth_call(body_cons, callable_name, target_n):
      count = [0]
      forms = body_cons
      while is_cons(forms):
         result = Debugger._walk_form(forms.car, callable_name, count, target_n)
         if result is not None:
            return result
         forms = forms.cdr
      return None

   @staticmethod
   def _collect_one(cell, callable_name, results):
      if not is_cons(cell):
         return
      if is_symbol(cell.car) and as_symbol(cell.car) == callable_name:
         results.append(cell)
      arg = cell.cdr
      while is_cons(arg):
         Debugger._collect_one(arg.car, callable_name, results)
         arg = arg.cdr

   @staticmethod
   def _collect_calls(body_cons, callable_name):
      results = []
      forms   = body_cons
      while is_cons(forms):
         Debugger._collect_one(forms.car, callable_name, results)
         forms = forms.cdr
      return results

   @staticmethod
   def _ann_has_any(cell, annotations):
      if id(cell) in annotations:
         return True
      if is_cons(cell):
         if Debugger._ann_has_any(cell.car, annotations):
            return True
         return Debugger._ann_has_any(cell.cdr, annotations)
      return False

   @staticmethod
   def _ann_flat(cell, annotations):
      tag = annotations.get(id(cell), '')
      if not is_cons(cell):
         return tag + pretty_print(cell)
      if is_nil(cell):
         return tag + '()'
      parts = []
      cur   = cell
      while is_cons(cur):
         parts.append(Debugger._ann_flat(cur.car, annotations))
         cur = cur.cdr
      if is_nil(cur):
         inner = ''
         i = 0
         while i < len(parts):
            if i > 0:
               inner = inner + ' '
            inner = inner + parts[i]
            i = i + 1
         return tag + '(' + inner + ')'
      inner = ''
      i = 0
      while i < len(parts):
         if i > 0:
            inner = inner + ' '
         inner = inner + parts[i]
         i = i + 1
      return tag + '(' + inner + ' . ' + Debugger._ann_flat(cur, annotations) + ')'

   @staticmethod
   def _ann_fmt(cell, annotations, ind):
      tag = annotations.get(id(cell), '')
      if not is_cons(cell):
         return tag + pretty_print(cell)
      if is_nil(cell):
         return tag + '()'
      flat    = Debugger._ann_flat(cell, annotations)
      has_ann = False
      cur     = cell.cdr
      while is_cons(cur):
         if Debugger._ann_has_any(cur.car, annotations):
            has_ann = True
            break
         cur = cur.cdr
      if len(flat) + ind <= 50 and not has_ann:
         return flat
      head_str  = Debugger._ann_flat(cell.car, annotations)
      child_ind = ind + 2
      pad       = ' ' * child_ind
      lines     = [tag + '(' + head_str]
      cur       = cell.cdr
      while is_cons(cur):
         lines.append(pad + Debugger._ann_fmt(cur.car, annotations, child_ind))
         cur = cur.cdr
      if not is_nil(cur):
         lines.append(pad + '. ' + Debugger._ann_fmt(cur, annotations, child_ind))
      last_idx = len(lines) - 1
      lines[last_idx] = lines[last_idx] + ')'
      result = ''
      i = 0
      while i < len(lines):
         if i > 0:
            result = result + '\n'
         result = result + lines[i]
         i = i + 1
      return result

   @staticmethod
   def _print_annotated(form, annotations, indent=0):
      print(Debugger._ann_fmt(form, annotations, indent))

   @staticmethod
   def _parse_help_entries(docstring):
      entries = []
      for line in docstring.strip().splitlines():
         line = line.strip()
         if not line:
            continue
         parts = re.split(r'\s{2,}', line, maxsplit=1)
         if len(parts) == 2:
            entries.append([parts[0], parts[1]])
      return entries

   @staticmethod
   def _print_help_entries(entries):
      if not entries:
         return
      max_w = 0
      i = 0
      while i < len(entries):
         w = len(entries[i][0])
         if w > max_w:
            max_w = w
         i = i + 1
      i = 0
      while i < len(entries):
         usage = entries[i][0]
         desc  = entries[i][1]
         print('  ' + usage.ljust(max_w) + '  ' + desc)
         i = i + 1

   @staticmethod
   def _parse_watch_args(source):
      try:
         from pyscheme.Parser import parse
         forms  = parse(source, '<watch>')
         result = []
         i = 0
         while i < len(forms):
            result.append(pretty_print(forms[i]))
            i = i + 1
         return result
      except Exception:
         return [source]

   def __init__(self):
      self.breakpoints     = {}      # name -> condition_str or None
      self._inner_targets  = {}      # id(cons_node) -> [display, cond, node, fn_name, fn_obj]
      self._disabled       = set()   # display names of disabled breakpoints
      self._bp_index       = []      # ordered names from last `b` listing (for b! N / b- N)
      self.watch_list      = []      # canonical expression strings
      self.break_on        = set(_DEFAULT_BREAK_ON)
      self._step_hook      = None    # StepHook or None
      self.input_fn        = input   # overridden by Listener for readline
      self._rl             = None    # readline module ref, set by Listener
      self._history        = []      # separate debug command history
      self._saved_history  = []      # main REPL history saved during debug session
      self._last_rd_expr   = None    # last rd expression for restart
      self._current_K      = None    # K reference captured at breakpoint for bt

      # Help method list: (base_name, method) in display order.
      # Flow-control commands (s n o c abort q) are documented inline.
      self._help_methods = [
         ('abort', self._cmd_abort),
         ('b',     self._cmd_b),
         ('bc',    self._cmd_bc),
         ('bo',    self._cmd_bo),
         ('bt',    self._cmd_bt),
         ('body',  self._cmd_body),
         ('c',     self._cmd_c),
         ('e',     self._cmd_e),
         ('h',     self._cmd_h),
         ('i',     self._cmd_i),
         ('n',     self._cmd_n),
         ('o',     self._cmd_o),
         ('q',     self._cmd_q),
         ('rd',    self._cmd_rd),
         ('s',     self._cmd_s),
         ('v',     self._cmd_v),
         ('w',     self._cmd_w),
      ]

      # Command dispatch table.  Flow-control commands (s n o c abort q)
      # are handled directly in _prompt_loop; they are NOT in this dict.
      self._commands = {
         'b':     self._cmd_b,
         'b-':    self._cmd_b,
         'b!':    self._cmd_b,
         'bc':    self._cmd_bc,
         'bc-':   self._cmd_bc,
         'bo':    self._cmd_bo,
         'bo-':   self._cmd_bo,
         'bo+':   self._cmd_bo,
         'bt':    self._cmd_bt,
         'body':  self._cmd_body,
         'e':     self._cmd_e,
         'h':     self._cmd_h,
         'i':     self._cmd_i,
         'rd':    self._cmd_rd,
         'v':     self._cmd_v,
         'w':     self._cmd_w,
         'w-':    self._cmd_w,
      }

   # ── History swapping ──────────────────────────────────────────────────

   def _swap_history_in(self):
      """Save main REPL history and install the debug history."""
      if self._rl is None:
         return
      rl = self._rl
      if sys.platform == 'win32':
         self._saved_history = rl.get_history()
         rl.set_history(self._history)
      else:
         n = rl.get_current_history_length()
         self._saved_history = []
         i = 1
         while i <= n:
            self._saved_history.append(rl.get_history_item(i))
            i = i + 1
         i = n
         while i > 0:
            rl.remove_history_item(i - 1)
            i = i - 1
         i = 0
         while i < len(self._history):
            rl.add_history(self._history[i])
            i = i + 1

   def _swap_history_out(self):
      """Save debug history and restore the main REPL history."""
      if self._rl is None:
         return
      rl = self._rl
      if sys.platform == 'win32':
         self._history = rl.get_history()
         rl.set_history(self._saved_history)
      else:
         n = rl.get_current_history_length()
         self._history = []
         i = 1
         while i <= n:
            self._history.append(rl.get_history_item(i))
            i = i + 1
         i = n
         while i > 0:
            rl.remove_history_item(i - 1)
            i = i - 1
         i = 0
         while i < len(self._saved_history):
            rl.add_history(self._saved_history[i])
            i = i + 1

   # ── Breakpoint resolution ─────────────────────────────────────────────

   def _resolve_bp_ref(self, ref):
      """Resolve a breakpoint reference (number or name) to a name string.
      Returns the name or None (with error printed) if not found."""
      if ref.isdigit():
         idx = int(ref)
         if idx < 1 or idx > len(self._bp_index):
            print('No breakpoint #' + str(idx) + '.  Use b to list breakpoints.')
            return None
         return self._bp_index[idx - 1]
      if ref in self.breakpoints:
         return ref
      for nid in self._inner_targets:
         display = self._inner_targets[nid][0]
         if display == ref:
            return ref
      print('No breakpoint on ' + ref + '.')
      return None

   # ── Command handlers ──────────────────────────────────────────────────
   #
   # Signature: (self, cmd, rest, ctx, env)
   #   cmd  = the full command word typed (e.g. 'w-', 'bo+', 'b!')
   #   rest = everything after the command word, stripped
   #   ctx  = Context
   #   env  = Environment

   def _cmd_abort(self, cmd, rest, ctx, env):
      """abort  abort execution to top level"""
      pass   # handled directly in _prompt_loop

   def _cmd_b(self, cmd, rest, ctx, env):
      """b [*]              list all breakpoints (numbered)
      b name ...         set breakpoint(s) on symbol name(s)
      b name (cond) ...  conditional breakpoint(s)
      b fn:call[:n]      break at nth call to call inside fn body
      b fn:call:?        list calls and choose interactively
      b fn:call:*        break on all calls to call in fn
      b! name|#          toggle enable/disable on a breakpoint
      b- name|# ...      remove breakpoint(s)
      b- fn:*            remove all inner breakpoints in fn
      b- fn:call:*       remove all breakpoints on call in fn
      b- *               clear all breakpoints"""

      if cmd == 'b!':
         if rest == '':
            print('Usage: b! <name-or-number> ...')
            return
         for ref in rest.split():
            name = self._resolve_bp_ref(ref)
            if name is None:
               continue
            if name in self._disabled:
               self._disabled.discard(name)
               print('Breakpoint on ' + name + ' enabled.')
            else:
               self._disabled.add(name)
               print('Breakpoint on ' + name + ' disabled.')
         return

      if cmd == 'b':
         if rest == '' or rest == '*':
            self._prune_stale_inner(env)
            self._bp_index = []
            has_any = False
            idx     = 1
            # Named breakpoints sorted alphabetically.
            bp_names = list(self.breakpoints.keys())
            bp_names.sort()
            i = 0
            while i < len(bp_names):
               has_any  = True
               name     = bp_names[i]
               cond     = self.breakpoints[name]
               self._bp_index.append(name)
               disabled = name in self._disabled
               tag      = '  :when ' + cond if cond else ''
               if disabled:
                  print('  ' + str(idx) + ': ' + _DIM + name + tag + '  [disabled]' + _RESET)
               else:
                  print('  ' + str(idx) + ': ' + _BOLD + name + _RESET + tag)
               idx = idx + 1
               i   = i + 1
            # Inner targets sorted by display name.
            inner_pairs = []
            for nid in self._inner_targets:
               display = self._inner_targets[nid][0]
               inner_pairs.append([display, nid])
            inner_pairs.sort()
            i = 0
            while i < len(inner_pairs):
               has_any  = True
               display  = inner_pairs[i][0]
               nid      = inner_pairs[i][1]
               cond     = self._inner_targets[nid][1]
               self._bp_index.append(display)
               disabled = display in self._disabled
               tag      = '  :when ' + cond if cond else ''
               if disabled:
                  print('  ' + str(idx) + ': ' + _DIM + display + tag + '  [disabled]' + _RESET)
               else:
                  print('  ' + str(idx) + ': ' + _BOLD + display + _RESET + tag)
               idx = idx + 1
               i   = i + 1
            if self.break_on:
               has_any     = True
               bo_names    = list(self.break_on)
               bo_names.sort()
               k = 0
               while k < len(bo_names):
                  print('  ' + bo_names[k] + '  [break-on]')
                  k = k + 1
            if not has_any:
               print('No breakpoints set.')
            return

         # b name [name ...] or b name (cond) [name (cond) ...]
         pairs = Debugger._parse_bp_args(rest)
         i = 0
         while i < len(pairs):
            name = pairs[i][0]
            cond = pairs[i][1]
            if ':' in name:
               self._set_inner_breakpoint(name, cond, env)
            else:
               self.breakpoints[name] = cond
               self._disabled.discard(name)
               if cond is None:
                  print('Breakpoint set on ' + name + '.')
               else:
                  print('Breakpoint set on ' + name + ' :when ' + cond)
            i = i + 1
         return

      # cmd == 'b-'
      if rest == '':
         print('Usage: b- <name-or-number> ... or b- *')
      elif rest == '*':
         self.breakpoints.clear()
         self._inner_targets.clear()
         self._disabled.clear()
         self._bp_index = []
         print('All breakpoints cleared.')
      else:
         for ref in rest.split():
            uref = ref
            # Check for wildcard patterns: fn:* or fn:call:*
            if ':' in uref and uref.endswith(':*'):
               parts = uref.split(':')
               count = 0
               if len(parts) == 2:
                  fn_name = parts[0]
                  for nid in list(self._inner_targets.keys()):
                     entry = self._inner_targets[nid]
                     if entry[3] == fn_name:
                        display = entry[0]
                        del self._inner_targets[nid]
                        self._disabled.discard(display)
                        count = count + 1
               elif len(parts) == 3:
                  fn_name   = parts[0]
                  call_name = parts[1]
                  prefix    = fn_name + ':' + call_name + ':'
                  for nid in list(self._inner_targets.keys()):
                     entry   = self._inner_targets[nid]
                     display = entry[0]
                     if entry[3] == fn_name and display.startswith(prefix):
                        del self._inner_targets[nid]
                        self._disabled.discard(display)
                        count = count + 1
               else:
                  print('Invalid spec: ' + ref)
                  continue
               if count:
                  suffix = 's' if count > 1 else ''
                  print(str(count) + ' breakpoint' + suffix + ' removed.')
               else:
                  print('No matching breakpoints.')
               continue
            # Normal ref (name or number).
            name = self._resolve_bp_ref(ref)
            if name is None:
               continue
            if name in self.breakpoints:
               del self.breakpoints[name]
               self._disabled.discard(name)
               print('Breakpoint on ' + name + ' removed.')
            else:
               removed = False
               for nid in list(self._inner_targets.keys()):
                  display = self._inner_targets[nid][0]
                  if display == name:
                     del self._inner_targets[nid]
                     self._disabled.discard(name)
                     print('Breakpoint on ' + name + ' removed.')
                     removed = True
                     break
               if not removed:
                  print('No breakpoint on ' + name + '.')

   def _cmd_bc(self, cmd, rest, ctx, env):
      """bc name|# (cond) ...  add/change condition on breakpoint(s)
      bc- name|# ...         clear condition from breakpoint(s)"""
      if not rest:
         print('Usage: bc <name-or-#> (cond) ... | bc- <name-or-#> ...')
         return

      if cmd == 'bc-':
         for ref in rest.split():
            name = self._resolve_bp_ref(ref)
            if name is None:
               continue
            if name in self.breakpoints:
               self.breakpoints[name] = None
               print('Condition cleared on ' + name + '.')
            else:
               found = False
               for nid in self._inner_targets:
                  entry = self._inner_targets[nid]
                  if entry[0] == name:
                     self._inner_targets[nid] = [entry[0], None, entry[2], entry[3], entry[4]]
                     print('Condition cleared on ' + name + '.')
                     found = True
                     break
               if not found:
                  print('No breakpoint on ' + name + '.')
         return

      # cmd == 'bc': set/change condition(s)
      pairs = Debugger._parse_bp_args(rest)
      i = 0
      while i < len(pairs):
         ref  = pairs[i][0]
         cond = pairs[i][1]
         if cond is None:
            print('Missing condition for ' + ref + '.')
            i = i + 1
            continue
         name = self._resolve_bp_ref(ref)
         if name is None:
            i = i + 1
            continue
         if name in self.breakpoints:
            self.breakpoints[name] = cond
            print('Condition set on ' + name + ' :when ' + cond)
         else:
            found = False
            for nid in self._inner_targets:
               entry = self._inner_targets[nid]
               if entry[0] == name:
                  self._inner_targets[nid] = [entry[0], cond, entry[2], entry[3], entry[4]]
                  print('Condition set on ' + name + ' :when ' + cond)
                  found = True
                  break
            if not found:
               print('No breakpoint on ' + name + '.')
         i = i + 1

   def _cmd_bo(self, cmd, rest, ctx, env):
      """bo [*]       show break-on list (auto-breaks during rd)
      bo name ...  add names to break-on list
      bo- name ...  remove from break-on list
      bo- *         clear break-on list
      bo+           restore default break-on list"""

      if cmd == 'bo+':
         self.break_on = set(_DEFAULT_BREAK_ON)
         bo_names = list(self.break_on)
         bo_names.sort()
         print('Break-on restored: ' + ', '.join(bo_names))
      elif cmd == 'bo-':
         if rest == '':
            print('Usage: bo- <name> or bo- *')
         elif rest == '*':
            self.break_on.clear()
            print('Break-on list cleared.')
         else:
            for n in rest.split():
               self.break_on.discard(n)
            if self.break_on:
               bo_names = list(self.break_on)
               bo_names.sort()
               print('Break-on: ' + ', '.join(bo_names))
            else:
               print('Break-on list cleared.')
      elif rest == '' or rest == '*':
         if self.break_on:
            bo_names = list(self.break_on)
            bo_names.sort()
            print('Break-on: ' + ', '.join(bo_names))
         else:
            print('Break-on list is empty.')
      else:
         for n in rest.split():
            self.break_on.add(n)
         bo_names = list(self.break_on)
         bo_names.sort()
         print('Break-on: ' + ', '.join(bo_names))

   def _cmd_bt(self, cmd, rest, ctx, env):
      """bt  show backtrace (shadow call stack)"""
      stack = ctx.shadow_stack
      if not stack:
         print('No backtrace available.')
         return
      from pyscheme.AST import format_with_caret
      i = 0
      while i < len(stack):
         entry = stack[i]
         label = entry[0]
         src   = entry[1]
         count = entry[2]
         if count > 1:
            label = label + ' [x' + str(count) + ']'
         print('  ' + str(i) + ': ' + format_with_caret(label, src))
         i = i + 1

   def _cmd_body(self, cmd, rest, ctx, env):
      """body fn  show expanded body of fn"""
      if not rest:
         print('Usage: body <fn>')
         return
      fn_name = rest.strip()
      try:
         fn = env.lookup(fn_name)
      except SchemeUnboundError:
         print(fn_name + ' is not defined.')
         return
      if not is_closure(fn):
         print(fn_name + ' is not a user-defined procedure.')
         return
      params    = as_closure_params(fn)
      rest_name = as_closure_rest_name(fn)
      body      = as_closure_body(fn)
      # Build parameter list display string.
      if rest_name is not None and len(params) == 0:
         params_str = rest_name
      elif rest_name is not None:
         params_str = '(' + ' '.join(params) + ' . ' + rest_name + ')'
      elif params:
         params_str = '(' + ' '.join(params) + ')'
      else:
         params_str = '()'
      print('Body of ' + fn_name + ' ' + params_str + ':')
      forms = body
      while is_cons(forms):
         print('  ' + Debugger._ann_fmt(forms.car, {}, 2))
         forms = forms.cdr

   def _cmd_c(self, cmd, rest, ctx, env):
      """c  continue execution to next breakpoint"""
      pass   # handled directly in _prompt_loop

   def _cmd_e(self, cmd, rest, ctx, env):
      """e expr  evaluate expr in current environment"""
      if rest:
         self.safe_eval(ctx, env, rest)

   def _cmd_h(self, cmd, rest, ctx, env):
      """h [cmd]  show help for all commands or a specific command"""
      if rest:
         base   = rest.strip()
         method = self._commands.get(base)
         if method is not None and method.__doc__:
            entries = Debugger._parse_help_entries(method.__doc__)
            if entries:
               Debugger._print_help_entries(entries)
            else:
               print(method.__doc__.strip())
         else:
            print('No help for "' + rest + '".')
         return
      entries = []
      i = 0
      while i < len(self._help_methods):
         method = self._help_methods[i][1]
         if method.__doc__:
            these = Debugger._parse_help_entries(method.__doc__)
            j = 0
            while j < len(these):
               entries.append(these[j])
               j = j + 1
         i = i + 1
      Debugger._print_help_entries(entries)

   def _cmd_i(self, cmd, rest, ctx, env):
      """i expr  evaluate and interactively inspect the result"""
      if rest:
         self.safe_inspect(ctx, env, rest)

   def _cmd_n(self, cmd, rest, ctx, env):
      """n  step over (skip deeper sub-expressions)"""
      pass   # handled directly in _prompt_loop

   def _cmd_o(self, cmd, rest, ctx, env):
      """o  step out (continue until current function returns)"""
      pass   # handled directly in _prompt_loop

   def _cmd_q(self, cmd, rest, ctx, env):
      """q / quit  exit the debugger"""
      pass   # handled directly in _prompt_loop

   def _cmd_rd(self, cmd, rest, ctx, env):
      """rd expr  run expr with debugging active
      rd       re-run the current rd expression"""
      if ctx._debugging and self._last_rd_expr is not None:
         raise _RestartRd()
      if not rest:
         expr = self._last_rd_expr
         if expr is None:
            print('No previous rd expression.')
            return
      else:
         expr = rest
         self._last_rd_expr = expr
      ctx._debugging = True
      ctx._update_instrumented()
      try:
         while True:
            try:
               self.debug_eval(ctx, env, expr)
               break
            except _RestartRd:
               print('Restarting: ' + expr)
      finally:
         ctx._debugging = False
         ctx._update_instrumented()
         self._step_hook  = None
         self._current_K  = None

   def _cmd_s(self, cmd, rest, ctx, env):
      """s  step into the next expression"""
      pass   # handled directly in _prompt_loop

   def _cmd_v(self, cmd, rest, ctx, env):
      """v [n]       show local variables (n limits scope depth)
      v name ...  show specific named variables"""
      if rest == '':
         Debugger._print_scoped_locals(env)
      elif rest.isdigit():
         Debugger._print_scoped_locals(env, int(rest))
      else:
         Debugger._print_named_vars(env, rest.split())

   def _cmd_w(self, cmd, rest, ctx, env):
      """w [*]          show watch list (numbered)
      w expr ...     add watches (variables or expressions)
      w- expr|# ...  remove from watch list
      w- *           clear watch list"""

      if cmd == 'w-':
         if rest == '':
            print('Usage: w- <expr-or-number> or w- *')
         elif rest == '*':
            self.watch_list = []
            print('Watch list cleared.')
         elif rest.isdigit():
            idx = int(rest)
            if idx < 1 or idx > len(self.watch_list):
               print('No watch #' + str(idx) + '.  Use w to list watches.')
            else:
               removed = self.watch_list.pop(idx - 1)
               print('Removed watch: ' + removed)
         else:
            for canonical in Debugger._parse_watch_args(rest):
               if canonical in self.watch_list:
                  self.watch_list.remove(canonical)
               else:
                  print(canonical + ' not in watch list.')
            if self.watch_list:
               i = 0
               while i < len(self.watch_list):
                  print('  ' + str(i + 1) + ': ' + self.watch_list[i])
                  i = i + 1
            else:
               print('Watch list cleared.')
         return

      # cmd == 'w'
      if rest == '' or rest == '*':
         if self.watch_list:
            i = 0
            while i < len(self.watch_list):
               print('  ' + str(i + 1) + ': ' + self.watch_list[i])
               i = i + 1
         else:
            print('Watch list is empty.')
      else:
         for canonical in Debugger._parse_watch_args(rest):
            if canonical not in self.watch_list:
               self.watch_list.append(canonical)
         print('Watching: ' + ', '.join(self.watch_list))

   # ── Command dispatch ─────────────────────────────────────────────────

   def _dispatch(self, line, ctx, env):
      """Route line to a command handler via _commands dict.
      Returns True if handled."""
      word   = Debugger._cmd_word(line)
      rest   = Debugger._cmd_rest(line)
      method = self._commands.get(word)
      if method is not None:
         method(word, rest, ctx, env)
         return True
      return False

   # ── Internal helpers ─────────────────────────────────────────────────

   def _prune_stale_inner(self, env):
      """Remove inner breakpoints whose function has been redefined."""
      if env is None or not self._inner_targets:
         return
      stale = []
      for nid in self._inner_targets:
         entry   = self._inner_targets[nid]
         fn_name = entry[3]
         fn_obj  = entry[4]
         try:
            current_fn = env.lookup(fn_name)
         except SchemeUnboundError:
            current_fn = None
         if current_fn is not fn_obj:
            stale.append([nid, entry[0]])
      i = 0
      while i < len(stale):
         nid     = stale[i][0]
         display = stale[i][1]
         del self._inner_targets[nid]
         self._disabled.discard(display)
         print('  (stale breakpoint ' + display + ' removed)')
         i = i + 1

   def _set_inner_breakpoint(self, spec, cond, env):
      """Resolve and set an inner breakpoint from a spec like fn:call:2."""
      parts = spec.split(':')
      if len(parts) == 2:
         fn_name   = parts[0]
         call_name = parts[1]
         index     = 1
      elif len(parts) == 3:
         fn_name   = parts[0]
         call_name = parts[1]
         if parts[2] == '?':
            index = '?'
         elif parts[2] == '*':
            index = '*'
         else:
            try:
               index = int(parts[2])
            except ValueError:
               print('Invalid index: ' + parts[2])
               return
            if index < 1:
               print('Index must be >= 1.')
               return
      else:
         print('Invalid breakpoint spec: ' + spec)
         return

      if env is None:
         print('Cannot resolve inner breakpoint without an environment.')
         return
      try:
         fn = env.lookup(fn_name)
      except SchemeUnboundError:
         print(fn_name + ' is not defined.')
         return
      if not is_closure(fn):
         print(fn_name + ' is not a user-defined procedure.')
         return

      body = as_closure_body(fn)

      # Interactive selection mode.
      if index == '?':
         calls = Debugger._collect_calls(body, call_name)
         if not calls:
            print('No calls to ' + call_name + ' in ' + fn_name + '.')
            return
         annotations = {}
         k = 0
         while k < len(calls):
            annotations[id(calls[k])] = '[' + str(k + 1) + ']'
            k = k + 1
         print('Body of ' + fn_name + ' - calls to ' + call_name + ' numbered:')
         forms = body
         while is_cons(forms):
            Debugger._print_annotated(forms.car, annotations, indent=2)
            forms = forms.cdr
         try:
            choice = self.input_fn('Breakpoint at #? ').strip()
         except (EOFError, KeyboardInterrupt):
            print()
            print('Set breakpoint cancelled.')
            return
         if choice == '':
            print('Set breakpoint cancelled.')
            return
         indices = []
         for tok in choice.split():
            try:
               n = int(tok)
            except ValueError:
               print('Invalid selection: ' + tok)
               return
            if n < 1 or n > len(calls):
               print('Selection out of range: ' + str(n) + ' (1-' + str(len(calls)) + ').')
               return
            indices.append(n)
         k = 0
         while k < len(indices):
            n       = indices[k]
            target  = calls[n - 1]
            display = fn_name + ':' + call_name + ':' + str(n)
            self._inner_targets[id(target)] = [display, cond, target, fn_name, fn]
            self._disabled.discard(display)
            if cond is None:
               print('Breakpoint set on ' + display + '.')
            else:
               print('Breakpoint set on ' + display + ' :when ' + cond)
            k = k + 1
         return

      # Set-all mode.
      if index == '*':
         calls = Debugger._collect_calls(body, call_name)
         if not calls:
            print('No calls to ' + call_name + ' in ' + fn_name + '.')
            return
         k = 0
         while k < len(calls):
            target  = calls[k]
            display = fn_name + ':' + call_name + ':' + str(k + 1)
            self._inner_targets[id(target)] = [display, cond, target, fn_name, fn]
            self._disabled.discard(display)
            if cond is None:
               print('Breakpoint set on ' + display + '.')
            else:
               print('Breakpoint set on ' + display + ' :when ' + cond)
            k = k + 1
         return

      # Single-index mode.
      target = Debugger._find_nth_call(body, call_name, index)
      if target is None:
         print('No call to ' + call_name + ' at index ' + str(index) + ' in ' + fn_name + '.')
         return
      display = fn_name + ':' + call_name + ':' + str(index)
      self._inner_targets[id(target)] = [display, cond, target, fn_name, fn]
      self._disabled.discard(display)
      if cond is None:
         print('Breakpoint set on ' + display + '.')
      else:
         print('Breakpoint set on ' + display + ' :when ' + cond)

   def print_watch(self, env, ctx):
      """Print current watch list values.  Called at every breakpoint and step."""
      if not self.watch_list:
         return
      saved_hook      = self._step_hook
      self._step_hook = None
      try:
         from pyscheme.Parser  import parse
         from pyscheme.Expander import expand
         from pyscheme.Analyzer import analyze
         print('  [watch]')
         i = 0
         while i < len(self.watch_list):
            entry = self.watch_list[i]
            try:
               forms  = parse(entry, '<watch>')
               result = NIL_VALUE
               j = 0
               while j < len(forms):
                  form   = expand(forms[j])
                  analyze(form, {})
                  result = ctx.lEval(env, form)
                  j = j + 1
               print('    ' + entry + ' = ' + pretty_print(result))
            except Exception as ex:
               print('    ' + entry + ' = %%% ' + str(ex))
            i = i + 1
      finally:
         self._step_hook = saved_hook

   # ── Eval helpers ─────────────────────────────────────────────────────

   def safe_eval(self, ctx, env, source):
      """Evaluate source in env, printing result.  Suppresses step hook."""
      from pyscheme.Parser   import parse
      from pyscheme.Expander import expand
      from pyscheme.Analyzer import analyze
      saved_hook      = self._step_hook
      self._step_hook = None
      try:
         forms  = parse(source, '<debug>')
         result = NIL_VALUE
         i = 0
         while i < len(forms):
            form   = expand(forms[i])
            analyze(form, {})
            result = ctx.lEval(env, form)
            i = i + 1
         print('==> ' + pretty_print(result))
      except Exception as ex:
         print('%%% ' + str(ex))
      finally:
         self._step_hook = saved_hook

   def safe_inspect(self, ctx, env, source):
      """Evaluate source, then interactively inspect.  Suppresses step hook."""
      from pyscheme.Parser     import parse
      from pyscheme.Expander   import expand
      from pyscheme.Analyzer   import analyze
      from pyscheme.primitives.debug import _run_inspect
      saved_hook      = self._step_hook
      self._step_hook = None
      try:
         forms  = parse(source, '<debug>')
         result = NIL_VALUE
         i = 0
         while i < len(forms):
            form   = expand(forms[i])
            analyze(form, {})
            result = ctx.lEval(env, form)
            i = i + 1
         _run_inspect(result, ctx.outStrm, self.input_fn)
      except Exception as ex:
         print('%%% ' + str(ex))
      finally:
         self._step_hook = saved_hook

   def debug_eval(self, ctx, env, source):
      """Evaluate source with breakpoints active (no step hook suppression)."""
      from pyscheme.Parser   import parse
      from pyscheme.Expander import expand
      from pyscheme.Analyzer import analyze
      try:
         forms  = parse(source, '<debug>')
         result = NIL_VALUE
         i = 0
         while i < len(forms):
            form   = expand(forms[i])
            analyze(form, {})
            result = ctx.lEval(env, form)
            i = i + 1
         print('==> ' + pretty_print(result))
      except _RestartRd:
         raise
      except Exception as ex:
         print('%%% ' + str(ex))

   # ── Unified prompt loop ───────────────────────────────────────────────

   def _prompt_loop(self, ctx, env, depth=None, step_hook=None):
      """Unified debug> prompt.

      depth=None      : main debugger REPL (supports q/quit, rd)
      depth=int       : breakpoint hit (s/n/o create a StepHook)
      step_hook!=None : stepping mode (s/n/o reconfigure existing hook)

      Returns: 'step', 'continue', 'abort', or 'quit'.
      May raise _RestartRd."""
      in_execution = depth is not None

      self._swap_history_in()
      try:
         while True:
            try:
               line = self.input_fn('debug> ').strip()
            except EOFError:
               print()
               if in_execution:
                  return 'abort'
               return 'quit'
            except KeyboardInterrupt:
               print()
               if in_execution:
                  return 'abort'
               continue

            if line == '':
               if in_execution:
                  return 'step'
               continue

            if self._rl and line:
               self._rl.add_history(line)

            if line == 'q' or line == 'quit':
               if in_execution:
                  return 'abort'
               return 'quit'

            if line == 's':
               if not in_execution:
                  print('Not in execution.  Use rd to run.')
                  continue
               if step_hook is None:
                  self._step_hook = StepHook()
               return 'step'

            if line == 'n':
               if not in_execution:
                  print('Not in execution.  Use rd to run.')
                  continue
               if step_hook is not None:
                  step_hook._skip_until_depth = depth
               else:
                  hook = StepHook()
                  hook._skip_until_depth = depth
                  self._step_hook = hook
               return 'step'

            if line == 'o':
               if not in_execution:
                  print('Not in execution.  Use rd to run.')
                  continue
               if depth == 0:
                  print('Already at top level.')
                  continue
               if step_hook is not None:
                  step_hook._skip_until_depth = depth - 1
               else:
                  hook = StepHook()
                  hook._skip_until_depth = depth - 1
                  self._step_hook = hook
               return 'step'

            if line == 'c':
               if not in_execution:
                  print('Not in execution.  Use rd to run.')
                  continue
               return 'continue'

            if line == 'abort':
               if not in_execution:
                  print('Not in execution.')
                  continue
               return 'abort'

            try:
               handled = self._dispatch(line, ctx, env)
            except Exception as e:
               print('%%% internal debugger error: ' + str(e))
               continue
            if not handled:
               print('Unknown command.  Type h for help.')
      finally:
         self._swap_history_out()

   # ── CEK loop entry point ─────────────────────────────────────────────

   def on_expr(self, C, E, K, ctx):
      """Called by the CEK loop for every cons-cell expression before dispatch.
      Handles breakpoint checking and stepping.  Returns normally to continue
      evaluation, or raises SchemeRuntimeError to abort."""

      # ── Breakpoint check ──
      bp_fired  = False
      bp_name   = ''
      cond_src  = None
      has_bp    = self.breakpoints or self._inner_targets
      has_bo    = bool(self.break_on)

      if has_bp or has_bo:
         if is_cons(C):
            cid = id(C)
            if cid in self._inner_targets:
               entry        = self._inner_targets[cid]
               display_name = entry[0]
               if display_name not in self._disabled:
                  bp_fired = True
                  bp_name  = display_name
                  cond_src = entry[1]
            if not bp_fired and is_symbol(C.car):
               sym_name = as_symbol(C.car)
               if sym_name in self.breakpoints and sym_name not in self._disabled:
                  cond_src = self.breakpoints[sym_name]
                  bp_name  = sym_name
                  bp_fired = True
               elif has_bo and sym_name in self.break_on:
                  cond_src = None
                  bp_name  = sym_name + ' [break-on]'
                  bp_fired = True

      if bp_fired and cond_src is not None:
         # Evaluate condition; suppress step hook to avoid recursion.
         saved_hook      = self._step_hook
         self._step_hook = None
         try:
            from pyscheme.Parser   import parse
            from pyscheme.Expander import expand
            from pyscheme.Analyzer import analyze
            forms  = parse(cond_src, '<cond>')
            result = NIL_VALUE
            i = 0
            while i < len(forms):
               form   = expand(forms[i])
               analyze(form, {})
               result = ctx.lEval(E, form)
               i = i + 1
            from pyscheme.Evaluator import isFalse
            if isFalse(result) or result is NIL_VALUE:
               bp_fired = False
         except Exception:
            bp_fired = False
         finally:
            self._step_hook = saved_hook

      if bp_fired:
         depth  = len(K)
         indent = '  ' * min(depth, 20)
         print('\n*** Breakpoint: ' + bp_name + ' ***')
         print(indent + pretty_print(C))
         self.print_watch(E, ctx)
         self._current_K = K
         result = self._prompt_loop(ctx, E, depth)
         if result == 'abort':
            self._step_hook = None
            raise SchemeRuntimeError('Aborted from breakpoint.')
         return   # step hook (if created) fires on the next expression

      # ── Step check ──
      sh = self._step_hook
      if sh is not None:
         depth = len(K)

         if sh._skip_until_depth is not None:
            if depth > sh._skip_until_depth:
               return
            sh._skip_until_depth = None

         if sh._step_over_first:
            sh._step_over_first  = False
            sh._skip_until_depth = depth
            indent = '  ' * min(depth, 20)
            print(indent + pretty_print(C))
            self.print_watch(E, ctx)
            return

         indent = '  ' * min(depth, 20)
         print(indent + pretty_print(C))
         self.print_watch(E, ctx)
         self._current_K = K
         action = self._prompt_loop(ctx, E, depth, step_hook=sh)
         if action == 'continue':
            self._step_hook = None
         elif action == 'abort':
            self._step_hook = None
            raise SchemeRuntimeError('Stepped execution aborted.')

   # ── Main debugger REPL ───────────────────────────────────────────────

   def run_debugger_repl(self, ctx, env):
      """Run the debug> REPL.  Returns NIL_VALUE."""
      print('\n*** Debugger ***\nType h for command help.')
      if self.breakpoints:
         print('Breakpoints:')
         bp_names = list(self.breakpoints.keys())
         bp_names.sort()
         i = 0
         while i < len(bp_names):
            name = bp_names[i]
            cond = self.breakpoints[name]
            if cond is None:
               print('  ' + name)
            else:
               print('  ' + name + '  :when ' + cond)
            i = i + 1
      if self.watch_list:
         print('Watching: ' + ', '.join(self.watch_list))
      if self.break_on:
         bo_names = list(self.break_on)
         bo_names.sort()
         print('Break-on: ' + ', '.join(bo_names))
      print()

      while True:
         result = self._prompt_loop(ctx, env)
         if result == 'quit':
            break

      return NIL_VALUE
