"""Resolution des identifiants de modeles Gemini et appels resilients.

Les modeles Gemini bougent vite et les modeles TTS sont en preview : coder un
identifiant en dur, c'est se garantir une panne silencieuse dans quelques mois.
Ce module interroge le compte pour savoir ce qui existe reellement, met le
resultat en cache, et redescend une cascade de candidats en cas d'echec.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

FLASH_LITE = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
]

# Cascades de candidats, du plus souhaitable au plus degrade. La resolution retient
# le premier disponible sur le compte.
CANDIDATES: dict[str, list[str]] = {
    # Trois lecons tirees d'essais sur compte reel, valables pour les trois etapes :
    #  - les modeles Pro ont un quota free tier a zero, ils echouent toujours ;
    #  - gemini-2.5-flash est encore liste par l'API mais renvoie 404 aux comptes
    #    recents ("no longer available to new users"), d'ou son absence ici ;
    #  - gemini-3.7-flash sature vite (20 requetes/jour), il sert de secours et non
    #    de choix par defaut.
    # gemini-3.6-flash est le compromis recommande par l'API elle-meme.
    #
    # Le quota gratuit est compte par modele et par jour (~20 requetes pour un Flash,
    # ~500 pour un Flash-Lite, remise a zero a minuit heure du Pacifique). D'ou :
    #  - chaque etape a un Flash de tete different, pour ne pas vider un seul compteur ;
    #  - les Flash-Lite ferment chaque cascade : un episode un peu moins fin vaut mieux
    #    que pas d'episode du tout le jour ou les Flash sont a sec.
    "curate": [
        "gemini-3.6-flash",
        "gemini-3.7-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        *FLASH_LITE,
    ],
    # Cinq appels par jour : l'etape la plus gourmande, sur son propre compteur.
    "digest": [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-3.7-flash",
        *FLASH_LITE,
    ],
    # Un seul appel par jour : on peut se permettre le modele le plus capable en tete.
    "script": [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        *FLASH_LITE,
    ],
    # Verification factuelle : tache de comparaison minutieuse, un appel par jour.
    # Elle passe avant tout sur un compteur que l'analyse n'a pas entame.
    "verify": [
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        *FLASH_LITE,
    ],
    # Veille par recherche web : un bonus, qui ne doit pas entamer le quota des Flash.
    "grounding": [*FLASH_LITE],
    # Synthese vocale multi-locuteurs.
    "tts": [
        "gemini-3.1-flash-tts-preview",
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
    ],
}

_CACHE_FILENAME = "models.json"
_RETRYABLE = ("429", "500", "502", "503", "504", "resource_exhausted", "unavailable")


def _cache_path(state_dir: str) -> str:
    return os.path.join(state_dir, _CACHE_FILENAME)


def available_models(client: Any) -> set[str]:
    """Identifiants effectivement exposes par le compte."""
    names: set[str] = set()
    try:
        for m in client.models.list():
            raw = getattr(m, "name", None) or getattr(m, "id", None) or str(m)
            names.add(str(raw).removeprefix("models/"))
    except Exception as exc:  # pragma: no cover - depend du reseau
        log.warning("Impossible de lister les modeles (%s) ; cascade a l'aveugle.", exc)
    return names


def resolve_all(client: Any, state_dir: str, refresh: bool = False) -> dict[str, str]:
    """Associe chaque etape a un identifiant de modele reellement disponible."""
    cache = _cache_path(state_dir)
    if not refresh and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                data = json.load(fh)
            # Cache perime des que les cascades changent : sinon une modification de
            # CANDIDATES resterait lettre morte tant que le fichier existe.
            if data.pop("_candidates", None) == CANDIDATES and set(data) >= set(CANDIDATES):
                return data
        except (OSError, json.JSONDecodeError):
            pass

    present = available_models(client)
    chosen: dict[str, str] = {}
    for stage, options in CANDIDATES.items():
        pick = next((o for o in options if o in present), None)
        if pick is None:
            # Le listing a echoue ou ne contient aucun candidat : on tente quand meme
            # le premier choix, l'appel dira s'il passe.
            pick = options[0]
            log.warning("Aucun candidat '%s' confirme ; essai avec %s.", stage, pick)
        chosen[stage] = pick

    os.makedirs(state_dir, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({**chosen, "_candidates": CANDIDATES}, fh, indent=2)
    return chosen


def ladder(stage: str, resolved: str) -> list[str]:
    """Modele retenu, suivi du reste de la cascade en secours.

    Permet a chaque etape de redescendre d'elle-meme quand un modele est epuise
    ou retire, au lieu de faire tomber tout l'episode.
    """
    return [resolved] + [m for m in CANDIDATES.get(stage, []) if m != resolved]


def make_client() -> Any:
    """Client Gemini construit depuis GEMINI_API_KEY."""
    from google import genai
    from google.genai import types

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit(
            "GEMINI_API_KEY absente.\n"
            "  Local   : cree un fichier .env a la racine avec GEMINI_API_KEY=...\n"
            "  Actions : ajoute-la dans Settings > Secrets and variables > Actions.\n"
            "  La cle se cree sur https://aistudio.google.com/apikey (gratuit)."
        )
    # Le SDK rejoue de lui-meme les 429, en silence et avec son propre backoff : sur un
    # quota journalier epuise, chaque appel mettait ainsi ~2 min a echouer, et un run a
    # atteint le timeout sans rien produire. Les reessais sont geres ici (with_retry),
    # le SDK ne rejoue plus que les 503 passagers.
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[503]),
        ),
    )


_RETRY_AFTER = re.compile(r"retry in ([\d.]+)s", re.I)
_ZERO_QUOTA = re.compile(r"limit:\s*0\b")
_DAILY_QUOTA = re.compile(r"per\s*day|perday", re.I)

# Modeles dont le quota a lache pendant ce run. On ne les resollicite plus : chaque
# sujet redescendait sinon toute la cascade en 429 avant d'atteindre un modele vivant.
_exhausted: set[str] = set()


def _is_retryable(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(token in blob for token in _RETRYABLE)


def is_unavailable(exc: Exception) -> bool:
    """Vrai quand le modele n'est pas accessible du tout sur ce compte.

    Les modeles Pro renvoient 'limit: 0' en free tier : le quota n'est pas epuise,
    il est inexistant. Reessayer ne changera rien, il faut passer au modele suivant.
    """
    return bool(_ZERO_QUOTA.search(str(exc)))


def _is_rate_limit(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(t in blob for t in ("429", "ratelimit", "resource_exhausted", "too_many_requests"))


def is_daily_quota(exc: Exception) -> bool:
    """Vrai quand c'est le quota du jour qui est epuise : attendre ne sert a rien."""
    return _is_rate_limit(exc) and bool(_DAILY_QUOTA.search(str(exc)))


