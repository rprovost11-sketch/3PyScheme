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
      self.debugger      = None    # Debugger instance; set by Interpreter
      self.tracer        = None    # Tracer instance; set by Interpreter
      self.lEval         = None    # (env, expr) -> Value; set by Interpreter
      self.wind_stack    = []      # active dynamic-wind frames; (before, after) tuples
      self.handler_stack = []      # active with-exception-handler handlers
      self.shadow_stack  = []      # call-stack entries for error backtraces

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
