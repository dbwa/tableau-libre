# TODO — ce qui reste

## Fait

- LOD `{FIXED}`, `{INCLUDE}`, `{EXCLUDE}` (fenêtres SQL + agrégation)
- calculs de table : `RUNNING_*`, `WINDOW_*`, `RANK*`, `INDEX`, `LOOKUP`,
  `TOTAL`, `FIRST`, `LAST`, `SIZE`
- `SPLIT`, `REGEXP_*`, `FIND`, `PROPER`, `ASCII`, `CHAR`, `DATENAME`,
  `MAKEDATE`, `MAKEDATETIME`, `NOW`, `TODAY`, `IIF`, `^`, `%`
- groupes, bins, ensembles (`IN` / `NOT IN`), hiérarchies (drill-down)
- agrégats `cnt`, `ctd`, `med`, `quart1`, `quart3`
- parties de date (`yr`, `mn`, `dy`, `wd`, `hr`…) et troncatures
  (`tyr`, `tdy`, `md`…) distinctes, comme dans Tableau
- filtres : catégoriels (liste, dropdown, cases, curseur), plages, dates
  relatives, jokers, top N, contexte, **filtres par formule** avec paramètres
- `COUNT(*)` implicite pour les vues sans mesure
- cartes de filtre et de paramètre dans les dashboards
- actions : filtre, surbrillance, URL, aller-à-la-feuille **ou au dashboard**,
  paramètre, ensemble ; `on-select`, `on-hover`, `on-menu`
- double axe, Gantt, barres groupées, heatmap, camembert, tendance,
  **plusieurs mesures** (une série par mesure), **treemap**
- totaux de crosstab, tris manuels, bandes de référence, info-bulles
- tableaux croisés **paginés** (500 lignes) et **colorés** (highlight table)
- dashboards : fond, textes enrichis (sauts de ligne, alignement), images,
  iframes, boutons, légendes couleur **et taille**, titres de feuille,
  marges/padding de zone
- **layouts téléphone/tablette** (`devicelayouts`) selon la largeur
- stories (storyboards) avec navigation et **filtres capturés par étape**
- alias de membres, alias de mesures (`:Measure Names`), alias de paramètres
- export CSV / PNG / PDF
- cache de session, Docker, assets tiers téléchargés à l’installation
- expiration affichée, mot de passe optionnel (`ACCESS_PASSWORD`)
- fond de carte OpenFreeMap + repli raster, couleur constante bien rendue

### Hébergement (étape 2)

- comptes e-mail / mot de passe (scrypt), sessions révocables, CSRF
- espace personnel avec quota, dépôt / remplacement / renommage / suppression
- mise à jour des données seules (`.hyper` / `.tdsx`) sans changer le `.twb`
- liens de partage : expiration, mot de passe, iframe, révocation, compteur de vues
- API Bearer `/api/v1` (liste, dépôt, remplacement, PATCH, suppression, partages)
- lecteur public éphémère lié à un cookie de session, purge à la fin de session
- purge périodique (sessions, partages expirés, classeurs expirés, orphelins)

## Restant (hors périmètre ou absent des classeurs fournis)

### Hébergement
- vérification d'adresse e-mail, réinitialisation de mot de passe
- interface d'administration (quotas, comptes, supervision)
- rôles / partage entre comptes, journal d'audit
- stockage objet (S3) au lieu du disque local
- limitation de débit de l'API, scopes de jetons
- HTTPS / reverse proxy : délégué à l'infrastructure (documenté)

### Rendu
- prévisions (aucune prévision active dans les classeurs fournis ; l'algorithme
  de lissage exponentiel de Tableau n'est pas reproduit)
- box plots, nuages de mots, `viz-extension`
- blending multi-sources au niveau viz (au-delà des jointures d'un même extrait)

### Données
- filtres `crossjoin` / `intersection` multi-champs (les membres simples,
  `union`, `except` et les filtres d'action dynamiques sont gérés)
- `RAWSQL` refusé : les extraits n'ont pas de base sous-jacente

### Performance
- simplification géométrique des polygones très détaillés

### Cartes
- tuiles hors connexion
- étiquettes de géométrie, regroupement de points

### Déploiement
- HTTPS / reverse proxy (délégué à l'infrastructure, documenté dans le README)
