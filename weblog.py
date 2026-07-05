"""In-memory ring buffer of recent log lines for the web UI activity panel.

The same records still go to stdout through the normal logging handlers;
this just keeps the most recent lines so the browser can poll them. Watching
the DICOM conversation line by line is part of the teaching value, so the
panel shows exactly what the console shows.
"""

import logging
import threading
from collections import deque

import config

_lines = deque(maxlen=config.LOG_BUFFER_LINES)
_lock = threading.Lock()
_seq = 0  # monotonically increasing id so the browser can fetch only new lines


class RingBufferHandler(logging.Handler):
    def emit(self, record):
        global _seq
        try:
            msg = self.format(record)
        except Exception:
            return
        with _lock:
            _seq += 1
            _lines.append({"seq": _seq, "level": record.levelname, "text": msg})


def get_lines(after_seq=0):
    """Return buffered lines with seq greater than after_seq."""
    with _lock:
        return [dict(line) for line in _lines if line["seq"] > after_seq]


def install(formatter):
    handler = RingBufferHandler()
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
