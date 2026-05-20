"""Idle scheduler for the opt-in AFM loop."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class AFMScheduler:
    """Daemon-adjacent idle scheduler.

    It observes last activity and only runs a pass when the loop is enabled,
    the daemon has been idle long enough, and no long-running operation is
    registered. The compile callable is injected to keep orchestration out of
    this module.
    """

    def __init__(self, config, compile_pass: Callable[[str], dict], interval_seconds: float = 5.0):
        self.config = config
        self.compile_pass = compile_pass
        self.interval_seconds = interval_seconds
        self.last_activity_ts = time.time()
        self.long_running_ops = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: dict[str, float] = {}

    def mark_activity(self) -> None:
        self.last_activity_ts = time.time()

    def begin_long_op(self) -> None:
        self.long_running_ops += 1

    def end_long_op(self) -> None:
        self.long_running_ops = max(0, self.long_running_ops - 1)

    def enabled(self) -> bool:
        return bool((getattr(self.config, "afm_loop_schedule", {}) or {}).get("enabled"))

    def due_pass(self) -> Optional[str]:
        schedule = getattr(self.config, "afm_loop_schedule", {}) or {}
        if not schedule.get("enabled"):
            return None
        if time.time() - self.last_activity_ts < int(schedule.get("idle_seconds", 300)):
            return None
        if self.long_running_ops > 0:
            return None
        passes = schedule.get("passes") or {}
        overdue = []
        now = time.time()
        for name, cfg in passes.items():
            interval = int((cfg or {}).get("interval_seconds", 24 * 3600))
            last = self._last_run.get(name, 0.0)
            if now - last >= interval:
                overdue.append((now - last - interval, name))
        if not overdue:
            return None
        overdue.sort(reverse=True)
        return overdue[0][1]

    def tick(self) -> Optional[dict]:
        name = self.due_pass()
        if not name:
            return None
        result = self.compile_pass(name)
        self._last_run[name] = time.time()
        return result

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.interval_seconds):
                self.tick()

        self._thread = threading.Thread(target=loop, name="afm-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
