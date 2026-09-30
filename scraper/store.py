"""SQLite storage: watches, listings, price history, alerts, scrape runs, tracked items."""
import collections
import json
import statistics
import time

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK (source IN ('kufar', 'onliner')),
    ref TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    filter TEXT NOT NULL DEFAULT '[]',
    exclude TEXT NOT NULL DEFAULT '[]',
    params TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    owner INTEGER,
    UNIQUE (source, ref));
CREATE TABLE IF NOT EXISTS subscriptions (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    tg_id INTEGER NOT NULL,
    PRIMARY KEY (watch_id, tg_id));
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    watch_id INTEGER REFERENCES watches(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    price REAL,
    currency TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    images TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL DEFAULT '',
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    UNIQUE (source, external_id));
CREATE INDEX IF NOT EXISTS listings_watch ON listings(watch_id, last_seen_at DESC);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS history_listing ON price_history(listing_id, at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS alerts_pending ON alerts(delivered, id);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1,
    checked_at REAL);
CREATE INDEX IF NOT EXISTS items_owner ON items(owner);
CREATE TABLE IF NOT EXISTS scrapes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER REFERENCES watches(id) ON DELETE SET NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    seen INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]');
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.db = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The database is the only persistent state; keep it private like the tgbot one.
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        # Databases from older versions pick the missing pieces up here.
        rows = await self.db.execute_fetchall("PRAGMA table_info(watches)")
        columns = [row["name"] for row in rows]
        if "exclude" not in columns:
            await self.db.execute("ALTER TABLE watches ADD COLUMN exclude TEXT NOT NULL DEFAULT '[]'")
        if "owner" not in columns:
            await self.db.execute("ALTER TABLE watches ADD COLUMN owner INTEGER")
        # The old alerts table restricted kind with a CHECK; rebuild it without the constraint.
        sql = (await self.db.execute_fetchall("SELECT sql FROM sqlite_master WHERE name = 'alerts'"))[0]["sql"]
        if sql and "CHECK" in sql:
            await self.db.execute("PRAGMA foreign_keys = OFF")
            await self.db.executescript(
                """CREATE TABLE alerts_new (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                       kind TEXT NOT NULL,
                       detail TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       delivered INTEGER NOT NULL DEFAULT 0);
                   INSERT INTO alerts_new SELECT * FROM alerts;
                   DROP TABLE alerts;
                   ALTER TABLE alerts_new RENAME TO alerts;
                   CREATE INDEX IF NOT EXISTS alerts_pending ON alerts(delivered, id);""")
            await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA synchronous = NORMAL")
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # watches
    async def seed_watches(self, watches):
        """Environment/seed values only fill gaps; later edits come from the API."""
        for watch in watches:
            await self.db.execute(
                "INSERT OR IGNORE INTO watches (source, ref, label, filter, exclude, params, active, owner) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (watch["source"], watch["ref"], watch.get("label", ""),
                 json.dumps(watch.get("filter", [])), json.dumps(watch.get("exclude", [])),
                 json.dumps(watch.get("params", {})), 1 if watch.get("active", True) else 0,
                 watch.get("owner")))
        await self.db.commit()

    async def watches(self):
        rows = await self.db.execute_fetchall(
            "SELECT w.*, (SELECT COUNT(*) FROM listings l WHERE l.watch_id = w.id) AS listings "
            "FROM watches w ORDER BY w.source, w.ref")
        result = []
        for row in rows:
            record = dict(row)
            record["filter"] = json.loads(record["filter"])
            record["exclude"] = json.loads(record["exclude"])
            record["params"] = json.loads(record["params"])
            record["active"] = bool(record["active"])
            result.append(record)
        return result

    async def add_watch(self, watch):
        cursor = await self.db.execute(
            """INSERT INTO watches (source, ref, label, filter, exclude, params, active, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, ref) DO UPDATE SET label = excluded.label, filter = excluded.filter,
               exclude = excluded.exclude, params = excluded.params, active = excluded.active,
               owner = excluded.owner""",
            (watch["source"], watch["ref"], watch.get("label", ""),
             json.dumps(watch.get("filter", [])), json.dumps(watch.get("exclude", [])),
             json.dumps(watch.get("params", {})), 1 if watch.get("active", True) else 0,
             watch.get("owner")))
        await self.db.commit()
        rows = await self.db.execute_fetchall("SELECT * FROM watches WHERE id = ?", (cursor.lastrowid,))
        if not rows and cursor.lastrowid is None:  # upsert with no new row: find the existing one
            rows = await self.db.execute_fetchall(
                "SELECT * FROM watches WHERE source = ? AND ref = ?", (watch["source"], watch["ref"]))
        return dict(rows[0]) if rows else None

    async def delete_watch(self, watch_id):
        cursor = await self.db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        await self.db.commit()
        return cursor.rowcount == 1

    async def set_watch_active(self, watch_id, active):
        cursor = await self.db.execute("UPDATE watches SET active = ? WHERE id = ?", (1 if active else 0, watch_id))
        await self.db.commit()
        return cursor.rowcount == 1

    async def watches_for_owner(self, tg_id):
        """Watches the user owns or is subscribed to; 'subscribed' marks the second case."""
        rows = await self.db.execute_fetchall(
            """SELECT w.*, s.tg_id IS NOT NULL AS subscribed,
                      (SELECT COUNT(*) FROM listings l WHERE l.watch_id = w.id) AS listings
               FROM watches w LEFT JOIN subscriptions s ON s.watch_id = w.id AND s.tg_id = ?
               WHERE w.owner = ? OR s.tg_id IS NOT NULL ORDER BY w.id""", (tg_id, tg_id))
        result = []
        for row in rows:
            record = dict(row)
            record["filter"] = json.loads(record["filter"])
            record["exclude"] = json.loads(record["exclude"])
            record["params"] = json.loads(record["params"])
            record["active"] = bool(record["active"])
            result.append(record)
        return result

    async def subscribe(self, watch_id, tg_id):
        rows = await self.db.execute_fetchall("SELECT id FROM watches WHERE id = ?", (watch_id,))
        if not rows:
            return False
        await self.db.execute("INSERT OR IGNORE INTO subscriptions (watch_id, tg_id) VALUES (?, ?)",
                              (watch_id, tg_id))
        await self.db.commit()
        return True

    async def unsubscribe(self, watch_id, tg_id):
        cursor = await self.db.execute("DELETE FROM subscriptions WHERE watch_id = ? AND tg_id = ?",
                                       (watch_id, tg_id))
        await self.db.commit()
        return cursor.rowcount == 1

    # listings
    async def upsert_listing(self, source, external_id, watch_id, title, price, currency,
                             region, condition, description, images, url):
        """Insert or refresh one ad. Returns (record, is_new, old_price, new_price)."""
        now = time.time()
        rows = await self.db.execute_fetchall(
            "SELECT * FROM listings WHERE source = ? AND external_id = ?", (source, external_id))
        if rows:
            record = dict(rows[0])
            await self.db.execute(
                """UPDATE listings SET watch_id = COALESCE(?, watch_id), title = ?, price = ?, currency = ?,
                   region = ?, condition = ?, description = ?, images = ?, url = ?, last_seen_at = ?
                   WHERE id = ?""",
                (watch_id, title, price, currency, region, condition, description,
                 json.dumps(images, ensure_ascii=False), url, now, record["id"]))
            await self.db.commit()
            return record, False, record["price"], price
        cursor = await self.db.execute(
            """INSERT INTO listings (source, external_id, watch_id, title, price, currency, region, condition,
               description, images, url, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, external_id, watch_id, title, price, currency, region, condition, description,
             json.dumps(images, ensure_ascii=False), url, now, now))
        listing_id = cursor.lastrowid
        if price is not None:
            await self.db.execute(
                "INSERT INTO price_history (listing_id, price, currency, at) VALUES (?, ?, ?, ?)",
                (listing_id, price, currency, now))
        await self.db.commit()
        rows = await self.db.execute_fetchall("SELECT * FROM listings WHERE id = ?", (listing_id,))
        return dict(rows[0]), True, None, price

    async def add_price_point(self, listing_id, price, currency):
        await self.db.execute(
            "INSERT INTO price_history (listing_id, price, currency, at) VALUES (?, ?, ?, ?)",
            (listing_id, price, currency, time.time()))
        await self.db.commit()

    async def listings(self, watch_id=None, limit=200):
        query = "SELECT * FROM listings"
        args: list = []
        if watch_id is not None:
            query += " WHERE watch_id = ?"
            args.append(watch_id)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        args.append(limit)
        rows = await self.db.execute_fetchall(query, args)
        result = []
        for row in rows:
            record = dict(row)
            record["images"] = json.loads(record["images"])
            result.append(record)
        return result

    # items (product links with per-owner price tracking)
    async def add_item(self, owner, url):
        cursor = await self.db.execute("INSERT INTO items (owner, url) VALUES (?, ?)", (owner, url))
        await self.db.commit()
        return await self.item(cursor.lastrowid)

    async def item(self, item_id):
        rows = await self.db.execute_fetchall(
            """SELECT i.*, l.title, l.price, l.currency, l.region
               FROM items i LEFT JOIN listings l ON l.id = i.listing_id WHERE i.id = ?""", (item_id,))
        return dict(rows[0]) if rows else None

    async def items(self, owner=None):
        query = ("SELECT i.*, l.title, l.price, l.currency, l.region "
                 "FROM items i LEFT JOIN listings l ON l.id = i.listing_id")
        args: list = []
        if owner is not None:
            query += " WHERE i.owner = ?"
            args.append(owner)
        query += " ORDER BY i.id"
        rows = await self.db.execute_fetchall(query, args)
        return [dict(row) for row in rows]

    async def active_items(self):
        rows = await self.db.execute_fetchall("SELECT * FROM items WHERE active = 1 ORDER BY id")
        return [dict(row) for row in rows]

    async def item_owner(self, item_id):
        rows = await self.db.execute_fetchall("SELECT owner FROM items WHERE id = ?", (item_id,))
        return rows[0]["owner"] if rows else None

    async def delete_item(self, item_id):
        cursor = await self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        await self.db.commit()
        return cursor.rowcount == 1

    async def set_item_active(self, item_id, active):
        await self.db.execute("UPDATE items SET active = ? WHERE id = ?", (1 if active else 0, item_id))
        await self.db.commit()

    async def touch_item(self, item_id, listing_id=None):
        if listing_id is not None:
            await self.db.execute("UPDATE items SET listing_id = ?, checked_at = ? WHERE id = ?",
                                  (listing_id, time.time(), item_id))
        else:
            await self.db.execute("UPDATE items SET checked_at = ? WHERE id = ?", (time.time(), item_id))
        await self.db.commit()

    async def price_medians(self, watch_id, since):
        """Median price per currency over price observations since `since`, for one watch."""
        rows = await self.db.execute_fetchall(
            """SELECT ph.price, ph.currency
               FROM price_history ph JOIN listings l ON l.id = ph.listing_id
               WHERE l.watch_id = ? AND ph.at >= ?""",
            (watch_id, since))
        by_currency = {}
        for row in rows:
            by_currency.setdefault(row["currency"], []).append(row["price"])
        return {currency: statistics.median(values) for currency, values in by_currency.items()}

    # alerts
    async def add_alert(self, listing_id, kind, detail):
        await self.db.execute(
            "INSERT INTO alerts (listing_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (listing_id, kind, detail, time.time()))
        await self.db.commit()

    async def pending_alerts(self, max_age=7 * 86400):
        """Undelivered alerts with the listing link and recipients, oldest first.
        Recipients are Telegram ids; [None] means the stack admins."""
        rows = await self.db.execute_fetchall(
            """SELECT a.id, a.kind, a.detail, a.created_at, l.source, l.title, l.url, l.watch_id, l.id AS listing_id
               FROM alerts a JOIN listings l ON l.id = a.listing_id
               WHERE a.delivered = 0 AND a.created_at >= ? ORDER BY a.id""",
            (time.time() - max_age,))
        alerts = [dict(row) for row in rows]
        if not alerts:
            return []
        watch_owners = {row["id"]: row["owner"] for row in
                        await self.db.execute_fetchall("SELECT id, owner FROM watches WHERE owner IS NOT NULL")}
        subs = collections.defaultdict(set)
        for row in await self.db.execute_fetchall("SELECT watch_id, tg_id FROM subscriptions"):
            subs[row["watch_id"]].add(row["tg_id"])
        item_owners = {row["listing_id"]: row["owner"] for row in
                       await self.db.execute_fetchall(
                           "SELECT i.listing_id, i.owner FROM items i WHERE i.listing_id IS NOT NULL")}
        for alert in alerts:
            recipients = set()
            if alert["source"] == "item":
                recipients.add(item_owners[alert["listing_id"]])
            elif alert["watch_id"] is not None:
                if alert["watch_id"] in watch_owners:
                    recipients.add(watch_owners[alert["watch_id"]])
                recipients.update(subs.get(alert["watch_id"], ()))
            for key in ("source", "watch_id", "listing_id"):
                del alert[key]
            alert["recipients"] = sorted(recipients) if recipients else [None]
        return alerts

    async def mark_delivered(self, alert_ids):
        if not alert_ids:
            return
        await self.db.executemany("UPDATE alerts SET delivered = 1 WHERE id = ?",
                                  [(alert_id,) for alert_id in alert_ids])
        await self.db.commit()

    async def alerts(self, limit=50):
        rows = await self.db.execute_fetchall(
            """SELECT a.id, a.kind, a.detail, a.created_at, a.delivered, l.title, l.url
               FROM alerts a JOIN listings l ON l.id = a.listing_id
               ORDER BY a.id DESC LIMIT ?""", (limit,))
        return [dict(row) for row in rows]

    # scrape runs
    async def record_scrape(self, watch_id, started, finished, seen, new_count, errors):
        await self.db.execute(
            "INSERT INTO scrapes (watch_id, started_at, finished_at, seen, new, errors) VALUES (?, ?, ?, ?, ?, ?)",
            (watch_id, started, finished, seen, new_count, json.dumps(errors, ensure_ascii=False)))
        await self.db.commit()

    async def scrapes(self, limit=20):
        rows = await self.db.execute_fetchall(
            """SELECT s.*, w.label AS watch_label
               FROM scrapes s LEFT JOIN watches w ON w.id = s.watch_id
               ORDER BY s.id DESC LIMIT ?""", (limit,))
        result = []
        for row in rows:
            record = dict(row)
            record["errors"] = json.loads(record["errors"])
            result.append(record)
        return result

    async def counts(self):
        """(listings per source, pending alerts)."""
        rows = await self.db.execute_fetchall("SELECT source, COUNT(*) AS n FROM listings GROUP BY source")
        pending, = (await self.db.execute_fetchall("SELECT COUNT(*) AS n FROM alerts WHERE delivered = 0"))[0]
        return {row["source"]: row["n"] for row in rows}, pending
