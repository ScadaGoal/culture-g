"""Collecte des flux RSS et deduplication.

Aucune requete LLM ici : cette etape doit pouvoir tourner et etre debuguee sans
consommer un seul jeton de quota.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import feedparser

from .config import ALL_SOURCES, Source

log = logging.getLogger(__name__)

_SEEN_FILENAME = "seen.json"
_SEEN_MAX = 2000  # borne la croissance du fichier d'etat
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


@dataclass
class Item:
    source: str
    kind: str  # "video" ou "article"
    weight: int
    lang: str
    title: str
    url: str
    published: str  # ISO 8601 UTC
    summary: str

    @property
    def uid(self) -> str:
        return self.url


def _clean(raw: str | None, limit: int = 600) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAGS.sub(" ", raw))
    text = _SPACES.sub(" ", text).strip()
    return text[:limit]


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _fetch(source: Source, cutoff: datetime) -> list[Item]:
    try:
        feed = feedparser.parse(source.url)
    except Exception as exc:  # pragma: no cover - depend du reseau
        log.warning("%s : flux illisible (%s)", source.name, exc)
        return []

    if getattr(feed, "bozo", 0) and not feed.entries:
        log.warning("%s : flux invalide ou injoignable", source.name)
        return []

    items: list[Item] = []
    for entry in feed.entries:
        when = _published(entry)
        if when is None or when < cutoff:
            continue
        link = entry.get("link") or ""
        if not link:
            continue
        summary = _clean(entry.get("summary") or entry.get("description"))
        # Les flux YouTube exposent le vrai resume dans media_description.
        media = entry.get("media_description")
        if source.kind == "video" and media:
            summary = _clean(media)
        items.append(
            Item(
                source=source.name,
                kind=source.kind,
                weight=source.weight,
                lang=source.lang,
                title=_clean(entry.get("title"), 300),
                url=link,
                published=when.isoformat(),
                summary=summary,
            )
        )
    log.info("%-18s %2d item(s)", source.name, len(items))
    return items


def load_seen(state_dir: str) -> set[str]:
    path = os.path.join(state_dir, _SEEN_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(state_dir: str, seen: set[str], added: list[str]) -> None:
    """Persiste les URL vues, les plus recentes en fin de liste.

    On tronque par la tete : les anciennes entrees ne peuvent de toute facon plus
    repasser le filtre de fenetre temporelle.
    """
    os.makedirs(state_dir, exist_ok=True)
    ordered = [u for u in seen if u not in set(added)] + added
    ordered = ordered[-_SEEN_MAX:]
    with open(os.path.join(state_dir, _SEEN_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=1)


def collect(
    state_dir: str,
    window_hours: int,
    *,
    apply_seen: bool = True,
    sources: list[Source] | None = None,
) -> list[Item]:
    """Recupere les nouveautes de tous les flux, hors items deja traites."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    feeds = sources if sources is not None else ALL_SOURCES

    items: list[Item] = []
    # Les flux sont independants et domines par la latence reseau : en parallele,
    # la collecte complete passe de ~30 s a ~4 s.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for batch in pool.map(lambda s: _fetch(s, cutoff), feeds):
            items.extend(batch)

    if apply_seen:
        seen = load_seen(state_dir)
        items = [i for i in items if i.uid not in seen]

    # Plus recent d'abord, a poids egal.
    items.sort(key=lambda i: (i.weight, i.published), reverse=True)
    log.info("Total retenu : %d item(s) sur %d flux", len(items), len(feeds))
    return items


def to_jsonable(items: list[Item]) -> list[dict]:
    return [asdict(i) for i in items]
