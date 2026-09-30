"""libtorrent engine: magnet metadata and downloads to the destination directory.

libtorrent 2.1.1 specifics (verified): a magnet added with `paused` alone still
downloads data, so metadata-only mode is `paused | default_dont_download`. A
confirmed download re-adds the torrent fresh as a full download: resuming a
metadata-only torrent leaves the metadata peer connection half-open, while a
fresh torrent gets a clean connection (DHT/trackers rediscover peers in prod).
Completion of a downloading torrent is `state in (finished, seeding)`; a
metadata-only torrent is "finished" immediately, so the registry tracks states
itself. The registry key is the hex hash from the magnet string (btih, else
btmh), because in 2.1 `add_torrent_params.info_hash` of a v2 torrent is not the
btih hash.
"""
import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

import libtorrent as lt

log = logging.getLogger("torrent")

STATES = lt.torrent_status.states
METADATA_ONLY_FLAGS = lt.torrent_flags.paused | lt.torrent_flags.default_dont_download
MAX_FILES_SHOWN = 50
DHT_ROUTERS = (("router.bittorrent.com", 6881), ("router.ubuntu.com", 6881),
               ("dht.transmissionbt.com", 6881))


class MagnetError(ValueError):
    """The link is not a parseable magnet."""


class MagnetTimeout(Exception):
    """Metadata could not be fetched in time (no seeders found)."""


class Duplicate(Exception):
    """The torrent is already downloading or seeding."""


def magnet_hash(magnet):
    """Hex hash from a magnet link: btih (40 hex) or btmh (64 hex); None if absent."""
    if not isinstance(magnet, str) or not magnet.strip().lower().startswith("magnet:?"):
        return None
    for prefix, length in (("btih", 40), ("btmh", 64)):
        match = re.search(r"xt=urn:" + prefix + r":([0-9a-fA-F]{" + str(length) + r"})", magnet)
        if match:
            return match.group(1).lower()
    return None


def human_size(value):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024


