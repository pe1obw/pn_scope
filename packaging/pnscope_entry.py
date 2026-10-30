"""Entry point for the frozen (PyInstaller) executable."""
import sys

from pnscope.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
