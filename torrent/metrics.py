"""Prometheus metrics for the torrent service."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.downloaded_bytes = Counter("torrent_downloaded_bytes_total",
                                        "Payload bytes downloaded", registry=self.registry)
        self.uploaded_bytes = Counter("torrent_uploaded_bytes_total",
                                      "Payload bytes uploaded (seeding)", registry=self.registry)
        self.downloads = Counter("torrent_downloads_total", "Downloads", ["result"],
                                 registry=self.registry)
        self.metadata_fetches = Counter("torrent_metadata_fetches_total", "Metadata fetches",
                                        ["result"], registry=self.registry)
        self.download_speed = Gauge("torrent_download_speed_bytes_per_second",
                                    "Current download speed per torrent", ["info_hash"],
                                    registry=self.registry)
        self.upload_speed = Gauge("torrent_upload_speed_bytes_per_second",
                                  "Current upload speed per torrent", ["info_hash"],
                                  registry=self.registry)
        self.active_downloads = Gauge("torrent_active_downloads",
                                      "Torrents downloading now", registry=self.registry)
        self.seeding = Gauge("torrent_seeding", "Finished torrents seeding",
                             registry=self.registry)
        # One observation per finished download: its average speed (bytes / duration).
        # The dashboard shows the mean of the period and the median via histogram_quantile.
        self.avg_speed = Histogram("torrent_download_avg_speed_bytes_per_second",
                                   "Average speed of one finished download",
                                   buckets=(1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9),
                                   registry=self.registry)
        self.duration = Histogram("torrent_download_duration_seconds",
                                  "Duration of finished downloads",
                                  buckets=(30, 60, 300, 600, 1800, 3600, 10800, 86400),
                                  registry=self.registry)
        self.size = Histogram("torrent_download_size_bytes", "Size of finished downloads",
                              buckets=(1e6, 1e8, 1e9, 1e10, 1e11, 1e12),
                              registry=self.registry)
        self.free_space = Gauge("torrent_destination_free_bytes",
                                "Free bytes in the destination directory",
                                registry=self.registry)
        self.total_space = Gauge("torrent_destination_total_bytes",
                                 "Total bytes of the destination filesystem",
                                 registry=self.registry)
