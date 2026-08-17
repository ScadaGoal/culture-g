# Culture G

Un podcast de veille généré automatiquement chaque matin : IA en fil rouge, ouverture
science et tech. Deux voix en dialogue, dix à quinze minutes, livré par flux RSS sur le
téléphone.

Coût de fonctionnement : **0 €**.

## Comment ça marche

```
Flux RSS YouTube (3 chaînes) ─┐
Flux RSS texte (11 sources)  ─┤→ collect → curate → digest → script → tts → publish
Google Search (filet labos)  ─┘                                            │
                                                                            ▼
                                        GitHub Pages : feed.xml + episodes/*.mp3
                                                                            │
                                              app podcast sur le téléphone ─┘
```

| Étape | Rôle |
|---|---|
| `collect` | Lit les 14 flux, filtre par fenêtre temporelle, déduplique. Aucun appel LLM. |
| `curate` | Choisit 4 à 6 sujets parmi ~90 items et alloue le temps d'antenne. |
| `digest` | Analyse en profondeur : les vidéos YouTube partent en URL directe chez Gemini. |
| `script` | Écrit le dialogue à deux voix, nettoyé pour la synthèse vocale. |
| `tts` | Synthèse multi-locuteurs, PCM concaténé puis encodé une seule fois en MP3. |
| `publish` | Flux RSS podcast valide, page web, purge à 60 jours. |

Les vidéos ne sont jamais téléchargées : l'URL YouTube est passée à l'API Gemini, qui
récupère le média depuis les serveurs de Google. C'est ce qui permet au pipeline de
tourner en CI sans se faire bloquer par la détection anti-bot de YouTube.

## Utilisation

```bash
python -m culture_g.run --dry-run
```

Génère le script sans synthèse vocale. C'est la commande à utiliser pour régler la ligne
éditoriale : elle ne consomme pas de quota audio.

```bash
python -m culture_g.run
```

Épisode complet, publication comprise.

| Option | Effet |
|---|---|
| `--since 7d` | Élargit la fenêtre de collecte (défaut : 26 h). |
| `--dry-run` | Tout sauf la synthèse vocale et la publication. |
| `--ignore-seen` | Ne filtre pas les items déjà traités. |
| `--no-grounding` | Désactive la veille par recherche web. |
| `--check-models` | Liste les modèles disponibles sur le compte. |
| `--make-cover` | Régénère la pochette (nécessite Pillow). |
| `--from-script FICHIER` | Resynthétise un script déjà écrit, sans repasser par la génération de texte. |
| `--out MP3` | Chemin de sortie pour `--from-script`. |

### Comparer des voix

Changer `HOST` et `EXPERT` dans `config.py`, puis resynthétiser un script existant :

```bash
python -m culture_g.run --from-script state/script-2026-08-17.txt
```

Le fichier produit est nommé d'après les voix employées, ce qui permet d'empiler les
essais et de les comparer. Cette commande ne consomme que du quota audio : les étapes de
curation, d'analyse et d'écriture ne sont pas rejouées.

## Réglages

Tout se règle dans [`culture_g/config.py`](culture_g/config.py) :

- **Ajouter une chaîne YouTube** : récupérer son `channel_id` puis l'ajouter à
  `YOUTUBE_SOURCES`. L'identifiant se lit dans le lien canonique de la page de chaîne :
  `curl -sL https://www.youtube.com/@LaChaine | grep -o 'channel/UC[A-Za-z0-9_-]\{22\}'`
- **Ajouter une source texte** : une ligne dans `ARTICLE_SOURCES`. Le `weight` va de 1
  (retenu seulement si l'actu est faible) à 3 (quasi toujours retenu).
- **Changer les voix** : `HOST` et `EXPERT`. Trente voix Gemini sont disponibles ; le
  `tag` doit rester identique à l'étiquette utilisée dans le script.
- **Changer la durée** : `TARGET_MINUTES` et `TARGET_WORDS` (environ 150 mots la minute).
- **Changer le ton** : `EDITORIAL_LINE`, injectée en consigne système à la curation et à
  l'écriture.

## Installation locale

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Puis créer un fichier `.env` à la racine :

```
GEMINI_API_KEY=votre_cle
```

La clé se crée sur [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
gratuitement et sans carte bancaire.

`ffmpeg` est nécessaire pour l'encodage MP3. Sans lui le pipeline produit un WAV, bien
plus lourd, et le prévient dans les logs.

## Automatisation

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) déclenche la génération tous
les jours à 4 h UTC, soit 6 h à Paris en été. Le workflow a besoin :

- du secret `GEMINI_API_KEY` (Settings → Secrets and variables → Actions) ;
- de GitHub Pages activé sur la branche principale, dossier `/docs`.

Le déclenchement manuel se fait depuis l'onglet Actions (« Run workflow »), avec la
possibilité de changer la fenêtre ou de forcer un dry-run.

## Coût

| Poste | Consommation | Quota |
|---|---|---|
| GitHub Actions | ~3 min/jour, soit ~90 min/mois | 2000 min/mois gratuites |
| GitHub Pages | quelques centaines de Mo | 100 Go/mois de bande passante |
| API Gemini | ~10 requêtes/jour | free tier, + 10 $/mois de crédits inclus dans Google AI Pro |

## Quotas — ce que l'expérience a montré

Trois pièges constatés sur un compte free tier réel, tous encaissés par la cascade de
`culture_g/models.py` :

- **Les modèles Pro ont un quota free tier à zéro** (`limit: 0`). Ils échouent
  systématiquement, ce n'est pas une saturation passagère. Aucun n'est utilisé ici.
- **`gemini-2.5-flash` est encore listé par l'API mais renvoie 404** aux comptes récents.
  Le listing des modèles n'est donc pas une garantie de disponibilité.
- **Les modèles les plus récents plafonnent bas** (20 requêtes/jour pour `gemini-3.7-flash`).
  Le pipeline place l'étape la plus répétée sur `gemini-3.6-flash` et réserve les modèles
  récents à l'écriture, appelée une seule fois par jour.

Le budget quotidien réel est d'environ 7 requêtes texte et 3 requêtes audio.

## Limites connues

- Les modèles TTS sont en preview : leurs identifiants peuvent disparaître.
  `culture_g/models.py` gère une cascade de repli, et `--check-models` permet de vérifier
  ce que le compte expose réellement.
- Anthropic n'expose aucun flux RSS. Leurs annonces sont rattrapées via Hacker News et la
  recherche web de l'étape de curation.
- Les jours sans actualité notable produisent un épisode court plutôt qu'un remplissage.
