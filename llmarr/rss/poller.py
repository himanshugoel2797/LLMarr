"""Periodic background loop that auto-grabs new episodes of monitored series and
imports completed downloads into Plex. Runs as an asyncio task for the lifetime
of the server; interval and enable/auto-grab are read from config on every tick
so LLM config changes take effect without a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..core import App

log = logging.getLogger("llmarr.rss")


class RssPoller:
    def __init__(self, app: App):
        self.app = app
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_run: float | None = None
        self.last_result: dict | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="llmarr-rss-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        log.info("RSS poller started")
        while not self._stop.is_set():
            rss = self.app.config.rss
            interval = max(1, rss.interval_minutes) * 60
            if rss.enabled and self.app.config.prowlarr.url:
                await self.poll_once()
            # Sleep in small slices so config/interval changes and shutdown are responsive.
            waited = 0
            while waited < interval and not self._stop.is_set():
                await asyncio.sleep(min(5, interval - waited))
                waited += 5
                if self.app.config.rss.interval_minutes * 60 != interval:
                    break  # interval changed — recompute
        log.info("RSS poller stopped")

    async def poll_once(self) -> dict:
        try:
            result = await self.app.rss_poll()
            # Opportunistically progress/import active downloads too.
            result["imports"] = await self.app.refresh_downloads()
            self.last_result = result
            self.last_run = time.time()
            if result["grabbed"]:
                log.info("RSS auto-grabbed %d release(s)", len(result["grabbed"]))
            return result
        except Exception as exc:  # noqa: BLE001
            log.exception("RSS poll failed")
            self.last_result = {"error": str(exc)}
            self.last_run = time.time()
            return self.last_result

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "enabled": self.app.config.rss.enabled,
            "auto_grab": self.app.config.rss.auto_grab,
            "interval_minutes": self.app.config.rss.interval_minutes,
            "last_run": self.last_run,
            "last_result": self.last_result,
        }
