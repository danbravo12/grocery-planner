"""A small in-process run queue: a fixed number of worker threads, a bounded FIFO behind them.

Runs are background work that must not depend on any client being connected, so each one is
handed to a worker thread; a closed browser tab changes nothing. Single process only (the
known limitation the README names for the per-minute limiter too)."""

import logging
import threading
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)


class RunQueue:
    def __init__(self, work: Callable[[str], None], workers: int = 2, max_queued: int = 10):
        self._work, self._workers, self._max_queued = work, workers, max_queued
        self._waiting: deque[str] = deque()
        self._running: set[str] = set()
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._stopping = False

    # ---- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        with self._cond:
            self._stopping = False
        for n in range(self._workers):
            t = threading.Thread(target=self._loop, name=f"run-worker-{n}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop taking new work; running runs are left to finish or be swept as stale."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    # ---- queueing ---------------------------------------------------------------------------

    def has_room(self) -> bool:
        with self._cond:
            return self._load() < self._workers + self._max_queued

    def _load(self) -> int:
        return len(self._waiting) + len(self._running)  # call with the lock held

    def try_enqueue(self, run_id: str) -> bool:
        """False when the queue is full. Atomic, so concurrent requests cannot overfill it."""
        with self._cond:
            if self._stopping:
                return False
            if self._load() >= self._workers + self._max_queued:
                return False
            self._waiting.append(run_id)
            self._cond.notify()
            return True

    def position(self, run_id: str) -> int | None:
        """1 is next to start; 0 is running; None is not known to this process."""
        with self._cond:
            if run_id in self._running:
                return 0
            try:
                return list(self._waiting).index(run_id) + 1
            except ValueError:
                return None

    def counts(self) -> tuple[int, int]:
        with self._cond:
            return len(self._running), len(self._waiting)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._waiting and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                run_id = self._waiting.popleft()
                self._running.add(run_id)
            try:
                self._work(run_id)
            except Exception:
                log.exception("worker failed on run %s", run_id)
            finally:
                with self._cond:
                    self._running.discard(run_id)
