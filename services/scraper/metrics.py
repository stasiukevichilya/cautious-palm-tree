"""Prometheus metrics for the scraper."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.watches = Counter("scraper_watches_total", "Watch scrapes", ["source", "result"],
                               registry=self.registry)
        self.scrape_seconds = Histogram("scraper_scrape_seconds", "Full scrape duration",
                                        registry=self.registry)
        self.listings = Gauge("scraper_listings", "Stored listings", ["source"], registry=self.registry)
        self.pending_alerts = Gauge("scraper_pending_alerts", "Alerts waiting for delivery",
                                    registry=self.registry)
        self.alerts = Counter("scraper_alerts_total", "Alerts raised", ["kind"], registry=self.registry)
        self.last_scrape_timestamp = Gauge("scraper_last_scrape_timestamp_seconds",
                                           "Unix time of the last scrape start", registry=self.registry)
