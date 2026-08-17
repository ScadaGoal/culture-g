"""Selection editoriale : passer de ~90 items bruts a 4-6 sujets qui meritent l'antenne.

C'est l'etape qui protege le reste du pipeline : elle filtre avant les appels couteux
(analyse video, ecriture, synthese vocale), donc elle determine autant la qualite de
l'episode que sa consommation de quota.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from . import config
from .collect import Item
from .models import ladder, try_models, with_retry

log = logging.getLogger(__name__)


class Topic(BaseModel):
    title: str = Field(description="Titre du sujet, en francais, factuel et concret.")
    angle: str = Field(
        description="En une phrase : pourquoi ce sujet merite l'antenne et sous quel angle le traiter."
    )
    item_urls: list[str] = Field(
        description="URL des items sources qui documentent ce sujet (une ou plusieurs)."
    )
    importance: int = Field(description="Importance de 1 (anecdotique) a 5 (incontournable).")
    minutes: float = Field(description="Temps d'antenne alloue, en minutes.")


class Selection(BaseModel):
    headline: str = Field(description="Le sujet principal du jour, en une formule courte.")
    topics: list[Topic]
    discarded_reason: str = Field(
        description="En une phrase : ce qui a ete ecarte et pourquoi. Sert au debug editorial."
    )


def _grounding_sweep(client: Any, model: str) -> str:
    """Rattrape les annonces de laboratoires que les flux RSS ne couvrent pas.

    Anthropic notamment n'expose aucun flux ; sans ce filet, une sortie de modele
    majeure pourrait passer sous les radars jusqu'a ce que la presse la reprenne.
    """
    watchlist = ", ".join(config.GROUNDING_WATCHLIST)
    prompt = (
        f"Nous sommes aujourd'hui. Recherche s'il y a eu, dans les dernieres 48 heures, "
        f"une annonce notable (sortie ou mise a jour de modele, publication de recherche "
        f"marquante, annonce produit majeure) chez : {watchlist}.\n\n"
        "Reponds en francais, en 5 lignes maximum, uniquement avec des faits date et sources. "
        "Si tu ne trouves rien de notable, reponds exactement : RIEN."
    )
    try:
        # Une seule tentative : ce filet est un bonus, et chaque requete depensee ici
        # est une requete en moins pour l'analyse des sujets, qui compte davantage.
        interaction = with_retry(
            lambda: client.interactions.create(
                model=model,
                input=prompt,
                tools=[{"type": "google_search"}],
            ),
            attempts=1,
            label="veille grounding",
        )
        text = (interaction.output_text or "").strip()
        if text.upper().startswith("RIEN"):
            log.info("Veille grounding : rien de neuf hors flux RSS.")
            return ""
        log.info("Veille grounding : elements complementaires trouves.")
        return text
    except Exception as exc:
        # Ce filet est un bonus : son echec ne doit jamais faire tomber l'episode.
        log.warning("Veille grounding indisponible (%s), on continue sans.", exc)
        return ""


def _format_items(items: list[Item]) -> str:
    lines = []
    for n, it in enumerate(items, 1):
        kind = "VIDEO" if it.kind == "video" else "ARTICLE"
        lines.append(
            f"[{n}] ({kind}, {it.source}, poids {it.weight})\n"
            f"    titre   : {it.title}\n"
            f"    url     : {it.url}\n"
            f"    resume  : {it.summary[:320]}"
        )
    return "\n".join(lines)


def curate(
    client: Any,
    model: str,
    items: list[Item],
    *,
    use_grounding: bool = True,
) -> Selection:
    """Choisit et hierarchise les sujets du jour."""
    if not items:
        raise ValueError("Aucun item collecte : rien a curer.")

    extra = _grounding_sweep(client, model) if use_grounding else ""
    lo, hi = config.TARGET_MINUTES

    system = (
        config.EDITORIAL_LINE
        + "\nTu es le redacteur en chef de ce podcast quotidien. Tu selectionnes les sujets "
        "du jour parmi une revue de presse brute, et tu alloues le temps d'antenne."
    )

    prompt = f"""Voici les {len(items)} items collectes depuis les flux suivis.

{_format_items(items)}
"""

    if extra:
        prompt += f"""
Elements complementaires issus d'une recherche web (labos sans flux RSS) :
{extra}
"""

    prompt += f"""
Selectionne entre {config.MIN_TOPICS} et {config.MAX_TOPICS} sujets pour l'episode d'aujourd'hui.

Regles :
- Une video suivie (poids 3) qui vient de sortir est presque toujours un bon sujet :
  ce sont les sources choisies par l'auditeur lui-meme.
- Priorite aux sorties de modeles et annonces de laboratoires.
- Regroupe sous un meme sujet plusieurs items qui parlent de la meme chose, et liste
  alors toutes leurs URL dans item_urls.
- Ecarte sans hesiter : le divertissement pur (cinema, jeux video, sport), le communique
  marketing sans substance, le sujet deja traite mille fois sans element neuf,
  le fait divers sans portee.
- Un sujet de science ou d'histoire vraiment marquant a sa place : le podcast n'est pas
  qu'un bulletin IA.
- Le total des minutes doit tomber entre {lo} et {hi}. Donne plus de temps au sujet
  principal (3 a 5 minutes) qu'aux breves (1 a 2 minutes).
- item_urls doit contenir des URL presentes ci-dessus, copiees a l'identique.
"""

    interaction = try_models(
        client,
        ladder("curate", model),
        lambda model_id: client.interactions.create(
            model=model_id,
            input=prompt,
            system_instruction=system,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": Selection.model_json_schema(),
            },
            generation_config={"thinking_level": "medium"},
        ),
        label="curation",
    )

    selection = Selection.model_validate_json(interaction.output_text)

    known = {i.url for i in items}
    for topic in selection.topics:
        # Le modele peut halluciner une URL ; on ne garde que celles reellement collectees,
        # sinon l'etape d'analyse partirait chercher une page inexistante.
        kept = [u for u in topic.item_urls if u in known]
        if len(kept) != len(topic.item_urls):
            log.warning("Sujet '%s' : %d URL inventee(s) ecartee(s).",
                        topic.title, len(topic.item_urls) - len(kept))
        topic.item_urls = kept

    selection.topics = [t for t in selection.topics if t.item_urls]
    selection.topics.sort(key=lambda t: t.importance, reverse=True)

    log.info("Curation : %d sujet(s) retenus — %s", len(selection.topics), selection.headline)
    for t in selection.topics:
        log.info("   [%d/5] %4.1f min  %s", t.importance, t.minutes, t.title)
    return selection


def dump(selection: Selection) -> str:
    return json.dumps(selection.model_dump(), ensure_ascii=False, indent=2)
