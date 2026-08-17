"""Ecriture du dialogue a deux voix.

Contrainte majeure : la sortie part directement dans un moteur de synthese vocale.
Tout ce qui n'est pas prononcable (markdown, URL, symboles, listes a puces) devient
un artefact audible. Le prompt est donc autant un cahier des charges de redaction
qu'un cahier des charges de prononciation.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from . import config
from .models import ladder, try_models

log = logging.getLogger(__name__)

_MARKDOWN = re.compile(r"[*_#`~]+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MULTISPACE = re.compile(r"[ \t]+")

# Symboles que la synthese lit de facon imprevisible : selon le contexte elle les
# prononce en anglais, les ignore, ou les epelle. On impose la forme parlee.
_SYMBOLS = [
    (re.compile(r"(\d)\s*%"), r"\1 pour cent"),
    (re.compile(r"%"), " pour cent"),
    (re.compile(r"(\d)\s*€"), r"\1 euros"),
    (re.compile(r"€"), " euros"),
    (re.compile(r"\$\s*(\d)"), r"\1 dollars"),
    (re.compile(r"\$"), " dollars"),
    (re.compile(r"\s&\s"), " et "),
    (re.compile(r"\s[=→>]+\s"), " donne "),
]


class Episode(BaseModel):
    title: str = Field(description="Titre de l'episode, accrocheur et concret, 60 caracteres max.")
    teaser: str = Field(description="Resume en deux phrases, pour la description du podcast.")
    script: str = Field(
        description=(
            "Le dialogue complet. Chaque replique commence par 'Animateur:' ou 'Expert:' "
            "en debut de ligne, suivie du texte a prononcer."
        )
    )


def _speaker_brief() -> str:
    return "\n".join(f"- {s.tag} : {s.persona}" for s in config.SPEAKERS)


def _build_prompt(headline: str, dossiers: list[dict], today: date) -> str:
    lo_w, hi_w = config.TARGET_WORDS
    lo_m, hi_m = config.TARGET_MINUTES

    blocks = []
    for n, d in enumerate(dossiers, 1):
        t = d["topic"]
        blocks.append(
            f"--- SUJET {n} — {t.title} "
            f"(importance {t.importance}/5, {t.minutes:.0f} min d'antenne)\n"
            f"Angle : {t.angle}\n\n{d['notes']}"
        )
    dossier_text = "\n\n".join(blocks)

    short = len(dossiers) <= config.SHORT_EPISODE_THRESHOLD
    length_note = (
        f"L'actualite du jour est mince : {len(dossiers)} sujets seulement. Fais un episode "
        "court et dense plutot que de delayer. Mieux vaut huit minutes utiles que quinze "
        "minutes de remplissage."
        if short
        else f"Vise {lo_w} a {hi_w} mots au total, soit {lo_m} a {hi_m} minutes a l'oral."
    )

    return f"""Date : {today.strftime('%d/%m/%Y')}
Sujet principal du jour : {headline}

Voici les notes de preparation :

{dossier_text}

=== TA MISSION ===

Ecris le dialogue complet de l'episode du jour.

INTERVENANTS
{_speaker_brief()}

STRUCTURE
1. Ouverture : l'Animateur accueille et annonce en deux phrases ce qu'on va voir. Pas de
   "bonjour a tous et bienvenue dans" generique — entre par le sujet le plus fort.
2. Les sujets dans l'ordre donne, du plus important au moins important.
3. Chaque sujet s'ouvre par une transition parlee, jamais par un titre annonce.
4. Cloture breve : une phrase de synthese, et rendez-vous demain.

LONGUEUR
{length_note}
Respecte grossierement le temps alloue a chaque sujet.

ECRITURE
- Vraie conversation : l'Animateur interrompt, demande de preciser, reformule pour
  l'auditeur. L'Expert repond, nuance, donne les chiffres.
- Repliques de longueur variable. Une relance de cinq mots est bienvenue entre deux
  developpements. Alterner mecaniquement long/long endort.
- Zero flagornerie entre les deux voix : pas de "excellente question", pas de
  "tu as tout a fait raison". Ils font leur travail, ils ne se felicitent pas.
- Ancrer dans le concret : un chiffre, une comparaison, une consequence pour l'auditeur.
- Signaler ce qui est incertain plutot que d'affirmer. Si une annonce sent le marketing,
  l'Expert le dit franchement.