class Engine:
    """Owns the libtorrent session and the registry of known torrents.

    Registry states: metadata (added, data not requested), downloading,
    seeding (finished, still uploading), failed.
    """

    def __init__(self, destination, metrics, notify=None, tick_seconds=5.0):
        self.destination = Path(destination)
        self.metrics = metrics
        self.notify = notify  # async (text) -> None
        self.tick_seconds = tick_seconds
        self.session = lt.session(lt.default_settings())
        for host, port in DHT_ROUTERS:
            self.session.add_dht_router(host, port)
        self.torrents = {}  # hash -> record
        self._last_payload = {"download": 0, "upload": 0}
        self._task = None
        self._stop = asyncio.Event()

    async def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self):
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Stats tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    # metadata
    async def metadata(self, magnet, timeout, peer=None):
        key = magnet_hash(magnet)
        if key is None:
            raise MagnetError("это не magnet-ссылка")
        record = self.torrents.get(key)
        if record and record["state"] in ("downloading", "seeding"):
            self.metrics.metadata_fetches.labels("duplicate").inc()
            raise Duplicate(key)
        if record and record["state"] != "metadata":
            self._drop(key, record)
            del self.torrents[key]
            record = None
        if record is None:
            try:
                atp = lt.parse_magnet_uri(magnet.strip())
            except Exception:
                raise MagnetError("это не magnet-ссылка") from None
            atp.save_path = str(self.destination)
            atp.flags |= METADATA_ONLY_FLAGS
            handle = self.session.add_torrent(atp)
            if peer:
                # connect_peer must follow add_torrent without yielding to the event
                # loop: a delayed connect makes the peer connection not survive resume.
                handle.connect_peer(peer)
            record = {"handle": handle, "state": "metadata", "name": "",
                      "size": 0, "num_files": 0, "files": [], "started": None}
            self.torrents[key] = record
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                self._give_up_metadata(key, record, "timeout")
            try:
                if record["handle"].status().has_metadata:
                    break
            except Exception:  # removed by /torrent-del while waiting
                self._give_up_metadata(key, record, "error")
            await asyncio.sleep(0.5)
        try:
            info = record["handle"].get_torrent_info()
        except Exception:
            self._give_up_metadata(key, record, "error")
        files = info.files()
        record["name"] = info.name()
        record["size"] = info.total_size()
        record["num_files"] = info.num_files()
        record["files"] = [(files.file_path(i), files.file_size(i))
                           for i in range(min(info.num_files(), MAX_FILES_SHOWN))]
        self.metrics.metadata_fetches.labels("ok").inc()
        return self._meta(key, record)

    def _give_up_metadata(self, key, record, result):
        """Drop a metadata-only torrent that no longer can be waited on."""
        self._drop(key, record)
        self.torrents.pop(key, None)
        self.metrics.metadata_fetches.labels(result).inc()
        raise MagnetTimeout(key)

    def _meta(self, key, record):
        return {"info_hash": key, "name": record["name"], "size": record["size"],
                "num_files": record["num_files"],
                "files": [[path, size] for path, size in record["files"]]}

    # downloads
    async def start_download(self, magnet, timeout, peer=None):
        key = magnet_hash(magnet)
        if key is None:
            raise MagnetError("это не magnet-ссылка")
        record = self.torrents.get(key)
        if record and record["state"] in ("downloading", "seeding"):
            raise Duplicate(key)
        if record and (record["state"] != "metadata" or not record["handle"].status().has_metadata):
            self._drop(key, record)
            del self.torrents[key]
        if key not in self.torrents:
            await self.metadata(magnet, timeout, peer)
        record = self.torrents[key]
        if record["state"] in ("downloading", "seeding"):
            raise Duplicate(key)
        # The metadata-only torrent is discarded and re-added as a full download. The
        # metadata peer connection is half-open on resume(), so a fresh torrent gets a
        # clean peer connection (in production DHT/trackers rediscover peers anyway).
        self.session.remove_torrent(record["handle"])
        atp = lt.parse_magnet_uri(magnet.strip())
        atp.save_path = str(self.destination)
        handle = self.session.add_torrent(atp)
        if peer:
            handle.connect_peer(peer)
        record["handle"] = handle
        record["state"] = "downloading"
        record["started"] = time.time()
        self.metrics.downloads.labels("started").inc()
        return self._info(key, record)

    def list(self):
        return [self._info(key, record) for key, record in self.torrents.items()]

    def _info(self, key, record):
        try:
            st = record["handle"].status()
            return {"info_hash": key, "name": record["name"] or st.name, "state": record["state"],
                    "progress": st.progress, "download_speed": st.download_payload_rate,
                    "upload_speed": st.upload_payload_rate, "size": record["size"]}
        except Exception:
            return {"info_hash": key, "name": record["name"], "state": record["state"],
                    "progress": 0.0, "download_speed": 0, "upload_speed": 0, "size": record["size"]}

    def remove(self, key):
        record = self.torrents.get(key)
        if not record:
            raise KeyError(key)
        if record["state"] == "downloading":
            self.metrics.downloads.labels("cancelled").inc()
        self._drop(key, record)
        del self.torrents[key]

    def _drop(self, key, record):
        try:
            self.session.remove_torrent(record["handle"])
        except Exception:
            pass
        self.metrics.download_speed.labels(key).set(0)
        self.metrics.upload_speed.labels(key).set(0)

    # stats
    async def tick(self):
        now = time.time()
        down_sum = up_sum = active = seeding = 0
        for key, record in list(self.torrents.items()):
            handle = record["handle"]
            try:
                st = handle.status()
            except Exception:
                continue
            if not handle.is_valid():
                continue
            down_sum += st.total_payload_download
            up_sum += st.total_payload_upload
            self.metrics.download_speed.labels(key).set(st.download_payload_rate)
            self.metrics.upload_speed.labels(key).set(st.upload_payload_rate)
            if record["state"] == "downloading":
                # After resume the status briefly stays in the stale "finished" state
                # of the metadata-only phase (progress 1.0, total_wanted 0); a real
                # completion always has total_wanted_done > 0.
                if (st.state in (STATES.finished, STATES.seeding) and st.progress >= 1.0
                        and st.total_wanted_done > 0):
                    await self._finish(key, record, st, now)
                elif st.error:
                    record["state"] = "failed"
                    self.metrics.downloads.labels("failed").inc()
                    await self._notify(f"⚠️ Ошибка при скачивании «{record['name']}»: {st.error}")
            if record["state"] == "downloading":
                active += 1
            elif record["state"] == "seeding":
                seeding += 1
        self.metrics.active_downloads.set(active)
        self.metrics.seeding.set(seeding)
        # Payload counters are monotonic: a removed torrent shrinks the sums, so only
        # positive deltas are counted and the baseline never goes down.
        for label, total in (("download", down_sum), ("upload", up_sum)):
            delta = total - self._last_payload[label]
            if delta >= 0:
                if label == "download":
                    self.metrics.downloaded_bytes.inc(delta)
                else:
                    self.metrics.uploaded_bytes.inc(delta)
            self._last_payload[label] = max(total, self._last_payload[label])
        usage = shutil.disk_usage(self.destination)
        self.metrics.free_space.set(usage.free)
        self.metrics.total_space.set(usage.total)

    async def _finish(self, key, record, st, now):
        record["state"] = "seeding"
        duration = now - record["started"] if record["started"] else 0
        downloaded = st.total_payload_download
        size = st.total_download or record["size"]
        if duration > 0 and downloaded > 0:
            self.metrics.avg_speed.observe(downloaded / duration)
        if duration > 0:
            self.metrics.duration.observe(duration)
        if size:
            self.metrics.size.observe(size)
        self.metrics.downloads.labels("completed").inc()
        speed = downloaded / duration if duration > 0 else 0
        await self._notify(f"⬇️ Скачано: «{record['name']}» ({human_size(downloaded)}, средняя скорость "
                           f"{human_size(speed)}/с, {duration / 60:.0f} мин). Сидирование продолжается.")

    async def _notify(self, text):
        if not self.notify:
            return
        try:
            await self.notify(text)
        except Exception as error:
            log.warning("Cannot notify tgbot: %s", type(error).__name__)
