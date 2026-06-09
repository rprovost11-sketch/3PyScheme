"""Function call tracer.

Passive (non-interactive) call tracing: prints an entry line when a traced
function is called and an exit line when it returns.

Usage from Scheme:
    (trace fib)            ; enable tracing for fib
    (untrace fib)          ; disable tracing for fib
    (untrace)              ; disable all tracing
    (trace)                ; list currently traced functions

Output format (depth right-justified to 2 digits, 2-space indent per level):
     0: (fib 5)
     1:   (fib 4)
     1:   fib returned 3

The Tracer instance is owned by the interpreter Context (set by Interpreter)
and is reset on interpreter reboot.  The _ctx back-reference is used to
update the single _instrumented gate in Context whenever the traced set changes.

C-port target: C++17 (relaxed ruleset, but no f-strings, no comprehensions,
no tuple-unpacking LHS, no *args/**kwargs, no decorators).
"""

from pyscheme.PrettyPrinter import pretty_print


class Tracer:
    def __init__(self):
        self._fns_to_trace = set()
        self._depth = 0
        self._active = False
        self._ctx = None   # back-reference; set by Interpreter after construction

    def reset(self):
        """Clear all tracing state.  Called on interpreter reboot."""
        self._fns_to_trace = set()
        self._depth = 0
        self._active = False

    # ---- Named function tracing ----------------------------------------

    def add_fn(self, name):
        """Register a function name for tracing.  Called by (trace fn)."""
        self._fns_to_trace.add(name)
        self._active = True
        if self._ctx is not None:
            self._ctx._update_instrumented()

    def remove_fn(self, name):
        """Unregister a function name.  Called by (untrace fn)."""
        self._fns_to_trace.discard(name)
        self._active = bool(self._fns_to_trace)
        if self._ctx is not None:
            self._ctx._update_instrumented()

    def remove_all(self):
        """Unregister all names.  Called by (untrace) with no args."""
        self._fns_to_trace.clear()
        self._active = False
        if self._ctx is not None:
            self._ctx._update_instrumented()

    def get_fns(self):
        """Return a frozenset of currently traced function names."""
        return frozenset(self._fns_to_trace)

    # ---- Hooks ---------------------------------------------------------

    def trace_enter(self, name, args, depth, outStrm):
        """Print an entry line if name is in the traced set.
        Returns True if printed (caller uses this to decide whether to
        increment depth and push FRAME_TRACE_EXIT)."""
        if name not in self._fns_to_trace:
            return False
        indent = ''
        i = 0
        while i < depth:
            indent = indent + '  '
            i = i + 1
        arg_parts = []
        i = 0
        while i < len(args):
            arg_parts.append(pretty_print(args[i]))
            i = i + 1
        if arg_parts:
            arg_str = ''
            i = 0
            while i < len(arg_parts):
                if i > 0:
                    arg_str = arg_str + ' '
                arg_str = arg_str + arg_parts[i]
                i = i + 1
            line = str(depth).rjust(2) + ': ' + indent + \
                '(' + name + ' ' + arg_str + ')'
        else:
            line = str(depth).rjust(2) + ': ' + indent + '(' + name + ')'
        print(line, file=outStrm)
        return True

    def trace_exit(self, name, val, depth, outStrm):
        """Print an exit line."""
        indent = ''
        i = 0
        while i < depth:
            indent = indent + '  '
            i = i + 1
        line = str(depth).rjust(2) + ': ' + indent + \
            name + ' returned ' + pretty_print(val)
        print(line, file=outStrm)