FIDELITE AUX NOTES (l'auditeur retient ces chiffres, une erreur se propage)
- Ne reprends que des chiffres presents dans les notes, a l'identique. N'en invente
  aucun, n'en arrondis aucun, n'en deduis aucun par calcul.
- Ne convertis jamais une devise ni une unite. Si les notes disent dollars, tu dis
  dollars. Confondre 1,5 milliard de dollars et 1,3 milliard d'euros discredite tout
  l'episode.
- Attribue les affirmations notables a leur source, a l'oral : "selon telle publication",
  "d'apres l'annonce de tel laboratoire". Cela permet a l'auditeur de juger lui-meme du
  credit a accorder.
- Une information issue d'une chaine de vulgarisation ou d'un forum n'a pas le meme
  poids qu'un communique officiel : dis-le quand c'est le cas.
- Aucun superlatif publicitaire : ni "revolutionnaire", ni "incroyable", ni "game changer".

=== CONTRAINTES DE SYNTHESE VOCALE (imperatives) ===

Le texte part tel quel dans un moteur vocal. Tout caractere non prononcable devient
un bruit parasite dans l'oreille de l'auditeur.

- Chaque ligne de dialogue commence exactement par "Animateur:" ou "Expert:".
  Rien d'autre en debut de ligne. Pas de tiret, pas de numero, pas de nom entre crochets.
- Interdits absolus : asterisques, diese, underscores, backticks, listes a puces,
  titres, emojis, parentheses de didascalie du type (rires).
- Aucune URL, aucun nom de domaine. On dit "le blog d'OpenAI", jamais l'adresse.
- Les nombres : ecris en toutes lettres uniquement les nombres simples et ronds
  ("trois milliards de dollars", "quatre-vingts pour cent", "deux mille vingt-six").
  Laisse en chiffres tout ce qui est decimal, precis ou composite : "46,2", "2,5 cents",
  "99 %", "750 tokens par seconde". Le moteur vocal les lit correctement, alors qu'une
  conversion manuelle produit des contresens ("99 % a 95 %" devenu "neuf pour cent a
  quatre-vingt-quinze pour cent"). Dans le doute, garde le chiffre.
- Garder les chiffres aussi dans les noms propres etablis : GPT-4, Claude 3.
- Les sigles s'ecrivent comme ils se prononcent : "IA", "API", "GPU" passent tels quels.
  Un sigle inhabituel se developpe a la premiere occurrence.
- Les mots anglais courants du domaine restent (transformer, prompt, benchmark), mais
  une phrase entiere en anglais est proscrite.
- Pas d'abreviations : ecrire "par exemple" et non "ex.", "c'est-a-dire" et non "cad".

Le champ script contient uniquement le dialogue, sans preambule ni commentaire.
"""


def _sanitize(script: str) -> tuple[str, int]:
    """Retire ce qui rendrait la synthese vocale bruyante.

    Le prompt interdit deja ces elements, mais un modele finit toujours par glisser
    une astensque ou une URL. La synthese les prononcerait litteralement, donc on
    nettoie systematiquement plutot que d'esperer.
    """
    fixes = 0
    lines_out: list[str] = []
    tags = tuple(f"{s.tag}:" for s in config.SPEAKERS)

    for raw in script.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Puces ou tirets de liste ajoutes devant l'etiquette.
        stripped = re.sub(r"^[-*•\d.)\s]+(?=(Animateur|Expert):)", "", line)
        if stripped != line:
            fixes += 1
            line = stripped
        cleaned = _MARKDOWN.sub("", line)
        cleaned = _URL.sub("", cleaned)
        for pattern, replacement in _SYMBOLS:
            cleaned = pattern.sub(replacement, cleaned)
        cleaned = _MULTISPACE.sub(" ", cleaned).strip()
        # Le retrait d'une URL laisse souvent une ponctuation orpheline.
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        if cleaned != line:
            fixes += 1
        if cleaned:
            lines_out.append(cleaned)

    # On ne conserve que les lignes reellement etiquetees : une ligne orpheline serait
    # attribuee a un locuteur arbitraire par le moteur.
    kept = [l for l in lines_out if l.startswith(tags)]
    dropped = len(lines_out) - len(kept)
    if dropped:
        log.warning("%d ligne(s) sans etiquette de locuteur ecartee(s).", dropped)
        fixes += dropped
    return "\n".join(kept), fixes


def write_script(client: Any, model: str, headline: str, dossiers: list[dict],
                 today: date | None = None) -> Episode:
    """Produit le dialogue final, nettoye et pret pour la synthese vocale."""
    today = today or date.today()

    system = (
        config.EDITORIAL_LINE
        + "\nTu ecris les dialogues de ce podcast quotidien. Tu es un auteur radio : "
        "tu ecris pour l'oreille, pas pour l'oeil. Une phrase qui ne se dit pas a voix "
        "haute est une phrase ratee."
    )

    prompt = _build_prompt(headline, dossiers, today)
    interaction = try_models(
        client,
        ladder("script", model),
        lambda model_id: client.interactions.create(
            model=model_id,
            input=prompt,
            system_instruction=system,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": Episode.model_json_schema(),
            },
            generation_config={"thinking_level": "high", "max_output_tokens": 16000},
        ),
        label="ecriture du script",
    )

    episode = Episode.model_validate_json(interaction.output_text)
    episode.script, fixes = _sanitize(episode.script)

    if not episode.script.strip():
        raise RuntimeError("Script vide apres nettoyage : rien a synthetiser.")

    words = len(episode.script.split())
    turns = len(episode.script.splitlines())
    log.info("Script : %d mots, %d repliques, ~%.1f min (%d correction(s))",
             words, turns, words / 150, fixes)
    if words < config.TARGET_WORDS[0] * 0.5:
        log.warning("Script anormalement court (%d mots).", words)

    return episode


def show_notes(dossiers: list[dict]) -> str:
    """Liste des sources, pour la description de l'episode dans l'app podcast."""
    lines = ["Sources de cet episode :", ""]
    seen: set[str] = set()
    for d in dossiers:
        lines.append(f"- {d['topic'].title}")
        for s in d.get("sources", []):
            if s.url in seen:
                continue
            seen.add(s.url)
            lines.append(f"    {s.source} : {s.url}")
    lines += ["", "Genere automatiquement par Culture G."]
    return "\n".join(lines)
