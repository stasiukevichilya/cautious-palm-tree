"""Scrape orchestration: run watches, store listings, raise alerts, notify the tgbot."""
import collections
import html
import logging
import time

from adapters import ADAPTERS
from adapters.kufar import parse_item
from config import Watch
from httpclient import ScrapeError

log = logging.getLogger("scraper.service")

# More new ads in one run than this means a first run or a flood: summarize, don't spam.
SUMMARY_THRESHOLD = 50
ALERT_MAX_AGE = 7 * 86400
REPORT_MEDIAN_WINDOW = 7 * 86400


class Busy(Exception):
    pass


def format_price(price, currency):
    if price is None:
        return "цена по договорённости"
    return f"{price:.0f} {currency}"


class Service:
    def __init__(self, store, client, tgbot, metrics):
        self.store = store
        self.client = client
        self.tgbot = tgbot
        self.metrics = metrics
        self.adapters = {source: adapter(client) for source, adapter in ADAPTERS.items()}
        self.running = False

    async def close(self):
        await self.client.close()
        await self.tgbot.close()

    async def scrape(self, watch_ids=None):
        """Run the given (or all active) watches once and deliver pending alerts."""
        if self.running:
            raise Busy("a scrape is already running")
        self.running = True
        started = time.time()
        self.metrics.last_scrape_timestamp.set(started)
        summary = {"watched": 0, "seen": 0, "new": 0, "price_drops": 0, "errors": []}
        try:
            for watch in await self.store.watches():
                if not watch["active"]:
                    continue
                if watch_ids is not None and watch["id"] not in watch_ids:
                    continue
                summary["watched"] += 1
                await self.scrape_watch(watch, summary)
            if watch_ids is None:
                await self.scrape_items(summary)
            await self._deliver_alerts()
        finally:
            self.running = False
            self.metrics.scrape_seconds.observe(time.time() - started)
            by_source, pending = await self.store.counts()
            for source in ADAPTERS:
                self.metrics.listings.labels(source=source).set(by_source.get(source, 0))
            self.metrics.pending_alerts.set(pending)
        return summary

    async def scrape_watch(self, watch, summary):
        adapter = self.adapters.get(watch["source"])
        if adapter is None:
            summary["errors"].append(f"{watch['label'] or watch['ref']}: unknown source {watch['source']}")
            return
        started = time.time()
        seen = new_count = 0
        new_records, drops = [], []
        errors = []
        watch_model = Watch.model_validate(watch)
        try:
            for listing in await adapter.fetch(watch_model):
                record, is_new, old_price, new_price = await self.store.upsert_listing(
                    source=watch["source"], external_id=listing.external_id, watch_id=watch["id"],
                    title=listing.title, price=listing.price, currency=listing.currency,
                    region=listing.region, condition=listing.condition,
                    description=listing.description, images=listing.images, url=listing.url)
                seen += 1
                if is_new:
                    new_count += 1
                    new_records.append((record, listing))
                else:
                    if new_price is not None:
                        await self.store.add_price_point(record["id"], new_price, record["currency"])
                        if old_price is not None and new_price < old_price:
                            drops.append((record, old_price, new_price))
        except (ScrapeError, Exception) as error:  # one bad watch must not kill the rest
            errors.append(f"{type(error).__name__}: {error}")
            log.warning("Watch %s failed: %s: %s", watch["ref"], type(error).__name__, error)
            self.metrics.watches.labels(source=watch["source"], result="error").inc()
        else:
            self.metrics.watches.labels(source=watch["source"], result="ok").inc()
        await self.store.record_scrape(watch["id"], started, time.time(), seen, new_count, errors)

        label = watch["label"] or watch["ref"]
        if new_records:
            if len(new_records) > SUMMARY_THRESHOLD:
                priced = [l for _, l in new_records if l.price is not None]
                cheapest = min(priced, key=lambda l: l.price, default=None)
                detail = (f"«{label}»: +{len(new_records)} новых объявлений"
                          + (f", самое дешёвое: {cheapest.title} — {format_price(cheapest.price, cheapest.currency)}"
                             if cheapest else ""))
                await self.store.add_alert(new_records[0][0]["id"], "new", detail)
            else:
                for record, listing in new_records:
                    detail = (f"Новое объявление ({label}): {listing.title} — "
                              f"{format_price(listing.price, listing.currency)}"
                              + (f" ({listing.region})" if listing.region else ""))
                    await self.store.add_alert(record["id"], "new", detail)
            self.metrics.alerts.labels(kind="new").inc(len(new_records))
            summary["new"] += len(new_records)
        for record, old_price, new_price in drops:
            await self.store.add_alert(
                record["id"], "price_dropped",
                f"Снижение цены: {record['title']} — {format_price(old_price, record['currency'])} → "
                f"{format_price(new_price, record['currency'])}")
            self.metrics.alerts.labels(kind="price_dropped").inc()
        summary["price_drops"] += len(drops)
        summary["seen"] += seen
        summary["errors"].extend(f"{label}: {error}" for error in errors)

    async def _check_item(self, item):
        """Fetch the current price of one tracked product link; alert on any change. Returns the item."""
        listing = parse_item(await self.client.get_text(item["url"]))
        if listing is None:  # deleted or sold
            await self.store.set_item_active(item["id"], False)
            log.info("Item %s is gone (deleted or sold)", item["url"])
            return await self.store.item(item["id"])
        record, is_new, old_price, new_price = await self.store.upsert_listing(
            source="item", external_id=listing.external_id, watch_id=None, title=listing.title,
            price=listing.price, currency=listing.currency, region=listing.region, condition="",
            description="", images=[], url=item["url"])
        await self.store.touch_item(item["id"], record["id"])
        if not is_new and new_price is not None and old_price is not None and new_price != old_price:
            kind = "price_dropped" if new_price < old_price else "price_risen"
            verb = "Снижение цены" if new_price < old_price else "Повышение цены"
            await self.store.add_alert(record["id"], kind,
                f"{verb}: {record['title']} — {format_price(old_price, record['currency'])} → "
                f"{format_price(new_price, record['currency'])}")
            self.metrics.alerts.labels(kind=kind).inc()
        return await self.store.item(item["id"])

    async def scrape_items(self, summary):
        """Check all tracked product links; each owner is notified on price changes (up or down)."""
        for item in await self.store.active_items():
            try:
                await self._check_item(item)
            except (ScrapeError, Exception) as error:
                summary["errors"].append(f"item {item['url']}: {type(error).__name__}: {error}")
                log.warning("Item %s failed: %s: %s", item["url"], type(error).__name__, error)

    async def add_item(self, owner, url):
        """Check the product price now, then register the link for periodic checks."""
        listing = parse_item(await self.client.get_text(url))
        if listing is None:
            raise ScrapeError("the item page has no ad data (deleted or wrong link)")
        item = await self.store.add_item(owner, url)
        record, _, _, _ = await self.store.upsert_listing(
            source="item", external_id=listing.external_id, watch_id=None, title=listing.title,
            price=listing.price, currency=listing.currency, region=listing.region, condition="",
            description="", images=[], url=url)
        await self.store.touch_item(item["id"], record["id"])
        return await self.store.item(item["id"])

    async def check_item(self, item_id):
        item = await self.store.item(item_id)
        if not item or not item["active"]:
            raise LookupError(f"item {item_id} not found")
        return await self._check_item(item)

    async def _deliver_alerts(self):
        """Deliver each undelivered alert to its owners/subscribers (None = the admins)."""
        alerts = await self.store.pending_alerts(ALERT_MAX_AGE)
        if not alerts:
            return
        by_user = collections.defaultdict(list)
        for alert in alerts:
            for tg_id in alert["recipients"]:
                by_user[tg_id].append(alert)
        delivered = set()
        try:
            for tg_id, group in by_user.items():
                lines = ["Объявления: мониторинг рынка БУ"]
                lines += [f"- {alert['detail']}\n  {alert['url']}" for alert in group]
                if tg_id is None:
                    await self.tgbot.notify("\n".join(lines))
                else:
                    await self.tgbot.notify_user(tg_id, "\n".join(lines))
                delivered.update(alert["id"] for alert in group)
        except Exception as error:
            # Undelivered alerts stay pending and are retried on the next scrape.
            log.warning("Could not deliver alerts to %s user(s): %s", len(by_user), type(error).__name__)
        if delivered:
            await self.store.mark_delivered(sorted(delivered))

    async def report(self):
        """Cheapest listings per active watch plus the 7-day median price; listings below the
        median are bolded (the tgbot sends the text as HTML). Only shared watches are reported;
        per-user queries notify their owners directly."""
        lines = ["Отчёт: БУ рынок (актуальные цены)", time.strftime("%d.%m.%Y %H:%M")]
        sent = False
        week_ago = time.time() - REPORT_MEDIAN_WINDOW
        for watch in await self.store.watches():
            if not watch["active"] or watch["owner"] is not None:
                continue
            rows = await self.store.listings(watch_id=watch["id"], limit=500)
            medians = await self.store.price_medians(watch["id"], week_ago)
            label = watch["label"] or watch["ref"]
            if not rows:
                lines.append(f"\n{label}: объявлений не найдено")
                sent = True
                continue
            priced = sorted((r for r in rows if r["price"] is not None), key=lambda r: r["price"])
            lines.append(f"\n{label}: {len(rows)} объявл., {len(priced)} с ценой")
            for currency, median in sorted(medians.items()):
                lines.append(f"  медиана за 7 дней: {median:.0f} {currency}")
            for row in priced[:5]:
                region = f", {row['region']}" if row["region"] else ""
                line = html.escape(f"{row['price']:.0f} {row['currency']} — {row['title'][:70]}{region}")
                median = medians.get(row["currency"])
                if median is not None and row["price"] < median:
                    line = f"<b>{line}</b>"
                lines.append(f"  {line}")
                lines.append(f"    {html.escape(row['url'])}")
            sent = True
        if not sent:
            return None
        text = "\n".join(lines)
        await self.tgbot.notify(text)
        return text
