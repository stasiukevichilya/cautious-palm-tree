"""Engine tests: a real libtorrent session and a local seeder, no external network."""
import asyncio
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

import libtorrent as lt

from engine import Duplicate, Engine, MagnetError, MagnetTimeout, human_size, magnet_hash
from metrics import Metrics


def value(metric, labels=(), suffix=""):
    """Current value of a prometheus_client metric (or its labeled child)."""
    source = metric.labels(*labels) if labels else metric
    for family in source.collect():
        for sample in family.samples:
            if sample.name.endswith(suffix):
                return sample.value
    return None


def make_torrent(directory, name="sample.bin", size=512 * 1024):
    """Create one file in directory and its torrent. Returns (magnet, torrent_bytes, payload)."""
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(size)
    path.write_bytes(payload)
    ct = lt.create_torrent([lt.create_file_entry(name, size)])
    lt.set_piece_hashes(ct, str(directory))
    torrent_bytes = lt.bencode(ct.generate())
    ti = lt.load_torrent_buffer(torrent_bytes).ti
    magnet = "magnet:?xt=urn:btih:" + str(ti.info_hashes().v1) + "&dn=" + name
    return magnet, torrent_bytes, payload


class Seeder:
    """A separate session holding the data on 127.0.0.1."""

    def __init__(self, directory, torrent_bytes):
        self.port = 17200 + random.randint(0, 2000)
        self.session = lt.session()
        atp = lt.load_torrent_buffer(torrent_bytes)
        atp.save_path = str(directory)
        self.handle = self.session.add_torrent(atp)
        self.session.listen_on(self.port, self.port)

    async def wait_ready(self):
        states = lt.torrent_status.states
        deadline = asyncio.get_event_loop().time() + 20
        while self.handle.status().state not in (states.seeding, states.finished):
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("seeder did not become ready")
            await asyncio.sleep(0.1)

    def close(self):
        self.session.remove_torrent(self.handle)
        del self.session


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.destination = Path(self.directory.name) / "downloads"
        self.destination.mkdir()
        self.metrics = Metrics()
        self.notified = []
        # No listen override: apply_settings(listen_interfaces) drops established
        # peer connections, and the tests only need outbound connections to the seeder.
        self.engine = Engine(self.destination, self.metrics, notify=self.notify, tick_seconds=0.2)
        await self.engine.start()
        src = Path(self.directory.name) / "src"
        src.mkdir()
        self.magnet, self.torrent_bytes, self.payload = make_torrent(src, "sample.bin")
        self.seeder = Seeder(src, self.torrent_bytes)
        await self.seeder.wait_ready()

    async def asyncTearDown(self):
        await self.engine.stop()
        self.seeder.close()
        del self.engine
        self.directory.cleanup()

    async def notify(self, text):
        self.notified.append(text)

    @property
    def peer(self):
        return ("127.0.0.1", self.seeder.port)

    async def wait_metadata(self, magnet, timeout=20):
        """Fetch metadata; the engine attaches the local seeder as a peer (DHT is empty offline)."""
        key = magnet_hash(magnet)
        meta = await self.engine.metadata(magnet, timeout, peer=self.peer)
        return meta, key

    async def wait_state(self, key, state, timeout=30):
        deadline = asyncio.get_event_loop().time() + timeout
        while self.engine.torrents[key]["state"] != state:
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError(f"torrent {key} stuck in "
                                     f"{self.engine.torrents[key]['state']!r}")
            await asyncio.sleep(0.05)

    async def test_magnet_hash_extraction(self):
        expected = str(lt.load_torrent_buffer(self.torrent_bytes).ti.info_hashes().v1)
        self.assertEqual(magnet_hash(self.magnet), expected)
        self.assertEqual(magnet_hash("  " + self.magnet), expected)
        self.assertIsNone(magnet_hash("http://example.com/a.torrent"))
        self.assertIsNone(magnet_hash("magnet:?xt=urn:btih:tooshort"))
        self.assertIsNone(magnet_hash(None))
        self.assertEqual(magnet_hash("magnet:?xt=urn:btmh:" + "a" * 64), "a" * 64)
        self.assertEqual(magnet_hash(self.magnet + "&xt=urn:btmh:" + "b" * 64), expected)
        self.assertEqual(human_size(512), "512 Б")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 МБ")

    async def test_metadata_and_download_flow(self):
        meta, key = await self.wait_metadata(self.magnet)
        self.assertEqual(meta["info_hash"], key)
        self.assertEqual(meta["name"], "sample.bin")
        self.assertEqual(meta["size"], len(self.payload))
        self.assertEqual(meta["num_files"], 1)
        self.assertEqual(meta["files"], [["sample.bin", len(self.payload)]])
        self.assertEqual(value(self.metrics.metadata_fetches, ("ok",)), 1)

        info = await self.engine.start_download(self.magnet, 20, peer=self.peer)
        self.assertEqual(info["state"], "downloading")
        self.assertEqual(value(self.metrics.downloads, ("started",)), 1)
        with self.assertRaises(Duplicate):
            await self.engine.metadata(self.magnet, 5)
        self.assertEqual(value(self.metrics.metadata_fetches, ("duplicate",)), 1)
        with self.assertRaises(Duplicate):
            await self.engine.start_download(self.magnet, 5)

        await self.wait_state(key, "seeding")
        deadline = asyncio.get_event_loop().time() + 5
        while not any("Скачано" in text for text in self.notified):
            if asyncio.get_event_loop().time() > deadline:
                raise AssertionError("no completion notification")
            await asyncio.sleep(0.02)
        self.assertEqual((self.destination / "sample.bin").read_bytes(), self.payload)
        self.assertEqual(value(self.metrics.downloads, ("completed",)), 1)
        downloaded = value(self.metrics.downloaded_bytes)
        self.assertGreaterEqual(downloaded, len(self.payload))
        self.assertLess(downloaded, len(self.payload) * 1.2)  # no retransmission storm
        self.assertEqual(value(self.metrics.avg_speed, suffix="_count"), 1)
        self.assertGreater(value(self.metrics.avg_speed, suffix="_sum"), 0)
        self.assertEqual(value(self.metrics.size, suffix="_count"), 1)
        self.assertEqual(value(self.metrics.duration, suffix="_count"), 1)
        self.assertEqual(value(self.metrics.seeding), 1)
        row, = self.engine.list()
        self.assertEqual(row["info_hash"], key)
        self.assertEqual(row["state"], "seeding")
        self.assertEqual(row["progress"], 1.0)

    async def test_metadata_timeout_without_peers(self):
        lonely = "magnet:?xt=urn:btih:" + "f" * 40
        with self.assertRaises(MagnetTimeout):
            await self.engine.metadata(lonely, 1)
        self.assertEqual(value(self.metrics.metadata_fetches, ("timeout",)), 1)
        self.assertEqual(self.engine.torrents, {})

    async def test_unconfirmed_metadata_is_dropped_after_ttl(self):
        meta, key = await self.wait_metadata(self.magnet)
        self.assertEqual(self.engine.torrents[key]["state"], "metadata")
        # Backdate the record: a user who never confirmed must not keep DHT busy forever.
        self.engine.torrents[key]["created"] = time.time() - self.engine.metadata_ttl - 1
        await self.engine.tick()
        self.assertEqual(self.engine.torrents, {})
        self.assertEqual(value(self.metrics.metadata_fetches, ("expired",)), 1)

    async def test_fresh_metadata_survives_ticks(self):
        meta, key = await self.wait_metadata(self.magnet)
        for _ in range(3):
            await self.engine.tick()
        self.assertIn(key, self.engine.torrents)
        self.assertEqual(self.engine.torrents[key]["state"], "metadata")

    async def test_bad_magnet(self):
        for bad in ("http://example.com/a.torrent", "magnet:?xt=urn:btih:zz", ""):
            with self.assertRaises(MagnetError):
                await self.engine.metadata(bad, 1)

    async def test_remove_while_downloading_counts_cancelled(self):
        meta, key = await self.wait_metadata(self.magnet)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
        # start_download re-adds the torrent: the limit goes to the fresh handle, set
        # while paused so the 512 KiB cannot finish before it takes effect (~64 s at 8 KiB/s).
        handle = self.engine.torrents[key]["handle"]
        handle.pause()
        handle.set_download_limit(8 * 1024)
        handle.resume()
        self.assertEqual(self.engine.torrents[key]["state"], "downloading")
        await asyncio.sleep(0.5)
        self.assertEqual(self.engine.torrents[key]["state"], "downloading")
        self.engine.remove(key)
        self.assertEqual(self.engine.torrents, {})
        self.assertEqual(value(self.metrics.downloads, ("cancelled",)), 1)
        self.assertEqual(self.engine.list(), [])
        with self.assertRaises(KeyError):
            self.engine.remove(key)

    async def test_remove_unknown(self):
        with self.assertRaises(KeyError):
            self.engine.remove("0" * 40)

    async def test_pause_resume(self):
        meta, key = await self.wait_metadata(self.magnet)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
        self.engine.torrents[key]["handle"].set_download_limit(8 * 1024)  # keep it downloading
        await asyncio.sleep(1.0)
        self.assertEqual(self.engine.torrents[key]["state"], "downloading")
        record = self.engine.torrents[key]
        with self.assertRaises(ValueError):
            self.engine.resume(key)  # not paused yet
        info = self.engine.pause(key)
        self.assertEqual(info["state"], "paused")
        self.assertEqual(record["paused_from"], "downloading")
        with self.assertRaises(ValueError):
            self.engine.pause(key)  # already paused
        with self.assertRaises(KeyError):
            self.engine.resume("0" * 40)  # unknown hash is KeyError, not ValueError
        await asyncio.sleep(0.5)  # let a tick run
        self.assertEqual(value(self.metrics.active_downloads), 0)
        self.assertEqual(value(self.metrics.download_speed, (key,)), 0)
        info = self.engine.resume(key)
        self.assertEqual(info["state"], "downloading")
        self.assertNotIn("paused_from", record)
        record["handle"].set_download_limit(0)  # let the 512 KiB finish
        await self.wait_state(key, "seeding")
        # pause works during seeding too, and resume restores seeding
        info = self.engine.pause(key)
        self.assertEqual(info["state"], "paused")
        self.assertEqual(record["paused_from"], "seeding")
        self.engine.resume(key)
        await self.wait_state(key, "seeding")
        self.assertEqual(value(self.metrics.seeding), 1)
        self.assertEqual((self.destination / "sample.bin").read_bytes(), self.payload)

    async def test_paused_survives_restart(self):
        meta, key = await self.wait_metadata(self.magnet)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
        self.engine.torrents[key]["handle"].set_download_limit(8 * 1024)  # keep it downloading
        await asyncio.sleep(1.0)
        self.engine.pause(key)
        stored = json.loads(self.engine.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[key]["state"], "paused")
        self.assertEqual(stored[key]["paused_from"], "downloading")
        engine = await self.restart_engine()
        record = engine.torrents[key]
        self.assertEqual(record["state"], "paused")
        self.assertEqual(record["paused_from"], "downloading")
        with self.assertRaises(ValueError):
            engine.pause(key)  # still paused after the restart
        engine.resume(key)
        record["handle"].set_download_limit(0)
        deadline = asyncio.get_event_loop().time() + 30
        while engine.torrents[key]["state"] != "seeding":
            if asyncio.get_event_loop().time() > deadline:
                self.fail(f"torrent {key} stuck in {engine.torrents[key]['state']!r}")
            await asyncio.sleep(0.1)
        self.assertEqual((self.destination / "sample.bin").read_bytes(), self.payload)

    async def restart_engine(self):
        """Simulate a container restart: the process dies, so its libtorrent session
        and peer connections are gone; a fresh Engine restores from the queue file.

        A mere pause() is not enough: the old session stays alive in the process and
        its stale sockets shadow the fresh session's peer connections.
        """
        await self.engine.stop()
        session = self.engine.session
        self.engine.session = None  # the tick loop is stopped; detach the session
        self.engine.torrents.clear()  # release the handles
        session.pause()
        del session  # refcount drops to zero: the session (and its sockets) die
        engine = Engine(self.destination, Metrics(), tick_seconds=0.2)
        await engine.start()
        self.engine = engine  # asyncTearDown then drops the restored engine
        return engine

    async def test_queue_survives_restart(self):
        meta, key = await self.wait_metadata(self.magnet)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
        handle = self.engine.torrents[key]["handle"]
        handle.pause()
        handle.set_download_limit(8 * 1024)  # keep the 512 KiB file downloading for a while
        handle.resume()
        await asyncio.sleep(1.5)
        self.assertEqual(self.engine.torrents[key]["state"], "downloading")
        stored = json.loads(self.engine.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[key]["state"], "downloading")
        self.assertEqual(stored[key]["magnet"], self.magnet)
        engine = await self.restart_engine()
        self.assertIn(key, engine.torrents)
        self.assertEqual(engine.torrents[key]["state"], "downloading")
        self.assertTrue(engine.torrents[key]["handle"].is_valid())
        # The seeder hint is restored from the queue file; in prod DHT/trackers
        # rediscover peers and the re-added torrent re-fetches its metadata.
        deadline = asyncio.get_event_loop().time() + 30
        while engine.torrents[key]["state"] != "seeding":
            if asyncio.get_event_loop().time() > deadline:
                self.fail(f"restored torrent stuck in {engine.torrents[key]['state']!r}")
            await asyncio.sleep(0.1)
        self.assertEqual((self.destination / "sample.bin").read_bytes(), self.payload)

    async def test_queue_restores_pending_metadata(self):
        meta, key = await self.wait_metadata(self.magnet)
        stored = json.loads(self.engine.queue_path.read_text(encoding="utf-8"))
        self.assertEqual(stored[key]["state"], "metadata")
        engine = await self.restart_engine()
        self.assertIn(key, engine.torrents)
        self.assertEqual(engine.torrents[key]["state"], "metadata")
        self.assertTrue(engine.torrents[key]["handle"].is_valid())

    async def test_broken_queue_file_is_ignored(self):
        self.engine.queue_path.write_text("{not json", encoding="utf-8")
        engine = await self.restart_engine()
        self.assertEqual(engine.torrents, {})
        # A later mutation rewrites the file to valid JSON.
        key = magnet_hash(self.magnet)
        await engine.metadata(self.magnet, 20, peer=self.peer)
        data = json.loads(engine.queue_path.read_text(encoding="utf-8"))
        self.assertIn(key, data)

    async def test_old_queue_file_without_peer_is_restored(self):
        """Queue files written before the peer hint existed must still load,
        and re-saving them must not raise (a live restart hit exactly this)."""
        key = magnet_hash(self.magnet)
        self.engine.queue_path.write_text(json.dumps({
            key: {"magnet": self.magnet, "state": "downloading", "name": "sample.bin",
                  "size": len(self.payload), "num_files": 1, "files": [["sample.bin", len(self.payload)]],
                  "started": None, "created": 0}}), encoding="utf-8")
        engine = await self.restart_engine()
        self.assertIn(key, engine.torrents)
        self.assertIsNone(engine.torrents[key].get("peer"))
        # a later save rewrites the file in the current format (with a peer slot)
        engine._save_queue()
        self.assertIn("peer", json.loads(engine.queue_path.read_text(encoding="utf-8"))[key])

    async def test_remove_updates_queue_file(self):
        meta, key = await self.wait_metadata(self.magnet)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
        self.assertIn(key, json.loads(self.engine.queue_path.read_text(encoding="utf-8")))
        self.engine.remove(key)
        self.assertEqual(json.loads(self.engine.queue_path.read_text(encoding="utf-8")), {})


if __name__ == "__main__":
    unittest.main()
