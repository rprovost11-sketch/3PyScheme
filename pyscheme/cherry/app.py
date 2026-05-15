"""CherryApp: main window.  Two-pane PanedWindow (editor top, REPL bottom)
plus a CWD status button at the very bottom."""

import os
import pathlib
import tkinter as tk
from tkinter import filedialog, font as tkfont

_CHERRY_DIR  = pathlib.Path.home() / '.cherry'
_STATE_PATH  = _CHERRY_DIR / 'pyscheme.json'

from pyscheme.cherry.subprocess_bridge import SubprocessBridge
from pyscheme.cherry.editor_pane       import EditorPane
from pyscheme.cherry.repl_pane         import ReplPane


class CherryApp(tk.Tk):
   def __init__(self):
      super().__init__()
      self.title('cherry - pyscheme')
      self.geometry('900x700')
      self.configure(bg='#1e1e1e')

      _CHERRY_DIR.mkdir(exist_ok=True)
      self._bridge = SubprocessBridge()
      self._build()
      self._editor.restore_state(_STATE_PATH)
      self.protocol('WM_DELETE_WINDOW', self._on_close)

   def _build(self):
      # ---- main paned area ----
      paned = tk.PanedWindow(
         self,
         orient=tk.VERTICAL,
         sashwidth=5,
         sashrelief=tk.FLAT,
         bg='#3c3c3c',
      )
      paned.pack(fill=tk.BOTH, expand=True)

      self._editor = EditorPane(paned, on_run=self._on_run, bg='#1e1e1e')
      self._repl   = ReplPane(paned, bridge=self._bridge, bg='#1e1e1e')

      paned.add(self._editor, stretch='always')
      paned.add(self._repl,   stretch='always')

      self.after(100, lambda: paned.sash_place(0, 0, self.winfo_height() // 2))
      self._paned = paned

      # ---- CWD status button ----
      self._cwd_var = tk.StringVar(value=os.getcwd())
      cwd_btn = tk.Button(
         self,
         textvariable=self._cwd_var,
         command=self._cmd_chdir,
         bg='#252526', fg='#888888',
         activebackground='#333333', activeforeground='#cccccc',
         relief=tk.FLAT,
         anchor=tk.W,
         padx=8, pady=2,
         cursor='hand2',
         font=tkfont.Font(family='Courier New', size=9),
      )
      cwd_btn.pack(side=tk.BOTTOM, fill=tk.X)

   def _cmd_chdir(self):
      path = filedialog.askdirectory(title='Change working directory',
                                     initialdir=os.getcwd())
      if path:
         os.chdir(path)
         self._cwd_var.set(os.getcwd())
         self._bridge.chdir(path)

   def _on_run(self, source):
      """Relay editor Run / Help button to the REPL."""
      self._repl.inject_source(source)

   def _on_close(self):
      self._editor.save_state(_STATE_PATH)
      self._repl.save_history()
      self._bridge.shutdown()
      self.destroy()


def main():
   app = CherryApp()
   app.mainloop()
