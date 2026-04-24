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

   def write(self, text):
      """Convenience: write raw text to the current output stream."""
      self.outStrm.write(text)
