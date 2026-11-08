# Notes de compatibilité Tableau

> Document de travail sur l’interopérabilité des formats. Aucun code source
> Tableau n’est reproduit ici. Les noms de types, formats et palettes sont des
> faits nécessaires à la lecture des fichiers `.twb`.

Références d’observation : API publique et documentation des formats Tableau.

## Types de marque (`MarkType`)

```
bar, line, area, square, circle, shape, text, map, pie,
gantt-bar, polygon, heatmap, viz-extension
```

Dans le `.twb`, `<mark class='Bar'/>` utilise la casse CamelCase
(`Automatic`, `Bar`, `Line`, `Square`, `Circle`, `Text`, `Map`, `Pie`,
`GanttBar`, `Polygon`, `Heatmap`).

## Agrégations (`FieldAggregationType`)

| Famille | Valeurs |
|---|---|
| numériques | `sum`, `avg`, `min`, `max`, `median`, `attr` |
| dispersion | `stdev`, `stdevp`, `var`, `varp` |
| comptage | `count`, `countd`, `collect` |
| dates (parties) | `year`, `qtr`, `month`, `day`, `hour`, `minute`, `second`, `week`, `weekday`, `month-year`, `mdy` |
| dates (troncature) | `trunc-year`, `trunc-qtr`, `trunc-month`, `trunc-week`, `trunc-day`, `trunc-hour`, `trunc-minute`, `trunc-second` |
| divers | `none`, `quart1`, `quart3`, `skewness`, `kurtosis`, `in-out`, `end` |

Dans le `.twb` (étagères `<rows>`/`<cols>`), la forme est
`[datasource].[aggregation:Champ:type]` avec `type` ∈ `nk` (nominal),
`ok` (ordinal), `qk` (quantitatif).

## Encodages (`EncodingType`)

```
column, row, page, filter, marks-type, measure-values,
color, size, label, detail, tooltip, shape, path, angle,
geometry, custom
```

## Formats (`DataFormatType`)

```
auto-format, system-number, system-currency, system-long-date,
system-short-date, system-time, custom, custom-number,
custom-currency, custom-scientific, custom-percentage
```

## Palette catégorielle « Tableau 10 »

```
#4E79A7  #F28E2B  #E15759  #76B7B2  #59A14F
#EDC948  #B07AA1  #FF9DA7  #9D7660  #BAB0AC
```

(palette catégorielle historique Tableau)

## Ce que ça change pour notre lecteur

- `mark` : `bar`/`line`/`area`/`circle`/`shape`/`square`/`heatmap`/`pie`
  ont une traduction Vega-Lite directe ; `map`/`polygon` restent en tableau.
- agrégations : `stdev*`, `var*`, `median` sont supportées par Hyper.
- parties de date : `date_trunc()` en SQL, `temporal` côté Vega.
- palette : configurée via `config.range.category`.
