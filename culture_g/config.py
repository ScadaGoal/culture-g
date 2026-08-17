"""Point de reglage unique du podcast.

C'est le seul fichier a editer au quotidien : ajouter une chaine, changer une voix,
rallonger les episodes. La logique du pipeline ne doit pas avoir besoin d'etre touchee.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Identite du podcast
# --------------------------------------------------------------------------

PODCAST_TITLE = "Culture G"
PODCAST_SUBTITLE = "Votre veille IA, tech et science, chaque matin"
PODCAST_DESCRIPTION = (
    "Un condense quotidien de l'actualite de l'intelligence artificielle, de la tech "
    "et de la science, monte en dialogue et genere automatiquement. L'IA en fil rouge, "
    "mais on ouvre large : histoire, sciences dures, societe."
)
PODCAST_AUTHOR = "Culture G"
PODCAST_LANGUAGE = "fr-FR"
PODCAST_CATEGORY = "Technology"

# Sert a construire les URL absolues du flux RSS : les lecteurs de podcast refusent
# les chemins relatifs. En execution GitHub Actions, l'URL est deduite automatiquement
# du depot ; cette valeur ne sert qu'aux executions locales.
SITE_URL = "https://scadagoal.github.io/culture-g"


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str  # "video" ou "article"
    weight: int = 1  # 3 = quasi toujours retenu, 1 = retenu si l'actu est faible
    lang: str = "fr"


def _yt(name: str, channel_id: str, weight: int = 3) -> Source:
    return Source(
        name=name,
        url=f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        kind="video",
        weight=weight,
    )


# Chaines YouTube : IDs verifies via le lien canonique de chaque page de chaine.
# Ce sont les sources choisies explicitement par l'utilisateur -> poids maximal.
YOUTUBE_SOURCES: list[Source] = [
    _yt("Underscore_", "UCWedHS9qKebauVIK2J7383g"),
    _yt("Grand Angle Nova", "UCkNZ-QtRIj0VepSoliDl_Bw"),
    _yt("Vision IA", "UCyc03X3uRuxM9n7fyRH_gIw"),
]

# Flux texte : tous testes (HTTP 200 + items frais). Ils garantissent un episode
# meme les matins ou aucune chaine n'a publie.
ARTICLE_SOURCES: list[Source] = [
    # Laboratoires : la source primaire pour "ne rien louper des sorties de modeles".
    Source("OpenAI", "https://openai.com/news/rss.xml", "article", 3, "en"),
    Source("Google DeepMind", "https://deepmind.google/blog/rss.xml", "article", 3, "en"),
    Source("Mistral AI", "https://mistral.ai/rss.xml", "article", 3, "en"),
    Source("Hugging Face", "https://huggingface.co/blog/feed.xml", "article", 2, "en"),
    # Tech generaliste. Hacker News sert aussi de filet pour Anthropic, qui n'expose
    # aucun flux RSS : leurs annonces montent systematiquement en une.
    Source("Hacker News", "https://hnrss.org/best", "article", 2, "en"),
    Source("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "article", 2, "en"),
    Source("MIT Tech Review", "https://www.technologyreview.com/feed/", "article", 2, "en"),
    # Science et culture : l'ouverture "culture generale large".
    Source("Quanta Magazine", "https://www.quantamagazine.org/feed/", "article", 2, "en"),
    Source("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml", "article", 1, "en"),
    # Presse FR.
    Source("Next.ink", "https://next.ink/feed/", "article", 2, "fr"),
    Source("Numerama", "https://www.numerama.com/feed/", "article", 2, "fr"),
]

ALL_SOURCES: list[Source] = YOUTUBE_SOURCES + ARTICLE_SOURCES

# Labos sans flux RSS exploitable : rattrapes via Google Search a l'etape de curation.
GROUNDING_WATCHLIST = [
    "Anthropic (Claude)",
    "xAI (Grok)",
    "Meta AI (Llama)",
    "DeepSeek",
    "Qwen / Alibaba",
]


# --------------------------------------------------------------------------
# Format editorial
# --------------------------------------------------------------------------

# Fenetre de collecte par defaut, en heures. 26 plutot que 24 : petit recouvrement
# pour absorber un run decale ou un flux publie juste apres le run precedent.
DEFAULT_WINDOW_HOURS = 26

TARGET_MINUTES = (10, 15)
# ~150 mots/minute en debit de podcast francais pose.
TARGET_WORDS = (1800, 2200)

MIN_TOPICS = 3
MAX_TOPICS = 6
# En dessous de ce nombre de sujets, on assume un episode court plutot que du remplissage.
SHORT_EPISODE_THRESHOLD = 3

EDITORIAL_LINE = """\
Ligne editoriale de "Culture G" :
- Fil rouge : l'intelligence artificielle (modeles, usages, acteurs, impacts).
- Ouverture assumee : sciences, histoire, societe, tech au sens large. L'auditeur veut
  se cultiver, pas seulement suivre l'actu IA.
- Priorite absolue aux sorties de modeles et annonces de laboratoires : c'est ce que
  l'auditeur ne veut jamais louper.
- On privilegie ce qui se raconte : un fait surprenant, un chiffre parlant, une histoire.
  On ecarte le communique de presse sans substance et le marketing deguise.
- Public : francophone, curieux, technophile mais pas forcement ingenieur. On peut dire
  "transformeur" ou "inference" en expliquant en une demi-phrase.
- Ton : vif, precis, sans emphase publicitaire. Jamais de superlatif gratuit.
"""


# --------------------------------------------------------------------------
# Voix
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Speaker:
    tag: str  # l'etiquette exacte utilisee dans le script, ex. "Animateur"
    voice: str  # nom d'une voix prebuilt Gemini
    persona: str


# Les etiquettes doivent correspondre au caractere pres a celles du script :
# le moteur multi-locuteurs apparie sur la chaine exacte.
HOST = Speaker(
    tag="Animateur",
    voice="Charon",
    persona=(
        "Anime l'episode. Pose les questions que se poserait l'auditeur, relance, "
        "assure les transitions. Curieux et rythme, jamais complaisant."
    ),
)
EXPERT = Speaker(
    tag="Expert",
    voice="Aoede",
    persona=(
        "Apporte la substance : contexte, chiffres, nuance, consequences concretes. "
        "Pedagogue sans etre condescendante. Sait dire quand une annonce est du vent."
    ),
)

SPEAKERS: list[Speaker] = [HOST, EXPERT]

TTS_LANGUAGE = "fr-FR"
# PCM brut demande au modele : on concatene les segments sans artefact, puis on encode
# une seule fois en MP3. Concatener directement du MP3 laisse des micro-blancs audibles.
TTS_SAMPLE_RATE = 24000
MP3_BITRATE = "64k"  # mono, largement suffisant pour de la voix et leger en 4G
# Sonie cible, en LUFS. -16 est la norme des podcasts mono ; descendre a -14 rend
# l'ecoute plus confortable dans une voiture bruyante, au prix d'un peu de dynamique.
TARGET_LUFS = -16


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

KEEP_EPISODES_DAYS = 60


@dataclass
class Paths:
    """Arborescence du projet, resolue depuis la racine du depot."""

    root: str
    docs: str = field(init=False)
    episodes: str = field(init=False)
    state: str = field(init=False)

    def __post_init__(self) -> None:
        import os

        self.docs = os.path.join(self.root, "docs")
        self.episodes = os.path.join(self.docs, "episodes")
        self.state = os.path.join(self.root, "state")
