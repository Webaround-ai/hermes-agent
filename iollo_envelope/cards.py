"""The existing text-list and flat-card projection, shared by both runtime bodies."""
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

DETAIL_KEYS = ("kind", "address", "latitude", "longitude", "phone", "book_url", "order_url", "buy_url",
               "tickets_url", "start", "end")
URL_KEYS = ("book_url", "order_url", "buy_url", "tickets_url")
KINDS = {"restaurant", "place", "product", "news", "event", "food"}
UBER_EATS_HOST = "ubereats.com"
def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_host(url: str, domain: str) -> bool:
    host = _host(url)
    return host == domain or host.endswith("." + domain)


def _float(value: Any, low: float, high: float) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if low <= f <= high else None


def clean_details(card: dict) -> dict:
    """The action facts of one card, checked: https links only, a dialable phone, coordinates in range."""
    out: dict[str, Any] = {}
    kind = str(card.get("kind") or "").strip().lower()
    if kind in KINDS:
        out["kind"] = kind
    for key in URL_KEYS:
        url = str(card.get(key) or "").strip()
        if url.startswith("https://") and _host(url):
            out[key] = url[:1000]
    address = " ".join(str(card.get("address") or "").split())[:300]
    if address:
        out["address"] = address
    lat, lng = _float(card.get("latitude"), -90, 90), _float(card.get("longitude"), -180, 180)
    if lat is not None and lng is not None and (lat, lng) != (0.0, 0.0):
        out["latitude"], out["longitude"] = lat, lng
    phone = re.sub(r"[^\d+]", "", str(card.get("phone") or ""))
    if 6 <= len(phone.lstrip("+")) <= 16 and "+" not in phone[1:]:
        out["phone"] = phone
    for key in ("start", "end"):
        if parse_when(card.get(key)) is not None:
            out[key] = str(card.get(key)).strip()[:40]
    return out


def parse_when(value: Any) -> datetime | date | None:
    """An ISO date or date-time; a date alone is an all-day event."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


ILLUSTRATIONS = {"clear-day", "partly-cloudy", "cloudy", "rain", "storm", "snow", "fog", "clear-night", "meeting",
                 "call", "travel", "meal", "event", "task", "place", "price-up", "price-down", "price-flat", "link", "info",
                 "product"}
def site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)]|[-•*])\s+")
_URL = re.compile(r"https://[^\s)>\]]+")
_BOLD_TITLE = re.compile(r"^\*{1,2}(.+?)\*{1,2}\s*[:.—–-]?\s*")


def cards_from_text(text: str) -> tuple[str, list[dict], str] | None:
    """A reply that is a list of 2 to 10 items, each with its own https link, as (intro, cards, outro);
    None for anything else (steps, plain lists, prose)."""
    lines = (text or "").strip().splitlines()
    intro, items, outro = [], [], []
    for line in lines:
        if _ITEM.match(line):
            items.append([_ITEM.sub("", line, count=1)])
        elif items and line.strip() and not outro and (line.startswith((" ", "\t")) or _URL.fullmatch(line.strip())):
            items[-1].append(line.strip())
        elif items and line.strip():
            outro.append(line.strip())
        elif not items:
            intro.append(line)
        elif outro:
            outro.append(line.strip())
    if not 2 <= len(items) <= 10:
        return None
    cards = []
    for parts in items:
        block = " ".join(p.strip() for p in parts)
        urls = _URL.findall(block)
        if not urls:
            return None
        url = urls[0].rstrip(".,;:")
        rest = " ".join(_URL.sub("", block).split()).strip(" —–-:·|()")
        m = _BOLD_TITLE.match(rest)
        if m:
            title, body = m.group(1).strip(), rest[m.end():]
        else:
            cut = re.split(r"\s[—–-]\s|:\s|\.\s", rest, maxsplit=1)
            title, body = cut[0], (cut[1] if len(cut) > 1 else "")
        title = title.strip(" *_")[:80] or site_name(url)
        cards.append({"title": title, "text": body.strip(" —–-:·|*"), "url": url, "image": ""})
    return "\n".join(intro).strip(), cards, "\n".join(outro).strip()


CARD_KINDS = ("place", "food", "event", "weather", "call", "link", "info", "quote", "directions", "restaurant",
              "product", "news")
_WEATHER_ILLUSTRATIONS = {"clear-day", "partly-cloudy", "cloudy", "rain", "storm", "snow", "fog", "clear-night"}
def _kind_of(flat: dict) -> str:
    kind = str(flat.get("kind") or "").strip().lower()
    if kind in CARD_KINDS:
        return kind
    illustration = flat.get("illustration") or ""
    if flat.get("link") and is_host(str(flat["link"]), UBER_EATS_HOST):
        return "food"
    if illustration in _WEATHER_ILLUSTRATIONS:
        return "weather"
    if illustration.startswith("price-"):
        return "quote"
    if illustration == "call":
        return "call"
    if illustration == "travel":
        return "directions"
    return "link" if flat.get("url") else "info"


def card_from_flat(flat: dict) -> dict:
    """A card as the control plane has always held it, in the catalog's shape."""
    card: dict[str, Any] = {"kind": _kind_of(flat), "title": " ".join(str(flat.get("title") or "").split())[:200]}
    text = " ".join(str(flat.get("text") or "").split())[:1000]
    if text:
        card["text"] = text
    for key in ("url", "image"):
        value = str(flat.get(key) or "").strip()
        if value.startswith("https://") and " " not in value:
            card[key] = value[:2000]
    if flat.get("illustration") in ILLUSTRATIONS:
        card["illustration"] = flat["illustration"]
    details = clean_details(flat)
    if details:
        card["details"] = details
    link = str(flat.get("link") or "").strip()
    if link.startswith("https://"):
        label = " ".join(str(flat.get("label") or "Open").split())[:40] or "Open"
        card["actions"] = [{"id": "open", "label": label, "kind": "order" if label.startswith("Order") else "open_url",
                            "value": link[:2000]}]
    return card


def cards_block(block_id: str, flat: list[dict], intro: str = "", layout: str = "carousel") -> dict:
    block: dict[str, Any] = {"type": "cards", "id": block_id, "layout": layout, "items": [card_from_flat(c) for c in flat]}
    if intro.strip():
        block["intro"] = intro.strip()[:1000]
    return block