def _suggested_delay(exc: Exception) -> float | None:
    """Delai d'attente propose par l'API, quand elle en donne un."""
    m = _RETRY_AFTER.search(str(exc))
    return float(m.group(1)) if m else None


def with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 75.0,
    label: str = "appel Gemini",
) -> Any:
    """Rejoue un appel sur indisponibilite passagere ou limite par minute.

    Deux regles apprises en conditions reelles :
    - un quota a zero n'est pas une erreur transitoire, on abandonne aussitot ;
    - quand l'API indique elle-meme un delai, on le respecte plutot que d'appliquer
      un backoff aveugle qui gaspille des requetes deja comptabilisees.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if is_unavailable(exc):
                log.warning("%s : modele indisponible sur ce compte (quota nul).", label)
                raise
            if is_daily_quota(exc):
                log.warning("%s : quota du jour epuise.", label)
                raise
            if not _is_retryable(exc) or attempt == attempts:
                raise
            hinted = _suggested_delay(exc)
            if hinted is not None:
                delay = min(hinted + 1.5, max_delay)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 2), max_delay)
            log.warning(
                "%s : echec %d/%d (%s). Attente %.0f s%s.",
                label, attempt, attempts, type(exc).__name__, delay,
                " (delai indique par l'API)" if hinted is not None else "",
            )
            time.sleep(delay)
    raise last  # type: ignore[misc]


def try_models(
    client: Any,
    model_ids: Iterable[str],
    call: Callable[[str], Any],
    *,
    label: str = "appel",
) -> Any:
    """Essaie plusieurs modeles dans l'ordre ; renvoie le premier succes.

    Utilise quand un identifiant preview disparait : on redescend la cascade au
    lieu de faire echouer tout l'episode.
    """
    errors: list[str] = []
    for model_id in model_ids:
        if model_id in _exhausted:
            errors.append(f"{model_id} (quota epuise plus tot dans ce run)")
            continue
        try:
            return with_retry(lambda: call(model_id), label=f"{label} [{model_id}]")
        except Exception as exc:
            if _is_rate_limit(exc):
                _exhausted.add(model_id)
            reason = "quota nul sur ce compte" if is_unavailable(exc) else type(exc).__name__
            # Le message complet est conserve : sur une erreur de requete, seul le
            # detail renvoye par l'API permet de comprendre ce qui est refuse.
            detail = " ".join(str(exc).split())[:220]
            errors.append(f"{model_id} ({reason}) : {detail}")
            log.warning("Modele %s inutilisable (%s), passage au suivant.", model_id, reason)
    raise RuntimeError(f"{label} : aucun modele utilisable.\n  " + "\n  ".join(errors))
