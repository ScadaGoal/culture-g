"""Synthese vocale multi-locuteurs et encodage MP3.

Deux choix structurants ici :

1. On demande du PCM brut (audio/l16) plutot que du MP3 par segment. Concatener des
   trames MP3 laisse des micro-silences audibles a chaque jointure ; concatener du PCM
   est exact a l'echantillon pres. On encode une seule fois, a la fin.

2. On decoupe sur les frontieres de repliques, jamais au milieu d'une phrase, et on
   garde des segments assez longs pour que le modele ait le contexte necessaire a une
   prosodie coherente.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import wave
from typing import Any

from . import config
from .models import CANDIDATES, try_models
from .script import split_turns

log = logging.getLogger(__name__)

# ~420 mots, soit environ deux minutes trente d'audio par segment.
#
# La contrainte n'est pas la fenetre de contexte (32k tokens, tres loin d'etre
# atteinte) mais la derive de prosodie : sur une longue generation, la voix se degrade
# progressivement, puis repart nette au segment suivant. La rupture s'entend. Des
# segments courts limitent cette derive ; le surcout est nul, la synthese etant
# l'etape la moins contrainte en quota.
WORDS_PER_CHUNK = 420

# Fraction de la cible au-dela de laquelle on accepte de couper plus tot pour tomber
# sur une frontiere propre.
EARLY_CUT_RATIO = 0.7


def split_script(script: str, words_per_chunk: int = WORDS_PER_CHUNK) -> list[str]:
    """Decoupe le dialogue en segments, sur les frontieres les moins audibles.

    Une reprise de voix passe inapercue quand elle coincide avec une relance de
    l'animateur, qui porte deja un changement de ton. Elle s'entend, en revanche, au
    milieu d'un developpement de l'expert. On coupe donc de preference juste avant une
    replique de l'animateur, des lors qu'on a atteint une longueur raisonnable.
    """
    lines = [l for l in split_turns(script).splitlines() if l.strip()]
    host_prefix = f"{config.HOST.tag}:"

    chunks: list[str] = []
    current: list[str] = []
    count = 0

    for line in lines:
        n = len(line.split())
        starts_turn = line.startswith(host_prefix)
        early = current and starts_turn and count >= words_per_chunk * EARLY_CUT_RATIO
        full = current and count + n > words_per_chunk

        if early or full:
            chunks.append("\n".join(current))
            current, count = [], 0

        current.append(line)
        count += n

    if current:
        chunks.append("\n".join(current))

    # Un segment residuel de quelques repliques sonne differemment : le modele manque
    # de contexte pour asseoir le ton. On le rattache au precedent.
    if len(chunks) > 1 and len(chunks[-1].split()) < words_per_chunk * 0.4:
        chunks[-2] = chunks[-2] + "\n" + chunks[-1]
        chunks.pop()
    return chunks


def _speech_config() -> dict:
    # La langue n'est plus un parametre : les modeles 3.8 la detectent sur le texte.
    return {"speakers": [{"speaker": s.tag, "voice": s.voice} for s in config.SPEAKERS]}


# Consigne de jeu courte et constante. Les modeles 3.8 lisent le texte mot pour mot :
# une consigne glissee dans le transcript serait prononcee, et une consigne longue
# ou changeante fait deriver la voix (documentation Gemini TTS).
TURN_STYLE = "conversation de podcast, naturelle et posee"


def _turns(text: str) -> list[dict]:
    """Une entree par replique, le locuteur porte par speech_metadata.

    Les modeles 3.8 n'interpretent plus les prefixes "Animateur:" dans le texte : ils
    les liraient. Le locuteur passe donc en metadonnee, hors du transcript.
    """
    by_prefix = {f"{s.tag}:": s.tag for s in config.SPEAKERS}
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        prefix = next((p for p in by_prefix if line.startswith(p)), None)
        if prefix is None:
            continue
        spoken = line[len(prefix):].strip()
        if not spoken:
            continue
        items.append({
            "type": "text",
            "text": spoken,
            "annotations": [{
                "type": "speech_metadata",
                "speaker": by_prefix[prefix],
                "style": TURN_STYLE,
            }],
        })
    if not items:
        raise RuntimeError("Segment sans replique etiquetee : rien a synthetiser.")
    return items


def _wav_to_pcm(blob: bytes) -> bytes:
    """Extrait le PCM d'un WAV, en verifiant qu'il colle au format attendu."""
    with wave.open(io.BytesIO(blob), "rb") as wf:
        if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (
            1, 2, config.TTS_SAMPLE_RATE
        ):
            raise RuntimeError(
                f"WAV inattendu : {wf.getnchannels()} canal(aux), "
                f"{wf.getsampwidth() * 8} bits, {wf.getframerate()} Hz."
            )
        return wf.readframes(wf.getnframes())


def _extract_pcm(interaction: Any) -> bytes:
    """Recupere les octets audio, que le SDK les rende en base64 ou en binaire."""
    audio = getattr(interaction, "output_audio", None)
    if audio is None:
        raise RuntimeError("Reponse sans audio : le modele a probablement repondu en texte.")

    data = getattr(audio, "data", None)
    if data is None:
        raise RuntimeError("Champ audio present mais vide.")
    if isinstance(data, str):
        data = base64.b64decode(data)
    if not isinstance(data, bytes):
        raise RuntimeError(f"Type de donnees audio inattendu : {type(data).__name__}")

    # On concatene du PCM brut. Les modeles 3.8 renvoient du WAV par defaut : son
    # en-tete, colle entre deux segments, s'entendrait comme un clic. On le retire.
    mime = str(getattr(audio, "mime_type", "") or "")
    if data[:4] == b"RIFF" or "wav" in mime:
        return _wav_to_pcm(data)
    if mime and "l16" not in mime and "pcm" not in mime:
        raise RuntimeError(
            f"Format audio inattendu ({mime}) : la concatenation suppose du PCM brut."
        )
    return data


def _synthesize_chunk(client: Any, models: list[str], text: str, index: int, total: int) -> bytes:
    turns = _turns(text)

    def call(model_id: str):
        return client.interactions.create(
            model=model_id,
            input=[{"type": "user_input", "content": turns}],
            response_modalities=["audio"],
            # PCM brut explicite : sans mime_type, les modeles 3.8 rendent du WAV.
            response_format={
                "type": "audio",
                "mime_type": "audio/l16",
                "sample_rate": config.TTS_SAMPLE_RATE,
            },
            generation_config={"speech_config": _speech_config()},
        )

    interaction = try_models(client, models, call, label=f"synthese {index}/{total}")
    pcm = _extract_pcm(interaction)
    seconds = len(pcm) / (config.TTS_SAMPLE_RATE * 2)
    log.info("Segment %d/%d synthetise : %.1f s", index, total, seconds)
    return pcm


def _encode_mp3(pcm: bytes, out_path: str) -> None:
    """Encode le PCM en MP3 mono. Repli sur WAV si ffmpeg est absent."""
    if not shutil.which("ffmpeg"):
        wav_path = os.path.splitext(out_path)[0] + ".wav"
        log.warning("ffmpeg introuvable : ecriture en WAV (%s), fichier bien plus lourd.", wav_path)
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(config.TTS_SAMPLE_RATE)
            wf.writeframes(pcm)
        return

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "s16le", "-ar", str(config.TTS_SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        # Normalisation EBU R128 a -16 LUFS, le standard des podcasts. Le modele rend
        # un signal a environ -22 dB : audible au casque, trop faible dans une voiture.
        # loudnorm corrige la sonie percue sans ecreter, la ou un simple gain saturerait.
        "-af", f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11",
        "-codec:a", "libmp3lame", "-b:a", config.MP3_BITRATE, "-ac", "1",
        out_path,
    ]
    proc = subprocess.run(cmd, input=pcm, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg a echoue : {proc.stderr.decode(errors='replace')[:400]}")


def synthesize(client: Any, model: str, script: str, out_path: str) -> float:
    """Synthetise le script complet en un MP3. Renvoie la duree en secondes."""
    chunks = split_script(script)
    log.info("Synthese vocale : %d segment(s), voix %s",
             len(chunks), " + ".join(f"{s.tag}={s.voice}" for s in config.SPEAKERS))

    # Le modele resolu d'abord, puis le reste de la cascade en secours : les modeles
    # TTS sont en preview et peuvent disparaitre du jour au lendemain.
    ladder = [model] + [m for m in CANDIDATES["tts"] if m != model]

    # Synthese sequentielle : le free tier limite les requetes par minute, et les
    # segments doivent de toute facon etre concatenes dans l'ordre.
    pieces = [
        _synthesize_chunk(client, ladder, chunk, n, len(chunks))
        for n, chunk in enumerate(chunks, 1)
    ]

    pcm = b"".join(pieces)
    if not pcm:
        raise RuntimeError("Synthese vide.")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    _encode_mp3(pcm, out_path)

    duration = len(pcm) / (config.TTS_SAMPLE_RATE * 2)
    size_mb = os.path.getsize(out_path) / 1e6 if os.path.exists(out_path) else 0
    log.info("Audio final : %.1f min, %.1f Mo", duration / 60, size_mb)
    return duration
