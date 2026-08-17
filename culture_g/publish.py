"""Publication : flux RSS de podcast, page web, et purge des vieux episodes.

Le flux doit etre strictement valide : les applications de podcast rejettent un flux
mal forme sans message d'erreur exploitable. Les points sensibles sont les URL absolues,
la date au format RFC 2822, et la taille exacte du fichier dans l'enclosure.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from . import config

log = logging.getLogger(__name__)

_MANIFEST = "episodes.json"


def _iso_to_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_manifest(docs_dir: str) -> list[dict]:
    path = os.path.join(docs_dir, _MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []


def save_manifest(docs_dir: str, episodes: list[dict]) -> None:
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, _MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(episodes, fh, ensure_ascii=False, indent=2)


def _hhmmss(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def register(
    docs_dir: str,
    *,
    slug: str,
    title: str,
    teaser: str,
    notes: str,
    audio_filename: str,
    duration: float,
    published: datetime,
) -> list[dict]:
    """Ajoute (ou remplace) un episode dans le manifeste et renvoie la liste a jour."""
    audio_path = os.path.join(docs_dir, "episodes", audio_filename)
    size = os.path.getsize(audio_path) if os.path.exists(audio_path) else 0

    episodes = [e for e in load_manifest(docs_dir) if e["slug"] != slug]
    episodes.append(
        {
            "slug": slug,
            "title": title,
            "teaser": teaser,
            "notes": notes,
            "audio": audio_filename,
            "bytes": size,
            "duration": duration,
            "published": published.isoformat(),
        }
    )
    episodes.sort(key=lambda e: e["published"], reverse=True)
    return episodes


def prune(docs_dir: str, episodes: list[dict], keep_days: int) -> list[dict]:
    """Supprime les episodes trop anciens pour que le depot reste leger."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept, dropped = [], []
    for ep in episodes:
        (kept if _iso_to_dt(ep["published"]) >= cutoff else dropped).append(ep)

    for ep in dropped:
        path = os.path.join(docs_dir, "episodes", ep["audio"])
        try:
            os.remove(path)
            log.info("Episode purge : %s", ep["audio"])
        except OSError:
            pass
    if dropped:
        log.info("%d episode(s) de plus de %d jours supprimes.", len(dropped), keep_days)
    return kept


def _rss(episodes: list[dict], site_url: str) -> str:
    base = site_url.rstrip("/")
    now = format_datetime(datetime.now(timezone.utc))
    e = escape

    items = []
    for ep in episodes:
        audio_url = f"{base}/episodes/{ep['audio']}"
        description = f"{ep['teaser']}\n\n{ep['notes']}"
        items.append(f"""    <item>
      <title>{e(ep['title'])}</title>
      <link>{e(base)}/</link>
      <guid isPermaLink="false">culture-g-{e(ep['slug'])}</guid>
      <pubDate>{format_datetime(_iso_to_dt(ep['published']))}</pubDate>
      <description>{e(description)}</description>
      <itunes:summary>{e(description)}</itunes:summary>
      <itunes:subtitle>{e(ep['teaser'][:255])}</itunes:subtitle>
      <itunes:duration>{_hhmmss(ep['duration'])}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
      <enclosure url="{e(audio_url)}" length="{ep['bytes']}" type="audio/mpeg"/>
    </item>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{e(config.PODCAST_TITLE)}</title>
    <link>{e(base)}/</link>
    <atom:link href="{e(base)}/feed.xml" rel="self" type="application/rss+xml"/>
    <language>{e(config.PODCAST_LANGUAGE)}</language>
    <description>{e(config.PODCAST_DESCRIPTION)}</description>
    <lastBuildDate>{now}</lastBuildDate>
    <generator>Culture G</generator>
    <itunes:author>{e(config.PODCAST_AUTHOR)}</itunes:author>
    <itunes:subtitle>{e(config.PODCAST_SUBTITLE)}</itunes:subtitle>
    <itunes:summary>{e(config.PODCAST_DESCRIPTION)}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{e(base)}/cover.png"/>
    <itunes:category text="{e(config.PODCAST_CATEGORY)}"/>
    <itunes:owner>
      <itunes:name>{e(config.PODCAST_AUTHOR)}</itunes:name>
    </itunes:owner>
{chr(10).join(items)}
  </channel>
</rss>
"""


def _html(episodes: list[dict], site_url: str) -> str:
    e = escape
    cards = []
    for ep in episodes:
        when = _iso_to_dt(ep["published"]).strftime("%d/%m/%Y")
        mins = int(round(ep["duration"] / 60))
        cards.append(f"""  <article>
    <h2>{e(ep['title'])}</h2>
    <p class="meta">{when} &middot; {mins} min</p>
    <p>{e(ep['teaser'])}</p>
    <audio controls preload="none" src="episodes/{e(ep['audio'])}"></audio>
    <details><summary>Sources</summary><pre>{e(ep['notes'])}</pre></details>
  </article>""")

    feed_url = f"{site_url.rstrip('/')}/feed.xml"
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(config.PODCAST_TITLE)}</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fbfaf8; --fg:#1a1a18; --mut:#6b6a66; --line:#e5e3de; --card:#fff; }}
  @media (prefers-color-scheme:dark) {{
    :root {{ --bg:#16161a; --fg:#eceaee; --mut:#9a98a2; --line:#2c2c33; --card:#1e1e24; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
         font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; }}
  header {{ border-bottom:1px solid var(--line); padding-bottom:1.5rem; margin-bottom:2rem; }}
  h1 {{ font-size:2rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  .sub {{ color:var(--mut); margin:0 0 1.25rem; }}
  .sub-cta {{ display:inline-block; padding:.6rem .9rem; border:1px solid var(--line);
              border-radius:.6rem; background:var(--card); font-size:.9rem; }}
  code {{ font-size:.85em; word-break:break-all; }}
  article {{ background:var(--card); border:1px solid var(--line); border-radius:.75rem;
             padding:1.25rem; margin-bottom:1rem; }}
  h2 {{ font-size:1.15rem; margin:0 0 .3rem; }}
  .meta {{ color:var(--mut); font-size:.85rem; margin:0 0 .6rem; }}
  audio {{ width:100%; margin-top:.75rem; }}
  details {{ margin-top:.75rem; font-size:.85rem; color:var(--mut); }}
  pre {{ white-space:pre-wrap; word-break:break-word; font-size:.8rem; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>{e(config.PODCAST_TITLE)}</h1>
    <p class="sub">{e(config.PODCAST_SUBTITLE)}</p>
    <div class="sub-cta">
      S'abonner dans une application de podcast :<br><code>{e(feed_url)}</code>
    </div>
  </header>
{chr(10).join(cards) if cards else '  <p>Aucun episode pour le moment.</p>'}
</main>
</body>
</html>
"""


def publish(docs_dir: str, episodes: list[dict], site_url: str) -> list[dict]:
    """Ecrit le manifeste, le flux RSS et la page d'accueil apres purge."""
    episodes = prune(docs_dir, episodes, config.KEEP_EPISODES_DAYS)
    os.makedirs(docs_dir, exist_ok=True)

    save_manifest(docs_dir, episodes)
    with open(os.path.join(docs_dir, "feed.xml"), "w", encoding="utf-8") as fh:
        fh.write(_rss(episodes, site_url))
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(_html(episodes, site_url))
    # Empeche GitHub Pages de passer le site dans Jekyll, qui ignorerait certains fichiers.
    open(os.path.join(docs_dir, ".nojekyll"), "w").close()

    log.info("Publication : %d episode(s) dans le flux.", len(episodes))
    return episodes
