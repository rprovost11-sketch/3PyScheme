"""Entry point for the Scheme interpreter.

Run with:
    python -m pyscheme                    # interactive REPL in CWD
    python -m pyscheme <directory>        # REPL rooted at <directory>
    python -m pyscheme <file.scm>         # evaluate the file, then exit
"""
import os
import sys

from pyscheme.Interpreter import Interpreter
from pyscheme.Listener    import Listener


def main():
   argc = len(sys.argv)
   if argc > 2:
      print('Usage: python -m pyscheme [<directory> | <scheme-source-file>]',
            file=sys.stderr)
      sys.exit(2)

   interp = Interpreter()

   if argc == 2:
      target = sys.argv[1]
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

   listener = Listener(
      interp,
      testdir='testing',
      language='pyscheme',
      version='0.2.6',
      author='Ron Provost/Longo',
      project='https://github.com/rprovost11/pyscheme',
   )
   try:
      listener.readEvalPrintLoop()
   except StopIteration:
      pass


if __name__ == '__main__':
   main()
