"""Engine tests: a real libtorrent session and a local seeder, no external network."""
import asyncio
import os
import random
import tempfile
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

    async def test_bad_magnet(self):
        for bad in ("http://example.com/a.torrent", "magnet:?xt=urn:btih:zz", ""):
            with self.assertRaises(MagnetError):
                await self.engine.metadata(bad, 1)

    async def test_remove_while_downloading_counts_cancelled(self):
        meta, key = await self.wait_metadata(self.magnet)
        # 8 KiB/s makes 512 KiB take ~64 s, so the download is guaranteed to be in flight.
        self.engine.torrents[key]["handle"].set_download_limit(8 * 1024)
        await self.engine.start_download(self.magnet, 20, peer=self.peer)
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


if __name__ == "__main__":
    unittest.main()
