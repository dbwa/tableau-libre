# Tableau Libre

**Lecteur et hébergeur auto-hébergé de classeurs Tableau.**

Ouvre un `.twbx`, un `.twb` accompagné de ses `.hyper` / `.tdsx`, dans le
navigateur, sans Tableau installé. Les fichiers restent sur votre machine.

Deux usages : **lecture immédiate et éphémère** (sans compte), ou **hébergement**
avec compte, liens de partage, iframe, expiration, protection par mot de passe,
whitelist IP, clé d’URL et API. Voir « Deux modes ».

L’interface utilise un **style rétro** (titres IBM Plex Serif, texte IBM Plex Sans,
libellés JetBrains Mono, palette papier/rouille/moutarde, ombres franches) et un
**thème clair/sombre** mémorisé dans le navigateur, avec un balayage rapide au
changement de thème. Les polices et les bibliothèques JavaScript sont
téléchargées à l’installation puis auto-hébergées dans `app/static/`.
Le thème ne modifie jamais les couleurs des graphiques. Il s’applique aux pages
de l’application ; le lecteur garde volontairement le style « Tableau Reader »
d’origine.

## Sommaire

- [Lancer](#lancer)
- [Deux modes](#deux-modes)
- [Administration](#administration)
- [API](#api)
- [Architecture](#architecture)
- [Formats acceptés](#formats-acceptés)
- [Parcours](#parcours)
- [Ce qui est rendu](#ce-qui-est-rendu)
- [Interactions](#interactions)
- [Fidélité de rendu](#fidélité-de-rendu)
- [Cartes](#cartes)
- [Champs calculés](#champs-calculés)
- [Limites](#limites-assumées)
- [Tests](#tests)
- [Sécurité](#sécurité)
- [Crédits et licences](#crédits-et-licences)
- [Indépendance et marques](#indépendance-et-marques)
- [Licence](#licence)

## Lancer

### Prérequis

- Python **3.11+** (3.12 recommandé) ;
- `curl` et `sha256sum` (ou `shasum`/`openssl`) pour télécharger les assets tiers ;
- environ 2 Go d’espace disque pour les dépendances et les fichiers ;
- pour les cartes : accès réseau au fond OpenFreeMap, ou un style MapLibre
  alternatif.

### En local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/fetch_vendor.sh
cd app
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Puis <http://127.0.0.1:8000> et déposer les fichiers. La liste des fichiers
sélectionnés permet de les retirer ou d’en ajouter avant l’envoi.

### Docker

Le `Dockerfile` et le `docker-compose.yml` sont prêts pour un lancement simple,
notamment sur un serveur de travail où Docker est autorisé.

```bash
cp .env.example .env
# éditer .env si besoin (URL publique, HTTPS, admin…)
docker compose up -d --build
```

Le build télécharge automatiquement les bibliothèques JavaScript et les polices
via `scripts/fetch_vendor.sh`.

Ouvrir <http://127.0.0.1:8000> (ou `http://IP_DU_SERVEUR:8000`). Le premier
compte créé sur `/signup` devient administrateur.

Les données sont conservées dans `./data/` : base SQLite, classeurs hébergés et
secret des cookies. Commandes utiles :

```bash
docker compose logs -f      # suivre les logs
docker compose down         # arrêter
docker compose up -d        # redémarrer
```

Si le port `8000` est déjà utilisé, modifier le mapping dans
`docker-compose.yml` :

```yaml
ports:
  - "8080:8000"
```

Derrière HTTPS/reverse proxy, renseigner dans `.env` :

```dotenv
PUBLIC_BASE_URL=https://tableau-libre.mondomaine.fr
COOKIE_SECURE=1
TRUST_PROXY=1
```

`TRUST_PROXY=1` ne doit être activé que si l'application est réellement
derrière un proxy de confiance. Sinon, un client pourrait falsifier l'en-tête
d'IP utilisé par la whitelist.

Mise à jour :

```bash
git pull
docker compose up -d --build
```

Sauvegarde des données :

```bash
docker compose down
tar czf tableaulibre-data-$(date +%F).tgz data
docker compose up -d
```

Si le serveur Docker n'a pas accès à Internet, construire l'image sur une
machine connectée puis la transférer :

```bash
# machine connectée
docker build -t tableaulibre .
docker save tableaulibre | gzip > tableaulibre.tar.gz

# serveur du travail
docker load < tableaulibre.tar.gz
docker run -d --name tableaulibre \
  -p 8000:8000 \
  -v "$PWD/data:/app/data" \
  --env-file .env \
  --restart unless-stopped \
  tableaulibre
```

Fond de carte : OpenFreeMap par défaut (aucune clé, aucune inscription).
Remplaçable par n'importe quel style MapLibre via `MAP_STYLE_URL`.

## Deux modes

### 1. Lecture immédiate, sans compte (éphémère)

La page d'accueil permet de déposer un classeur et de le lire tout de suite.
Les fichiers sont liés à un **cookie de session** :

- suppression automatique après **4 h sans activité** (`PUBLIC_TTL_HOURS`) ;
- bouton **« Terminer »** dans l'en-tête du lecteur et **« Terminer la session »**
  sur l'accueil : suppression immédiate ;
- un classeur public n'est pas accessible avec l'URL seule depuis un autre
  navigateur (le cookie de session est requis) ;
- les anciens dossiers de `data/` restent accessibles par URL jusqu'à leur
  expiration (7 jours), pour compatibilité.

### 2. Hébergement avec compte

`/signup` crée un compte (e-mail + mot de passe). Le premier compte créé est
administrateur ; `ADMIN_EMAILS` permet d'en promouvoir d'autres. Chaque compte
dispose de :

- un **espace personnel** avec quota (2 Go par défaut, `DEFAULT_QUOTA_BYTES`) ;
- l'**hébergement** de plusieurs classeurs, avec remplacement des fichiers sans
  changer l'identifiant ni les liens de partage ;
- des **liens de partage** révocables, avec expiration, mot de passe optionnel
  et autorisation d'**iframe** ;
- une **API Bearer** pour lister, déposer, remplacer, renommer, supprimer des
  classeurs et gérer les partages.

Les fichiers hébergés sont dans `data/hosted/<utilisateur>/<classeur>/`, les
métadonnées dans `data/hosted.db` (SQLite). Un classeur expiré est supprimé du
disque à la purge suivante (toutes les 10 minutes).

#### Administration

Le premier compte créé est administrateur. La page `/admin` donne une vue
d'ensemble (utilisateurs, stockage privé/public, classeurs, liens, jetons) et
permet de modifier le quota, promouvoir/rétrograder un administrateur ou
supprimer un compte. `ADMIN_EMAILS` permet de promouvoir d'autres comptes.

Les confirmations de suppression sont affichées dans une boîte de dialogue du
site, pas via le `confirm()` du navigateur.

#### API

Créez un jeton dans **Mes classeurs → Jetons d'API** (il n'est affiché qu'une
fois). Toutes les routes sont sous `/api/v1` et utilisent
`Authorization: Bearer <jeton>`.

```bash
TOKEN=tbr_...
BASE=http://127.0.0.1:8000
H="Authorization: Bearer $TOKEN"
```

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/me` | compte, quota, stockage, nombre de classeurs/liens/jetons |
| `GET` | `/workbooks` | liste des classeurs |
| `POST` | `/workbooks` | créer un classeur (fichiers complets) |
| `GET` | `/workbooks/{id}` | détail + liens de partage |
| `PATCH` | `/workbooks/{id}` | renommer / changer l'expiration |
| `DELETE` | `/workbooks/{id}` | supprimer le classeur |
| `GET` | `/workbooks/{id}/files` | fichiers, tailles et types |
| `POST` | `/workbooks/{id}/files` | ajouter des fichiers |
| `PUT` | `/workbooks/{id}/files/{nom}` | remplacer un fichier (même nom) |
| `DELETE` | `/workbooks/{id}/files/{nom}` | supprimer un fichier |
| `GET` | `/shares` | tous les liens du compte |
| `POST` | `/workbooks/{id}/shares` | créer un lien |
| `PATCH` | `/shares/{id}` | modifier les conditions d'un lien |
| `DELETE` | `/shares/{id}` | révoquer un lien |

```bash
# compte
curl -H "$H" $BASE/api/v1/me

# classeurs
curl -H "$H" $BASE/api/v1/workbooks

# créer un classeur complet
curl -X POST -H "$H" \
  -F "files=@mon.twbx" -F "name=Mon tableau" \
  -F "expires=2027-01-01" \
  $BASE/api/v1/workbooks

# fichiers d'un classeur
curl -H "$H" $BASE/api/v1/workbooks/$ID/files

# remplacer un fichier : le fichier envoyé doit porter le même nom
curl -X PUT -H "$H" \
  -F "file=@mon_extrait.hyper" \
  $BASE/api/v1/workbooks/$ID/files/mon_extrait.hyper

# ajouter un fichier
curl -X POST -H "$H" \
  -F "files=@nouveau.hyper" \
  $BASE/api/v1/workbooks/$ID/files

# supprimer un fichier
curl -X DELETE -H "$H" \
  $BASE/api/v1/workbooks/$ID/files/nouveau.hyper

# créer un lien : libellé dans l'URL + mot de passe + IP + clé
curl -X POST -H "$H" -H "Content-Type: application/json" \
  -d '{"label":"Mon menu","password":"motdepasse123",
       "expires_at":"2027-01-01","ip_whitelist":"127.0.0.1, 10.0.0.0/8",
       "require_key":true,"allow_embed":true}' \
  $BASE/api/v1/workbooks/$ID/shares

# modifier les conditions d'un lien
curl -X PATCH -H "$H" -H "Content-Type: application/json" \
  -d '{"ip_whitelist":"192.168.1.0/24","iframe_only":true,
       "require_key":false,"remove_password":true}' \
  $BASE/api/v1/shares/$SHARE_ID
```

Un libellé `Mon menu` produit un lien de la forme
`/s/<jeton>-mon-menu`. Les conditions disponibles sont : expiration, mot de
passe, liste d'IP/CIDR, clé dans l'URL (`?key=...`), autorisation iframe, et
mode **iframe uniquement** (le lien direct est alors refusé). `GET /me` renvoie
aussi `is_admin`. La whitelist IP utilise l'adresse vue par le serveur ; derrière
un reverse proxy, configurez-le pour transmettre correctement l'adresse cliente.

La documentation OpenAPI interactive est sur `/docs` ; les exemples ci-dessus
montrent l'usage attendu de chaque route.

#### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `PUBLIC_TTL_HOURS` | `4` | inactivité avant purge d'une session publique |
| `MAX_PUBLIC_BYTES` | `5368709120` | stockage total des sessions publiques (5 Go) |
| `PUBLIC_MAX_FILES` | `20` | classeurs maximum par session publique |
| `MAX_FILES` | `200` | fichiers maximum par dépôt |
| `MAX_CACHE` | `64` | classeurs gardés en mémoire (LRU) |
| `DEFAULT_QUOTA_BYTES` | `2147483648` | quota par compte (2 Go) |
| `ADMIN_EMAILS` | vide | e-mails séparés par des virgules, promus admin |
| `PUBLIC_BASE_URL` | vide | URL publique absolue pour les liens/iframe/API derrière un proxy |
| `SECRET_KEY` | fichier `data/.secret` | signature des cookies de partage |
| `COOKIE_SECURE` | vide | `1` pour cookies `Secure` (HTTPS) |
| `ACCESS_PASSWORD` | vide | mot de passe global optionnel pour le lecteur public |
| `HOSTED_DB` | `data/hosted.db` | chemin SQLite |

## Architecture

```text
app/
  main.py       routes publiques, upload, sessions éphémères, export
  hosted.py     comptes, hébergement, partages, admin, API v1
  reader.py     lecture des .twb / .twbx, extraction Hyper, SQL
  calcs.py      traduction des champs calculés Tableau
  core.py       cache, finalisation, purge, chemins et constantes
  store.py      SQLite : comptes, sessions, classeurs, partages, jetons
  ui.py         layout, CSS rétro, thème clair/sombre, composants
  view.html     lecteur navigateur (Vega-Lite / MapLibre)
  static/       bibliothèques JS et polices téléchargées à l'installation
tests/          tests unitaires et tests de sécurité
data/           données locales (ignorées par Git)
docs/           notes de sémantique Tableau
```

Le serveur ne renvoie jamais les extraits au navigateur : les requêtes sont
exécutées côté serveur, puis les agrégats nécessaires au rendu sont transmis.

## Formats acceptés

| Dépôt | Cas |
|---|---|
| `.twbx` seul | classeur empaqueté (extrait inclus) |
| `.twb` + `.hyper` | classeur nu + son extrait (appariés par nom de fichier) |
| `.twb` + `.tdsx` | classeur + sources de données empaquetées (appariées par nom de source) |

Les connexions live et le format `.tde` sont refusés explicitement : le lecteur
ne peut pas embarquer les pilotes propriétaires.

## Parcours

Un `.twb` ne contient pas les données : il pointe vers des fichiers externes.

1. on dépose le **`.twb`** (ou un `.twbx`, qui contient tout) ;
2. si des fichiers manquent, le site liste les **`.hyper` / `.tdsx`** attendus,
   avec le nombre de feuilles concernées, et propose de les ajouter ;
3. on peut les ajouter **par lots successifs** ;
4. quand tout est là, le classeur est rendu et l'URL devient définitive.

Diagnostic en ligne de commande :

```bash
cd app
../.venv/bin/python reader.py --deps chemin/vers/fichier.twb
```

Les dépôts incomplets sont conservés 24 h (`PENDING_HOURS`) puis purgés.

## Ce qui est rendu

- **graphes** Vega-Lite : barres, lignes, aires, points, carrés/heatmap,
  camembert, barres groupées, **double axe**, **Gantt**, **treemap**,
  **plusieurs mesures** (une série par mesure), lignes de tendance
- **tableaux croisés** HTML multi-mesures, avec **totaux** ligne/colonne/général
  quand le classeur les active, tris manuels, **drill-down** de hiérarchie,
  **tableaux de mise en évidence** (couleur de cellule) et pagination au-delà
  de 500 lignes
- **KPI** quand la feuille n'a qu'une valeur
- **tableaux** (mark Text, trop de catégories pour un graphe lisible)
- **cartes** MapLibre + fond vectoriel OpenFreeMap
- **dashboards** avec zones positionnées, fond, textes enrichis (sauts de ligne,
  alignement), images, iframes, boutons de navigation, cartes de filtre, cartes
  de paramètre, légendes couleur et taille, titres de feuille, marges/padding
- **layouts téléphone/tablette** (`devicelayouts`) utilisés automatiquement
  selon la largeur de la fenêtre
- **zoom des feuilles** : le `entire-view` du `.twb` est respecté ; sans lui,
  la feuille garde sa taille naturelle et la zone défile (graphiques à
  nombreuses barres et tableaux longs restent lisibles, comme dans Tableau Reader)
- **taille du tableau de bord** : le `sizing-mode='automatic'` du `.twb` est
  respecté (le dashboard remplit la fenêtre) ; le sélecteur d'en-tête
  « Fenêtre / Largeur / Taille du classeur » permet de changer
- **stories** (storyboards) avec navigation par étapes et **filtres capturés**
  par étape (`capturedDeltas`)
- **notice** quand aucun champ n'est exploitable, avec la raison

## Interactions

- **paramètres** Tableau en listes / champs, valeur courante du `.twb` par défaut
- **filtres** : modes Tableau respectés — liste multi-sélection (défaut), liste
  déroulante, liste déroulante à cases, curseur ; plages numériques, dates
  relatives, jokers, top N, **filtres par formule** (`date([JOUR]) > [Paramètre]`) ;
  les cartes de filtre des dashboards sont actives
- **actions** : filtre, surbrillance, URL, aller-à-la-feuille ou au dashboard,
  paramètre, ensemble — déclenchées au clic, au survol (`on-hover`) ou au menu
  (`on-menu`)
- **filtre croisé** : cliquer une barre, une cellule, un en-tête ou une marque de
  carte filtre les feuilles liées ; bandeau avec ✕ pour retirer
- **sélection cartographique** : rectangle (▭), lasso (✎), zoom sur la sélection
  (⤢), recentrer (⌖), effacer (✕)
- **export** : CSV de la feuille, PNG de la vue, impression / PDF
- recherche d'onglets, navigation au clavier (← / →)

## Fidélité de rendu

- tri Tableau (`computed`, `manual`) → `ORDER BY` / ordre manuel
- **parties de date** (`year`, `month`, `day`, `weekday`, `hour`…) affichées
  comme dans Tableau (mois en toutes lettres, années sans séparateur de milliers)
  et **troncatures** (`day trunc`, `month trunc`…) en dates continues
- **en-têtes** : les `caption` du `.twb` sont utilisés tels quels ; un champ
  calculé non renommé affiche donc sa formule (comme Tableau Reader), avec la
  formule complète en info-bulle
- **COUNT(*) implicite** quand une vue n'a que des dimensions
- formats numériques (`p0.00%`, `c0.00`, `n0.00`, `*YYYY-MM-DD`)
- libellés Tableau (`caption`) partout : axes, en-têtes, légendes, « Valeur : … »
- étiquettes de marque (`mark-labels-show`)
- lignes de référence (`average`, `median`, `min`, `max`, `constant`)
  et **bandes de référence** (intervalle de confiance à 95 %)
- lignes de tendance (régression linéaire)
- palettes Tableau (`tableau10`, `tableau20`, `colorblind10`, dégradés
  divergents) et couleurs de marque du classeur
- tailles de marque (`<size>`), détail (`<lod>`), info-bulles personnalisées
- mise en forme des cellules (fond, taille de police) et des zones de texte
  (gras, italique, souligné, taille, couleur)
- colonnes tronquées (`JOUR (mois)`) et champs calculés gérés dans les filtres

## Cartes

- `geom` (type `spatial`) lu en **WKT** : `CAST(col AS VARCHAR)` côté Hyper
- WKT → GeoJSON (point, multi-point, ligne, multi-ligne, polygone, multi-polygone)
- rendu **MapLibre GL** avec fond **vectoriel OpenFreeMap** (schéma OpenMapTiles,
  gratuit, sans clé), coloration continue par la mesure, infobulle au survol
- fond remplaçable par n'importe quelle URL de style MapLibre via `MAP_STYLE_URL`
- repli automatique sur un fond raster OpenStreetMap si le style ne charge pas
- extraits **multi-tables** : jointures `<relationships>` appliquées
  (`COLLATE "C"` quand les deux côtés sont des chaînes)
- **couleur** : dégradé continu, échelle divergente dès que les négatifs pèsent
  vraiment (≥ 15 %), ou couleur par catégorie ; **taille** pilotée par `<size>`
- **légende** : dégradé gradué (min / milieu / max) ou pastilles catégorielles
- **sélection visible** : marques non sélectionnées grisées, sélectionnées avec
  liseré bleu Tableau `#1b96ff`
- centre, zoom et mode de sélection conservés entre deux rendus

Attribution obligatoire (données ODbL), affichée par le contrôle MapLibre :
« OpenFreeMap © OpenMapTiles · Data from OpenStreetMap ».

Limites : fond en ligne (pas de tuiles hors connexion), pas de simplification
géométrique, pas d'étiquettes de géométrie ni de regroupement de points,
plafond de 20 000 géométries par feuille.

## Champs calculés

Traduits : références `[Champ]`, paramètres `[Parameters].[X]`, arithmétique
(dont `^` et `%`), comparaisons, `IF/ELSEIF/END`, `CASE/WHEN/THEN`, `IIF`,
concaténation `+` sur chaînes, agrégats (`SUM`, `AVG`, `COUNT`, `COUNTD`,
`MEDIAN`, `STDEV`, `VAR`, `PERCENTILE`, `ATTR`), `ISNULL`, `IFNULL`, `ZN`,
`STR`, `INT`, `FLOAT`, `DATE`, `DATETIME`, `DATEPART`, `DATETRUNC`, `DATEDIFF`,
`DATEADD`, `DATEPARSE`, `DATENAME`, `MAKEDATE`, `MAKEDATETIME`, `NOW`, `TODAY`,
`LEFT/RIGHT/MID`, `LEN`, `FIND`, `SPLIT`, `UPPER/LOWER/PROPER/TRIM`, `REPLACE`,
`CONTAINS`, `STARTSWITH`, `ENDSWITH`, `REGEXP_MATCH/REPLACE/EXTRACT`, `ASCII`,
`CHAR`, `SPACE`, `REPEAT`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `POWER`, `SQRT`,
`EXP`, `LN`, `LOG`, `SIGN`, `PI`, `RANDOM`.

- **LOD** `{FIXED …}`, `{INCLUDE …}`, `{EXCLUDE …}` : calculés par fenêtres SQL
  dans une sous-requête, puis agrégés au niveau de la feuille
- **calculs de table** : `RUNNING_SUM/AVG/COUNT/MIN/MAX`, `WINDOW_*`, `RANK`,
  `RANK_DENSE`, `RANK_MODIFIED`, `RANK_UNIQUE`, `INDEX`, `LOOKUP`, `TOTAL`,
  `FIRST`, `LAST`, `SIZE`
- **groupes**, **bins** et **ensembles** (`IN`/`NOT IN`) traduits en SQL

Tout ce qui sort de ce sous-ensemble lève une erreur de calcul : la feuille
affiche un avertissement, jamais de chiffres faux. `RAWSQL` reste refusé
(les extraits n'ont pas de base sous-jacente). Les agrégats `cnt`, `ctd`,
`med`, `quart1`, `quart3` sont traduits.

## Limites (assumées)

- blending multi-sources au niveau viz (au-delà des jointures d'un même extrait)
- prévisions (aucune prévision active dans les classeurs fournis), box plots,
  nuages de mots, `viz-extension`
- un extrait par source ; `.tde` refusé ; connexions live refusées
- filtres `crossjoin` / `intersection` multi-champs non traduits (les membres
  simples, `union` et `except` le sont ; les filtres d'action sont dynamiques)
- crosstabs paginés au-delà de 500 lignes, requêtes à 50 000 lignes,
  20 000 lignes de crosstab
- pas de tuiles hors connexion, pas de simplification géométrique,
  pas d'étiquettes de géométrie ni de regroupement de points

Notes de compatibilité et sémantique des formats : `docs/tableau-semantics.md`.
Reste à faire : `TODO.md`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

- `tests/test_calcs.py` : traducteur de calculs
- `tests/test_store.py` : comptes, sessions, jetons, partages, quotas, public
- `tests/test_security.py` : échappement SQL, chemins d'extraits, purge,
  entrées d'API, redirections
- `tests/test_engine.py` : LOD, calculs de table, groupes, bins, ensembles,
  filtres plage et top N, sur un extrait Hyper synthétique
- `tests/test_reader.py` : rendu des classeurs réels, dashboards, stories,
  styles, filtres, sérialisation des types Hyper

Les tests sur classeurs réels sont ignorés si `import_donnees_exemples/` est absent.

## Rétention

- session publique : purge après **4 h d'inactivité** (`PUBLIC_TTL_HOURS`) ou
  immédiatement via « Terminer la session »
- anciens dossiers publics : purge au-delà de **7 jours** (`RETENTION_DAYS`)
- classeurs hébergés : suppression à leur date d'expiration, sinon permanents
- quota disque global de **20 Go** (`MAX_DATA_BYTES`) pour les anciens dossiers
  publics, purge du plus ancien
- quota par compte : **2 Go** par défaut (`DEFAULT_QUOTA_BYTES`)
- un `.tdsx` de plusieurs centaines de Mo laisse ~1,4 Go décompressé par dépôt

## Sécurité

- comptes : mots de passe hachés avec **scrypt** (sel aléatoire par compte),
  comparaison à temps constant ; aucune dépendance externe
- sessions : jeton aléatoire en cookie `HttpOnly`, `SameSite=Lax`, stocké haché
  en base, révocable ; expiration à 30 jours
- formulaires : **jeton CSRF** en double soumission, vérifié sur toutes les
  actions navigateur
- API : jetons `Bearer` stockés hachés, préfixe seul affiché, révocables ;
  chaque requête est limitée au propriétaire du classeur
- partages : jeton non devinable, expiration, mot de passe scrypt, cookie
  d'accès signé (HMAC) valable 12 h, révocation immédiate
- iframe : autorisée uniquement si le partage l'autorise ; en-tête
  `X-Frame-Options: SAMEORIGIN` sur le reste, absent sur `/embed/`
- un partage **protégé par mot de passe** est prévu pour une ouverture directe ;
  dans une iframe tierce, le navigateur peut bloquer le cookie d'accès
  (SameSite) : utilisez un partage sans mot de passe pour l'intégration
- limitation des tentatives de connexion (8 échecs / 15 min / IP + e-mail)
- HTTPS : terminer par un reverse proxy (nginx/Caddy) et poser
  `COOKIE_SECURE=1` + `PUBLIC_BASE_URL=https://…`
- zip : chemins validés, pas de symlink, 2 Gio décompressés max, 20 000 entrées max
- XML : entités externes et réseau désactivés (XXE)
- formules : analyse par jetons, tout ce qui n'est pas reconnu est rejeté
  (pas d'injection SQL par formule)
- chemins d'assets confinés au dossier du classeur (pas de traversée)
- extraits `.hyper` confinés au dossier du classeur (pas de `..` ni chemin absolu)
- unités de date et directions de tri validées par liste blanche ; paramètres
  date échappés avant SQL ; requêtes Hyper limitées à 30 s
- pages de lecture : **CSP avec nonce** (les classeurs sont traités comme des
  données non fiables) ; les scripts injectés par un classeur ne s'exécutent pas
- les extraits restent côté serveur, jamais envoyés au navigateur
- upload plafonné à 1,5 Gio
- cache de session par classeur (le `.twb` n'est pas reparsé à chaque requête)

## Crédits et licences

La page publique **`/credits`** liste les composants, leurs versions et leurs
licences. Les principales bibliothèques sont :

- **Vega 5.33.1**, **Vega-Lite 5.23.0**, **vega-embed 6.29.0** — BSD-3-Clause ;
- **MapLibre GL JS 4.7.1** — BSD-3-Clause ;
- **html2canvas 1.4.1** — MIT ;
- **FastAPI** — MIT ; **Uvicorn**, **lxml** — BSD-3-Clause ;
- **python-multipart** — Apache-2.0 ; **SQLite** — domaine public ;
- **Python** — PSF-2.0 ;
- **IBM Plex Serif**, **IBM Plex Sans**, **JetBrains Mono** — SIL OFL 1.1.

Les bibliothèques JavaScript et les polices ne sont **pas versionnées** : elles
sont téléchargées par [`scripts/fetch_vendor.sh`](scripts/fetch_vendor.sh), avec
des versions et des empreintes SHA-256 fixées. Docker exécute ce script pendant
le build. La liste complète et les liens sont dans
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) ; les textes de licence sont
dans [`licenses/`](licenses/). La note des polices est dans
`app/static/fonts/README.txt`.

**Tableau Hyper API** (`tableauhyperapi`, version `0.0.26963`) est un composant
Tableau open source sous **Apache-2.0**, utilisé comme dépendance pour lire les
extraits `.hyper`. Il n’est pas modifié par ce projet.

## Indépendance et marques

Tableau Libre est un projet indépendant, **sans affiliation avec Tableau
Software ou Salesforce**. Aucun binaire, code source ou ressource graphique de
**Tableau Reader** n’est intégré, copié ou redistribué.

La compatibilité repose sur les formats de fichiers (`.twb`, `.twbx`, `.hyper`,
`.tdsx`) et sur la Tableau Hyper API pour la lecture des extraits. Le rendu est
assuré par Vega-Lite et MapLibre. « Tableau » est une marque de Salesforce ;
son usage ici est nominatif, pour décrire la compatibilité.

## Dépôt Git

Le fichier [`.gitignore`](.gitignore) exclut les données locales (`data/`), les
classeurs et extraits, les environnements Python, les secrets, les assets tiers
téléchargés et les binaires d’installation ou d’analyse Tableau (`msi/`,
`burn/`, `wine/`, `vizclient/`, `attached.cab`, `TableauReader-*.exe`,
`extract_rcc.py`). Ces éléments ne doivent pas être publiés.

Avant un `git push`, vérifier aussi que `data/hosted.db`, `data/.secret` et les
classeurs téléversés ne sont pas suivis.

## Licence

Le code applicatif de ce dépôt est publié sous **licence MIT** (voir
[`LICENSE`](LICENSE)). Les composants tiers conservent leurs licences
respectives ; la licence MIT ne s’applique pas à la Tableau Hyper API
(Apache-2.0), aux marques, ni aux données ou styles cartographiques externes.
