"""Interpreter context.

A small per-evaluation object that carries cross-cutting state the
evaluator and primitives may need.  Right now it only carries
`outStrm` - the text stream that display/print/help primitives write
to - but any future IO or tracing state that doesn't belong on the env
chain would live here too.

The Interpreter owns a single Context instance whose outStrm is
swapped per eval() call when a caller wants to capture output (for
example, the test runner).
"""

import sys


class Context:
   def __init__(self, outStrm=None):
      if outStrm is None:
         self.outStrm = sys.stdout
      else:
         self.outStrm = outStrm
      self._debugging    = False   # True while rd is running
      self._instrumented = False   # gate: any debug tooling active
      self.interactive   = False   # True in a live REPL session (set by the
                                   # Listener); (exit) aborts to the prompt
                                   # instead of terminating the process.  See
                                   # primitives/meta.py:_prim_exit.
      self.debugger      = None    # Debugger instance; set by Interpreter
      self.tracer        = None    # Tracer instance; set by Interpreter
      self.lEval         = None    # (env, expr) -> Value; set by Interpreter
      self.wind_stack    = []      # active dynamic-wind frames; (before, after) tuples
      self.handler_stack = []      # active with-exception-handler handlers
      self.shadow_stack  = []      # call-stack entries for error backtraces
      self.timeout_at    = 0.0    # monotonic deadline; 0 = disabled
      self._timeout_step = 0      # iteration counter for throttled timeout checks
      # Continuation-escape bookkeeping.  Each cek_eval activation claims a
      # unique id from eval_id_counter and publishes it in current_eval_id
      # while running; continuations record the id of the loop that captured
      # them.  eval_id_stack holds the ids of every cek_eval still live on the
      # Python call stack, so an invocation can tell whether a continuation's
      # owning loop is still an ancestor (escape) or has already returned
      # (re-entry).  See Evaluator.ContinuationEscape / _continuation_must_escape.
      self.eval_id_counter = 0
      self.current_eval_id = 0
      self.eval_id_stack   = []

   def _update_instrumented(self):
      """Recompute the single instrumentation gate after any flag change."""
      active = self._debugging
      if self.tracer is not None:
         if self.tracer._active:
            active = True
      self._instrumented = active

   def write(self, text):
      """Convenience: write raw text to the current output stream."""
      self.outStrm.write(text)
