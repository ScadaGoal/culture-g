"""Analyse en profondeur des seuls sujets retenus.

Les videos partent chez Gemini sous forme d'URL YouTube : c'est Google qui va chercher
le media depuis ses propres serveurs. Aucun telechargement, aucun yt-dlp, donc aucun
blocage anti-bot depuis les IP de datacenter — le mode d'echec classique de ce genre
de pipeline hebergee en CI.

Les articles passent par l'outil url_context, qui lit la page cote Gemini.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .collect import Item
from .curate import Topic
from .models import ladder, try_models

log = logging.getLogger(__name__)

YOUTUBE_HOSTS = ("youtube.com", "youtu.be")

# Espacement entre deux analyses, en secondes. Suffit a rester sous la limite par
# minute du free tier sans rallonger notablement la duree totale de l'etape.
PAUSE_BETWEEN_TOPICS = 6.0


def _is_video(url: str) -> bool:
    return any(host in url for host in YOUTUBE_HOSTS)


def _notes_prompt(topic: Topic, sources: list[Item]) -> str:
    listing = "\n".join(f"- {s.source} : {s.title} ({s.url})" for s in sources)
    return f"""Sujet a traiter : {topic.title}
Angle demande : {topic.angle}
Temps d'antenne prevu : {topic.minutes:.0f} minutes.

Sources :
{listing}

Produis des notes de preparation en francais pour l'animateur d'un podcast. Structure :

FAITS — les elements verifiables : quoi, qui, quand, chiffres exacts.
CONTEXTE — ce qu'il faut savoir pour comprendre, en deux ou trois points.
POURQUOI C'EST INTERESSANT — l'angle qui accroche, le contre-intuitif, l'enjeu reel.
A NUANCER — ce qui est incertain, exagere, ou qui reste a confirmer. Sois honnete :
  si l'annonce est surtout du marketing, dis-le.
CITATION — s'il y a une phrase ou un chiffre marquant a reprendre a l'antenne.

Contraintes : uniquement ce qui est reellement dans les sources, aucune extrapolation.
Densite maximale, pas de remplissage. N'ecris pas encore de dialogue, ce sont des notes.
"""


def _analyse_topic(client: Any, model: str, topic: Topic, by_url: dict[str, Item]) -> dict:
    sources = [by_url[u] for u in topic.item_urls if u in by_url]
    if not sources:
        return {"topic": topic, "notes": "", "ok": False}

    video_urls = [s.url for s in sources if _is_video(s.url)]
    article_urls = [s.url for s in sources if not _is_video(s.url)]

    parts: list[dict] = [{"type": "text", "text": _notes_prompt(topic, sources)}]
    # Gemini 2.5+ accepte jusqu'a 10 videos par requete ; on borne par prudence.
    for url in video_urls[:3]:
        parts.append({"type": "video", "uri": url})

    tools = [{"type": "url_context"}] if article_urls else None

    def call(model_id: str):
        kwargs: dict[str, Any] = {
            "model": model_id,
            "input": parts,
            "generation_config": {"thinking_level": "medium"},
        }
        if tools:
            kwargs["tools"] = tools
        return client.interactions.create(**kwargs)

    try:
        interaction = try_models(
            client, ladder("digest", model), call, label=f"analyse [{topic.title[:40]}]"
        )
        notes = (interaction.output_text or "").strip()
    except Exception as exc:
        log.warning("Analyse impossible pour '%s' (%s).", topic.title, exc)
        return {"topic": topic, "notes": "", "ok": False}

    if not notes:
        return {"topic": topic, "notes": "", "ok": False}

    log.info("Analyse OK : %-52s (%d videos, %d articles)",
             topic.title[:52], len(video_urls), len(article_urls))
    return {"topic": topic, "notes": notes, "sources": sources, "ok": True}


def digest(client: Any, model: str, topics: list[Topic], items: list[Item]) -> list[dict]:
    """Analyse chaque sujet ; renvoie les dossiers exploitables, dans l'ordre d'importance."""
    by_url = {i.url: i for i in items}

    # Analyse sequentielle, avec une pause entre les sujets. Le parallelisme a ete
    # teste : il declenche immediatement la limite de requetes par minute du free
    # tier, et les reessais qui s'ensuivent coutent plus cher que le temps gagne.
    results = []
    for n, topic in enumerate(topics):
        if n:
            time.sleep(PAUSE_BETWEEN_TOPICS)
        results.append(_analyse_topic(client, model, topic, by_url))

    usable = [r for r in results if r["ok"]]
    failed = len(results) - len(usable)
    if failed:
        log.warning("%d sujet(s) abandonnes faute d'analyse exploitable.", failed)
    if not usable:
        raise RuntimeError("Aucun sujet n'a pu etre analyse : episode impossible.")

    usable.sort(key=lambda r: r["topic"].importance, reverse=True)
    return usable
