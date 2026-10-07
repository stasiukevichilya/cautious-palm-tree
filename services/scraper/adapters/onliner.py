"""Onliner second-hand offers via the catalog API: /search/<schema>/second-offers."""
import json
import logging

from .base import Adapter, Listing

log = logging.getLogger("scraper.onliner")

API = "https://catalog.api.onliner.by"
PAGE_LIMIT = 30


class OnlinerAdapter(Adapter):
    source = "onliner"

    async def fetch(self, watch):
        max_pages = int(watch.params.get("max_pages", 50))
        result = []
        for page in range(1, max_pages + 1):
            url = f"{API}/search/{watch.ref}/second-offers" + (f"?page={page}" if page > 1 else "")
            data = json.loads(await self.client.get_text(url))
            offers = data.get("offers") or []
            if not offers:
                break
            for offer in offers:
                listing = self._parse_offer(offer)
                if listing and watch.matches(listing.title, listing.description):
                    result.append(listing)
            meta = data.get("page") or {}
            if page >= int(meta.get("last") or 1) or len(offers) < PAGE_LIMIT:
                break
        return result

    def _parse_offer(self, offer):
        product = offer.get("product") or {}
        title = (product.get("full_name") or "").strip()
        if not title:
            return None
        price = (offer.get("price") or {}).get("amount")
        photos = []
        for photo in offer.get("photos") or []:
            photos.extend(value for value in photo.values() if value)
        return Listing(
            external_id=str(offer.get("id")),
            title=title,
            price=float(price) if price not in (None, "") else None,
            currency=(offer.get("price") or {}).get("currency") or "BYN",
            url=offer.get("html_url") or "",
            region=(offer.get("location") or {}).get("address_string") or "",
            condition=str(offer.get("condition") or ""),
            description=(offer.get("description") or "").strip(),
            images=photos,
        )
