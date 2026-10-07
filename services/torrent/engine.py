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
import json
import logging
import os
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

    Registry states: metadata (added, data not requested), downloading, paused
    (paused_from remembers downloading/seeding), seeding (finished, still
    uploading), failed.

    The registry is mirrored to a JSON queue file next to the downloads directory
    (same volume) so a restart re-adds the torrents and resumes them from disk.
    """

    def __init__(self, destination, metrics, notify=None, tick_seconds=5.0, metadata_ttl=1800.0,
                 queue_path=None):
        self.destination = Path(destination)
        self.queue_path = Path(queue_path) if queue_path else self.destination.parent / "queue.json"
        self.metrics = metrics
        self.notify = notify  # async (text) -> None
        self.tick_seconds = tick_seconds
        # A metadata-only torrent that nobody confirms is dropped after this many seconds
        # (the bot's confirm button expires earlier, so the engine outlives the pending UI).
        self.metadata_ttl = metadata_ttl
        self.session = lt.session(lt.default_settings())
        for host, port in DHT_ROUTERS:
            self.session.add_dht_router(host, port)
        self.torrents = {}  # hash -> record
        self._last_payload = {"download": 0, "upload": 0}
        self._task = None
        self._stop = asyncio.Event()

    async def start(self):
        self._stop.clear()
        await self.load_queue()  # active downloads survive a restart
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def load_queue(self):
        """Re-add the registry from the queue file after a restart.

        The magnet string is stored with each record, which is all libtorrent
        needs to recreate the torrent; data already on disk is picked up by hash.
        Failed records are skipped on purpose: the user just re-sends the magnet.
        """
        if not self.queue_path.exists():
            return
        try:
            data = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Could not read the torrent queue %s; starting empty", self.queue_path,
                        exc_info=True)
            return
        restored = 0
        for key, record in data.items():
            if not isinstance(record, dict) or \
                    record.get("state") not in ("metadata", "downloading", "seeding", "paused"):
                continue
            try:
                if magnet_hash(record["magnet"]) != key:
                    log.warning("Torrent queue: hash mismatch for %s, skipping", key)
                    continue
                atp = lt.parse_magnet_uri(record["magnet"])
                atp.save_path = str(self.destination)
                if record["state"] == "metadata":
                    atp.flags |= METADATA_ONLY_FLAGS
                handle = self.session.add_torrent(atp)
                peer = record.get("peer")
                if peer:
                    # Must follow add_torrent without yielding: a delayed connect is
                    # silently dropped (same quirk as in metadata()/start_download()).
                    handle.connect_peer((str(peer[0]), int(peer[1])))
                if record["state"] == "paused":
                    # Re-added downloads start immediately; keep the pre-restart pause.
                    handle.pause()
            except Exception:
                log.warning("Could not restore torrent %s from the torrent queue", key, exc_info=True)
                continue
            record["handle"] = handle
            self.torrents[key] = record
            restored += 1
        if restored:
            log.info("Restored %d torrent(s) from the queue", restored)
            self._save_queue()

    def _save_queue(self):
        """Persist the registry (without handles) via an atomic file swap.
        Best effort: losing the queue costs at most a re-send of the magnet."""
        try:
            fields = ("magnet", "peer", "state", "name", "size", "num_files", "files",
                      "started", "created", "paused_from")
            # .get(): queue files from older versions lack the newer fields.
            payload = {key: {field: record.get(field) for field in fields}
                       for key, record in self.torrents.items()}
            tmp = self.queue_path.with_name(self.queue_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.queue_path)
        except Exception:
            log.warning("Could not save the torrent queue to %s", self.queue_path, exc_info=True)

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
            self._save_queue()
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
            record = {"handle": handle, "magnet": magnet.strip(), "peer": peer,
                      "state": "metadata", "name": "",
                      "size": 0, "num_files": 0, "files": [], "started": None,
                      "created": time.time()}
            self.torrents[key] = record
            self._save_queue()
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
        self._save_queue()
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
        if peer:
            record["peer"] = peer  # kept for the restart path; an old hint is harmless
        self.metrics.downloads.labels("started").inc()
        self._save_queue()
        return self._info(key, record)

    def list(self):
        return [self._info(key, record) for key, record in self.torrents.items()]

    def pause(self, key):
        """Pause an active download (or seeding upload); state is persisted."""
        record = self.torrents.get(key)
        if not record:
            raise KeyError(key)
        if record["state"] not in ("downloading", "seeding"):
            raise ValueError(f"загрузка не активна (состояние: {record['state']})")
        record["paused_from"] = record["state"]
        record["state"] = "paused"
        record["handle"].pause()
        self._save_queue()
        return self._info(key, record)

    def resume(self, key):
        """Resume a paused download, restoring the state it was paused from."""
        record = self.torrents.get(key)
        if not record:
            raise KeyError(key)
        if record["state"] != "paused":
            raise ValueError(f"загрузка не на паузе (состояние: {record['state']})")
        record["state"] = record.pop("paused_from", "downloading")
        record["handle"].resume()
        self._save_queue()
        return self._info(key, record)

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
        self._save_queue()

    def _drop(self, key, record):
        try:
            self.session.remove_torrent(record["handle"])
        except Exception:
            pass
        # Remove the per-hash gauge series entirely: a zeroed series would otherwise
        # stay in the collector for every hash the service has ever seen.
        for gauge in (self.metrics.download_speed, self.metrics.upload_speed):
            try:
                gauge.remove(key)
            except KeyError:
                pass

    # stats
    async def tick(self):
        now = time.time()
        down_sum = up_sum = active = seeding = 0
        changed = False
        for key, record in list(self.torrents.items()):
            if record["state"] == "metadata" and now - record.get("created", now) > self.metadata_ttl:
                # Unconfirmed metadata fetch: drop it so DHT lookups do not run forever.
                self._drop(key, record)
                del self.torrents[key]
                self.metrics.metadata_fetches.labels("expired").inc()
                changed = True
                continue
            handle = record["handle"]
            try:
                st = handle.status()
            except Exception:
                continue
            if not handle.is_valid():
                continue
            down_sum += st.total_payload_download
            up_sum += st.total_payload_upload
            if record["state"] == "paused":
                # Report zero speeds while paused; the completion check below applies
                # only to "downloading" records, so a finished-but-paused torrent
                # switches to seeding on resume (next tick sees progress >= 1).
                self.metrics.download_speed.labels(key).set(0)
                self.metrics.upload_speed.labels(key).set(0)
                continue
            self.metrics.download_speed.labels(key).set(st.download_payload_rate)
            self.metrics.upload_speed.labels(key).set(st.upload_payload_rate)
            if record["state"] == "downloading":
                # After resume the status briefly stays in the stale "finished" state
                # of the metadata-only phase (progress 1.0, total_wanted 0); a real
                # completion always has total_wanted_done > 0.
                if (st.state in (STATES.finished, STATES.seeding) and st.progress >= 1.0
                        and st.total_wanted_done > 0):
                    await self._finish(key, record, st, now)
                    changed = True
                elif st.error:
                    record["state"] = "failed"
                    self.metrics.downloads.labels("failed").inc()
                    changed = True
                    await self._notify(f"⚠️ Ошибка при скачивании «{record['name']}»: {st.error}")
            if record["state"] == "downloading":
                active += 1
            elif record["state"] == "seeding":
                seeding += 1
        if changed:
            self._save_queue()
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
