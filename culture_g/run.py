"""Orchestrateur du pipeline.

    python -m culture_g.run                 episode du jour, publication comprise
    python -m culture_g.run --dry-run       tout sauf la synthese vocale
    python -m culture_g.run --since 7d      fenetre de collecte elargie
    python -m culture_g.run --check-models  verifie les modeles disponibles sur le compte
    python -m culture_g.run --make-cover    genere la pochette du podcast
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone

from . import config
from .collect import collect, save_seen, load_seen
from .curate import curate, dump as dump_selection
from .digest import digest
from .models import CANDIDATES, available_models, make_client, resolve_all
from .publish import publish, register
from .script import show_notes, write_script
from .tts import synthesize
from .verify import verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("culture_g")


def _setup_console() -> None:
    """Force la sortie en UTF-8.

    La console Windows est en cp1252 par defaut : le moindre accent dans un titre
    d'article fait tomber le pipeline sur un UnicodeEncodeError, alors que le meme
    code passe sans probleme sur les runners Linux.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _load_dotenv() -> None:
    """Charge un .env a la racine, sans dependance supplementaire."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    # utf-8-sig : Notepad et PowerShell ecrivent volontiers un BOM en tete, qui se
    # collerait au nom de la premiere variable et la rendrait introuvable.
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _parse_window(value: str) -> int:
    """Convertit '26h', '7d' ou '48' en nombre d'heures."""
    m = re.fullmatch(r"(\d+)\s*([hdHD]?)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError("Format attendu : 26h, 7d ou un nombre d'heures.")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * 24 if unit == "d" else n


def _site_url() -> str:
    """URL publique du site, deduite de l'environnement Actions si disponible."""
    env = os.environ.get("SITE_URL")
    if env:
        return env.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        # Le nom de compte GitHub conserve sa casse, pas le sous-domaine Pages. Les
        # applications de podcast comparent les URL a la lettre : une majuscule ici
        # peut leur faire voir deux flux distincts.
        return f"https://{owner.lower()}.github.io/{name}"
    return config.SITE_URL.rstrip("/")


def cmd_check_models(paths: config.Paths) -> int:
    client = make_client()
    present = available_models(client)
    print(f"\n{len(present)} modele(s) exposes par ton compte.\n")
    ok = True
    for stage, options in CANDIDATES.items():
        pick = next((o for o in options if o in present), None)
        if pick:
            print(f"  {stage:8} -> {pick}")
        else:
            ok = False
            print(f"  {stage:8} -> AUCUN candidat disponible")
            print(f"             cherches : {', '.join(options)}")
    if not ok:
        print("\nAucun candidat pour une etape : ouvre culture_g/models.py et ajoute")
        print("un identifiant present dans la liste ci-dessous.")
        tts_like = sorted(m for m in present if "tts" in m or "audio" in m)
        if tts_like:
            print(f"\nModeles audio detectes : {', '.join(tts_like)}")
    resolve_all(client, paths.state, refresh=True)
    print(f"\nChoix enregistres dans {os.path.join(paths.state, 'models.json')}")
    return 0 if ok else 1


SCRIPT_SEPARATOR = "-" * 60


def _read_script_file(path: str) -> tuple[str, str, str]:
    """Relit un script sauvegarde : titre, teaser, dialogue."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    head, sep, body = raw.partition(SCRIPT_SEPARATOR)
    if not sep:
        # Fichier ne contenant que du dialogue.
        return "Essai de voix", "", raw.strip()
    lines = [l for l in head.strip().splitlines() if l.strip()]
    title = lines[0] if lines else "Essai de voix"
    teaser = " ".join(lines[1:]) if len(lines) > 1 else ""
    return title, teaser, body.strip()


def cmd_from_script(paths: config.Paths, path: str, out: str | None) -> int:
    """Synthetise un script existant, sans repasser par la generation de texte.

    Sert a comparer des voix : on change HOST/EXPERT dans config.py et on relance,
    sans redepenser le quota des etapes de curation et d'ecriture.
    """
    if not os.path.exists(path):
        log.error("Script introuvable : %s", path)
        return 1

    title, _, dialogue = _read_script_file(path)
    client = make_client()
    models = resolve_all(client, paths.state, refresh=False)

    voices = "-".join(s.voice.lower() for s in config.SPEAKERS)
    out_path = out or os.path.join(paths.episodes, f"essai-{voices}.mp3")

    log.info("Script : %s (%d mots)", title, len(dialogue.split()))
    duration = synthesize(client, models["tts"], dialogue, out_path)
    print(f"\nAudio genere : {out_path}  ({duration / 60:.1f} min)")
    return 0


def cmd_make_cover(paths: config.Paths) -> int:
    from .cover import make_cover

    out = os.path.join(paths.docs, "cover.png")
    make_cover(out)
    print(f"Pochette generee : {out}")
    return 0


def run(args: argparse.Namespace) -> int:
    paths = config.Paths(ROOT)
    os.makedirs(paths.episodes, exist_ok=True)
    os.makedirs(paths.state, exist_ok=True)

    if args.check_models:
        return cmd_check_models(paths)
    if args.make_cover:
        return cmd_make_cover(paths)
    if args.from_script:
        return cmd_from_script(paths, args.from_script, args.out)

    client = make_client()
    models = resolve_all(client, paths.state, refresh=args.refresh_models)
    log.info("Modeles : %s", ", ".join(f"{k}={v}" for k, v in models.items()))

    # 1. Collecte -------------------------------------------------------------
    items = collect(paths.state, args.since, apply_seen=not args.ignore_seen)
    if not items:
        log.info("Aucune nouveaute sur la fenetre demandee. Pas d'episode aujourd'hui.")
        return 0

    # 2. Curation -------------------------------------------------------------
    selection = curate(client, models["curate"], items, use_grounding=not args.no_grounding)
    if not selection.topics:
        log.info("Aucun sujet n'a passe la selection editoriale. Pas d'episode.")
        return 0
    if args.verbose:
        log.info("Selection :\n%s", dump_selection(selection))

    # 3. Analyse --------------------------------------------------------------
    dossiers = digest(client, models["digest"], selection.topics, items)

    # 4. Ecriture -------------------------------------------------------------
    episode = write_script(client, models["script"], selection.headline, dossiers)

    # 5. Verification factuelle -----------------------------------------------
    if not args.no_verify:
        episode.script, corrections = verify(
            client, models["verify"], episode.script, dossiers
        )
        if corrections:
            corr_path = os.path.join(paths.state, f"corrections-{datetime.now():%Y-%m-%d}.txt")
            with open(corr_path, "w", encoding="utf-8") as fh:
                for c in corrections:
                    fh.write(f"[{c.motif}]\n  avant : {c.original}\n  apres : {c.corrige}\n\n")

    notes = show_notes(dossiers)

    today = datetime.now(timezone.utc)
    slug = today.strftime("%Y-%m-%d")

    script_path = os.path.join(paths.state, f"script-{slug}.txt")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(f"{episode.title}\n\n{episode.teaser}\n\n{'-' * 60}\n\n{episode.script}")
    log.info("Script ecrit : %s", script_path)

    # Notes d'analyse conservees : c'est la piece a conviction pour remonter a la
    # source quand une affirmation de l'episode parait douteuse.
    with open(os.path.join(paths.state, f"notes-{slug}.txt"), "w", encoding="utf-8") as fh:
        for d in dossiers:
            sources = "\n".join(f"  {s.source} : {s.url}" for s in d.get("sources", []))
            fh.write(f"{'=' * 60}\n{d['topic'].title}\n{sources}\n{'=' * 60}\n{d['notes']}\n\n")

    if args.dry_run:
        print("\n" + "=" * 68)
        print(f"  {episode.title}")
        print("=" * 68)
        print(f"\n{episode.teaser}\n")
        print("-" * 68)
        print(episode.script)
        print("-" * 68)
        print(f"\n{notes}\n")
        log.info("Mode dry-run : synthese vocale et publication ignorees.")
        return 0

    # 5. Synthese vocale ------------------------------------------------------
    audio_name = f"{slug}.mp3"
    audio_path = os.path.join(paths.episodes, audio_name)
    duration = synthesize(client, models["tts"], episode.script, audio_path)

    # 6. Publication ----------------------------------------------------------
    episodes = register(
        paths.docs,
        slug=slug,
        title=episode.title,
        teaser=episode.teaser,
        notes=notes,
        audio_filename=audio_name,
        duration=duration,
        published=today,
    )
    publish(paths.docs, episodes, _site_url())

    # Marquer comme vus seulement maintenant : si le pipeline casse avant, les items
    # repasseront demain au lieu d'etre perdus.
    seen = load_seen(paths.state)
    save_seen(paths.state, seen, [i.uid for i in items])

    log.info("Episode publie : %s (%.1f min)", episode.title, duration / 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    _load_dotenv()

    parser = argparse.ArgumentParser(
        prog="culture_g", description="Genere l'episode quotidien de Culture G."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="tout sauf la synthese vocale et la publication")
    parser.add_argument("--since", type=_parse_window, default=config.DEFAULT_WINDOW_HOURS,
                        metavar="DUREE", help="fenetre de collecte, ex. 26h ou 7d")
    parser.add_argument("--ignore-seen", action="store_true",
                        help="ne pas filtrer les items deja traites")
    parser.add_argument("--no-grounding", action="store_true",
                        help="desactiver la veille par recherche web")
    parser.add_argument("--no-verify", action="store_true",
                        help="sauter la verification factuelle du script")
    parser.add_argument("--refresh-models", action="store_true",
                        help="reinterroger le compte pour les identifiants de modeles")
    parser.add_argument("--check-models", action="store_true",
                        help="lister les modeles disponibles et sortir")
    parser.add_argument("--make-cover", action="store_true",
                        help="generer la pochette du podcast et sortir")
    parser.add_argument("--from-script", metavar="FICHIER",
                        help="synthetiser un script deja ecrit (pour comparer des voix)")
    parser.add_argument("--out", metavar="MP3",
                        help="chemin du fichier audio produit par --from-script")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("Interrompu.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
