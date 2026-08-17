"""Verification factuelle du script contre les notes d'analyse.

Le pipeline reformule deux fois : une fois pour produire des notes a partir des
sources, une fois pour ecrire le dialogue a partir des notes. Chaque reformulation
peut deriver, et l'experience montre que ce sont les chiffres qui trinquent en
premier : un montant de 1,5 milliard de dollars ressorti en 1,3 milliard d'euros.

Cette passe relit le dialogue en le confrontant aux notes, et corrige tout ce qui
n'y est pas litteralement soutenu. Elle coute une requete par episode.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .models import ladder, try_models

log = logging.getLogger(__name__)


class Correction(BaseModel):
    original: str = Field(description="Le passage fautif, copie a l'identique du script.")
    corrige: str = Field(description="Le passage corrige, fidele aux notes.")
    motif: str = Field(description="En quelques mots : chiffre errone, devise changee, "
                                   "attribution absente des notes, affirmation non sourcee.")


class Verification(BaseModel):
    corrections: list[Correction] = Field(
        description="Ecarts constates entre le script et les notes. Liste vide si tout est fidele."
    )
    script_corrige: str = Field(
        description="Le dialogue complet apres corrections, format et etiquettes de "
                    "locuteur inchanges."
    )


PROMPT = """Voici les notes de preparation, seule source de verite, puis le dialogue
ecrit a partir d'elles.

=== NOTES DE PREPARATION (source de verite) ===
{notes}

=== DIALOGUE A VERIFIER ===
{script}

=== TA MISSION ===

Confronte chaque affirmation du dialogue aux notes. Corrige tout ce qui ne s'y trouve pas.

Points de controle, par ordre d'importance :

1. CHIFFRES. Chaque montant, pourcentage, date, quantite ou score doit apparaitre a
   l'identique dans les notes. C'est le point de defaillance le plus frequent.
2. DEVISES ET UNITES. Ne jamais convertir. Si les notes disent dollars, le dialogue dit
   dollars. Idem pour les unites de mesure.
3. NOMS PROPRES. Personnes, entreprises, produits, publications : orthographe et
   attribution exactes. Ne pas attribuer a quelqu'un un propos que les notes attribuent
   a un autre.
4. AFFIRMATIONS NON SOURCEES. Toute affirmation factuelle absente des notes doit etre
   supprimee, ou transformee en formulation prudente ("il semblerait", "selon telle
   source") si elle reste plausible et utile au recit.
5. CERTITUDE EXCESSIVE. Si les notes presentent un element comme incertain, conteste ou
   preliminaire, le dialogue ne doit pas l'affirmer comme etabli.

Ce que tu ne dois PAS faire :
- Ne reecris pas pour le style. Seule la fidelite factuelle est en jeu.
- Ne supprime pas les opinions ou analyses clairement presentees comme telles.
- Ne touche pas aux etiquettes "Animateur:" et "Expert:" ni au decoupage des repliques.
- N'ajoute ni markdown, ni URL, ni symbole : le texte part dans un moteur vocal.

Rends le dialogue complet corrige, ainsi que la liste des corrections effectuees.
"""


def verify(client: Any, model: str, script: str, dossiers: list[dict]) -> tuple[str, list[Correction]]:
    """Corrige le script d'apres les notes. Renvoie le script et les corrections."""
    notes = "\n\n".join(
        f"--- {d['topic'].title}\n{d['notes']}" for d in dossiers if d.get("notes")
    )
    if not notes:
        log.warning("Aucune note disponible : verification impossible.")
        return script, []

    prompt = PROMPT.format(notes=notes, script=script)

    try:
        interaction = try_models(
            client,
            ladder("verify", model),
            lambda model_id: client.interactions.create(
                model=model_id,
                input=prompt,
                system_instruction=(
                    "Tu es verificateur des faits. Tu confrontes un texte a ses sources "
                    "et tu corriges les ecarts. Tu ne juges pas le style, seulement "
                    "l'exactitude. Dans le doute, tu retires plutot que d'affirmer."
                ),
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": Verification.model_json_schema(),
                },
                generation_config={"thinking_level": "high", "max_output_tokens": 16000},
            ),
            label="verification factuelle",
        )
        result = Verification.model_validate_json(interaction.output_text)
    except Exception as exc:
        # Un episode non verifie vaut mieux que pas d'episode : on laisse passer en
        # le signalant clairement dans les logs.
        log.warning("Verification impossible (%s) : script publie sans relecture.", exc)
        return script, []

    corrected = result.script_corrige.strip()

    # Garde-fou : si la passe a ampute le script, on garde l'original. Une verification
    # qui detruit le contenu est pire que pas de verification du tout.
    ratio = len(corrected.split()) / max(len(script.split()), 1)
    if ratio < 0.75:
        log.warning(
            "Verification rejetee : script reduit de %.0f %% (probable derapage). "
            "Version originale conservee.", (1 - ratio) * 100,
        )
        return script, []

    if result.corrections:
        log.info("Verification : %d correction(s) factuelle(s).", len(result.corrections))
        for c in result.corrections:
            log.info("   [%s] %s", c.motif, c.original[:70])
    else:
        log.info("Verification : aucun ecart detecte.")

    return corrected, result.corrections
