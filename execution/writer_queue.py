from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from sqlalchemy.orm import Session

from core.exceptions import WriterQueueDegradedError, WriterQueueError
from core.settings import Settings, get_settings
from data.database import get_session_factory


T = TypeVar("T")
WriteTask = Callable[[Session], T]


@dataclass(slots=True)
class QueueHealth:
    running: bool
    degraded: bool
    queue_depth: int
    last_error: str | None


@dataclass(slots=True)
class _QueueItem(Generic[T]):
    task: WriteTask[T]
    future: Future[T]
    description: str
    attempts: int = 0


class WriterQueue:
    def __init__(
        self,
        *,
        session_factory=None,
        max_retries: int = 3,
        retry_delay_sec: float = 0.1,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._max_retries = max_retries
        self._retry_delay_sec = retry_delay_sec
        self._queue: queue.Queue[_QueueItem[Any] | None] = queue.Queue()
        self._running = False
        self._degraded = False
        self._last_error: str | None = None
        self._worker: threading.Thread | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "WriterQueue":
        _ = settings or get_settings()
        return cls()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, name="writer-queue", daemon=True)
        self._worker.start()

    def stop(self, timeout: float = 5) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

    def submit(self, task: WriteTask[T], *, description: str = "") -> Future[T]:
        if self._degraded:
            raise WriterQueueDegradedError("writer queue is degraded")
        if not self._running:
            raise WriterQueueError("writer queue is not running")
        future: Future[T] = Future()
        self._queue.put(_QueueItem(task=task, future=future, description=description))
        return future

    def health(self) -> QueueHealth:
        return QueueHealth(
            running=self._running,
            degraded=self._degraded,
            queue_depth=self._queue.qsize(),
            last_error=self._last_error,
        )

    def _run(self) -> None:
        while self._running or not self._queue.empty():
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                continue
            self._execute_item(item)
            self._queue.task_done()

    def _execute_item(self, item: _QueueItem[Any]) -> None:
        while item.attempts < self._max_retries:
            item.attempts += 1
            try:
                with self._session_factory() as session:
                    with session.begin():
                        result = item.task(session)
                item.future.set_result(result)
                return
            except Exception as exc:
                self._last_error = f"{item.description or 'write task'} failed: {exc}"
                if item.attempts >= self._max_retries:
                    self._degraded = True
                    item.future.set_exception(
                        WriterQueueDegradedError(
                            f"writer queue degraded after {item.attempts} attempts: {exc}"
                        )
                    )
                    return
                time.sleep(self._retry_delay_sec)
