"""cherry - tkinter GUI frontend for pyscheme.

Launch with:
    python cherry.py
or:
    python -m pyscheme.cherry
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from pyscheme.cherry.app import main
main()
