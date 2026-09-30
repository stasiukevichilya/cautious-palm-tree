"""Background loop: scrape every interval, send the daily report once after report_hour."""
import asyncio
import logging
import time

log = logging.getLogger("scraper.scheduler")


class Scheduler:
    def __init__(self, service, interval, report_hour):
        self.service = service
        self.interval = interval
        self.report_hour = report_hour
        self.task = None
        self._stop = None
        self._last_report_day = None

    async def start(self):
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self._stop is not None:
            self._stop.set()
        if self.task is not None:
            await self.task

    async def _run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                summary = await self.service.scrape()
                log.info("Scrape done: %s", summary)
                await self._maybe_report()
            except Exception:
                log.exception("Scrape run failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(5.0, self.interval - (time.time() - started)))
            except asyncio.TimeoutError:
                pass

    async def _maybe_report(self):
        today = time.strftime("%Y-%m-%d")
        if self._last_report_day == today or time.localtime().tm_hour < self.report_hour:
            return
        try:
            if await self.service.report() is not None:
                self._last_report_day = today
        except Exception:
            log.exception("Report failed")
