"""Kufar listings (kufar.by): full-text search (?query=...) and category pages.
Primary source: __NEXT_DATA__.listing.ads; rendered ad cards are the fallback.
Search pages paginate via listing.pagination tokens (&cursor=<token>); category
pages via ?page=N. Prices in __NEXT_DATA__ are in kopecks (1/100 of a ruble)."""
import json
import logging
import re

from .base import Adapter, Listing

log = logging.getLogger("scraper.kufar")

BASE = "https://kufar.by"
PAGE_SIZE = 30
THUMBS = "https://rms.kufar.by/v1/list_thumbs_2x/"


def parse_next_data(html):
    """The SSR listing state (ads, total), or None when the page has no __NEXT_DATA__."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return (data.get("props", {}).get("initialState", {}).get("listing")) or None


def card_price(text):
    """'4 500 р.' -> 4500.0; 'Договорная' and the like -> None."""
    digits = re.sub(r"[^\d.,]", "", text.replace(" ", ""))
    if not digits or digits in (".", ","):
        return None
    return float(digits.replace(",", "."))


def parse_cards(html):
    """Fallback: rendered ad cards; stable anchors are data-testid, not the hashed classes."""
    listings = []
    for tag, body in re.findall(
            r'(<a[^>]*data-testid="kufar(?:-vip)?-ad"[^>]*>)(.*?)</section>', html, re.S):
        href = re.search(r'href="([^"]+)"', tag)
        url = href.group(1) if href else ""
        match = re.search(r"/vi/(?:[a-z0-9-]+/)?(\d+)", url) or re.search(r"/item/(\d+)", url)
        if not match:
            continue

        def text(pattern):
            found = re.search(pattern, body, re.S)
            return re.sub(r"<[^>]+>", "", found.group(1)).strip() if found else ""

        title = text(r"<h2[^>]*>(.*?)</h2>")
        if not title:
            continue
        listings.append(Listing(
            external_id=match.group(1),
            title=title,
            price=card_price(text(r'data-testid="card-price"[^>]*>(.*?)</span>')),
            currency="BYN",
            url=url,
            region=text(r'class="[^"]*__info__region"[^>]*>(.*?)</div>'),
        ))
    return listings


ITEM_RE = re.compile(r"^https?://(?:www\.)?kufar\.by/item/(\d+)$")


def item_url(url):
    """Normalized kufar item url, or None when the link is not a kufar ad page."""
    match = ITEM_RE.match((url or "").strip())
    return f"https://www.kufar.by/item/{match.group(1)}" if match else None


def parse_item(html):
    """The ad from an item page (__NEXT_DATA__.adView.data.initial), or None."""
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    view = data.get("props", {}).get("initialState", {}).get("adView") or {}
    if view.get("error"):
        return None
    ad = (view.get("data") or {}).get("initial") or {}
    ad_id = ad.get("ad_id")
    if not ad_id:
        return None
    price, currency = None, ""
    byn = (ad.get("price_byn") or "").strip()
    usd = (ad.get("price_usd") or "").strip()
    if byn and byn != "0":
        price, currency = float(byn) / 100, "BYN"
    elif usd and usd != "0":
        price, currency = float(usd) / 100, "USD"
    region = ", ".join(dict.fromkeys(
        param["vl"] for param in (ad.get("ad_parameters") or [])
        if param.get("p") in ("region", "area") and param.get("vl")))
    return Listing(
        external_id=str(ad_id),
        title=(ad.get("subject") or "").strip(),
        price=price,
        currency=currency,
        url=ad.get("ad_link") or f"https://www.kufar.by/item/{ad_id}",
        region=region,
    )


def next_token(listing):
    """Token of the next search page (label 'next' in listing.pagination), or None."""
    for entry in listing.get("pagination") or []:
        if entry.get("label") == "next" and entry.get("token"):
            return entry["token"]
    return None


def page_url(ref, page):
    sep = "&" if "?" in ref else "?"
    return f"{BASE}{ref}{sep}page={page}"


class KufarAdapter(Adapter):
    source = "kufar"

    async def fetch(self, watch):
        ref = watch.ref if watch.ref.startswith("/") else "/" + watch.ref
        max_pages = int(watch.params.get("max_pages", 40))
        search = "query=" in ref
        result = []
        url = BASE + ref
        for page in range(1, max_pages + 1):
            html = await self.client.get_text(url)
            listing = parse_next_data(html) or {}
            parsed_ads = [p for p in (self._parse_ad(ad) for ad in listing.get("ads") or []) if p]
            if not parsed_ads:
                log.warning("Kufar %s page %s: no ads in __NEXT_DATA__, using card fallback", ref, page)
                parsed_ads = parse_cards(html)
            for parsed in parsed_ads:
                if watch.matches(parsed.title, parsed.description):
                    result.append(parsed)
            if not parsed_ads:
                break
            if search:
                token = next_token(listing)
                if not token:
                    break
                url = f"{BASE}{ref}&cursor={token}"
            else:
                total = int(listing.get("total") or 0)
                if (total and page * PAGE_SIZE >= total) or len(parsed_ads) < PAGE_SIZE:
                    break
                url = page_url(ref, page + 1)
        return result

    def _parse_ad(self, ad):
        title = (ad.get("subject") or "").strip()
        if not title:
            return None
        price, currency = None, ""
        byn = (ad.get("price_byn") or "").strip()
        usd = (ad.get("price_usd") or "").strip()
        if byn and byn != "0":
            price, currency = float(byn) / 100, "BYN"  # kopecks -> rubles
        elif usd and usd != "0":
            price, currency = float(usd) / 100, "USD"
        region = ", ".join(dict.fromkeys(
            param["vl"] for param in (ad.get("ad_parameters") or [])
            if param.get("p") in ("region", "area") and param.get("vl")))
        images = [THUMBS + image["path"] for image in (ad.get("images") or []) if image.get("path")]
        ad_id = ad.get("ad_id")
        return Listing(
            external_id=str(ad_id),
            title=title,
            price=price,
            currency=currency,
            url=ad.get("ad_link") or (f"https://kufar.by/item/{ad_id}" if ad_id else ""),
            region=region,
            images=images,
        )
