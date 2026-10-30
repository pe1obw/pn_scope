"""Run instrument work in a background thread; report back through Qt signals."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QThread, Signal

from ..analyzers.base import Cancelled


class Task(QThread):
    """fn(progress, partial, cancel_event) -> result.  progress(msg, fraction 0..1);
    partial(obj) for intermediate results. Exactly one of done/failed/cancelled fires."""
    progress = Signal(str, float)
    partial = Signal(object)
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            r = self.fn(self.progress.emit, self.partial.emit, self.cancel_event)
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001 - reported to the user
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}")
        else:
            self.done.emit(r)
