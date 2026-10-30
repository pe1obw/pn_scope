import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from .. import __version__


def main(argv=None):
    argv = sys.argv if argv is None else argv
    pg.setConfigOptions(antialias=True)
    app = QApplication(argv)
    app.setOrganizationName("PNScope")
    app.setApplicationName("PNScope")
    app.setApplicationVersion(__version__)
    from .main_window import MainWindow
    w = MainWindow()
    w.show()
    if len(argv) > 1:
        w.open_files(argv[1:])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
