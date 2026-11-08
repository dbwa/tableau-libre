# Third-party notices

Tableau Libre includes or depends on the following third-party components.
Each component remains under its own license. The application code is MIT
licensed; see `LICENSE`. Full license texts are in `licenses/`.

The JavaScript libraries and fonts are **not committed** to the repository.
They are downloaded at install/build time by `scripts/fetch_vendor.sh`, with
pinned versions and SHA-256 checksums. The Python packages are listed in
`requirements.txt` and downloaded by pip at build time; no wheels or virtualenv
are committed.

| Component | Version | License | Project |
|---|---|---|---|
| Vega | 5.33.1 | BSD-3-Clause | https://vega.github.io/vega/ |
| Vega-Lite | 5.23.0 | BSD-3-Clause | https://vega.github.io/vega-lite/ |
| vega-embed | 6.29.0 | BSD-3-Clause | https://github.com/vega/vega-embed |
| MapLibre GL JS | 4.7.1 | BSD-3-Clause | https://maplibre.org/ |
| html2canvas | 1.4.1 | MIT | https://html2canvas.hertzen.com/ |
| FastAPI | >=0.115,<1 | MIT | https://fastapi.tiangolo.com/ |
| Uvicorn | >=0.30,<1 | BSD-3-Clause | https://www.uvicorn.org/ |
| lxml | >=5,<7 | BSD-3-Clause | https://lxml.de/ |
| python-multipart | >=0.0.9 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| SQLite | 3 | Public domain | https://www.sqlite.org/ |
| Python | 3 | PSF-2.0 | https://www.python.org/ |
| IBM Plex Serif | — | SIL OFL 1.1 | https://fonts.google.com/specimen/IBM+Plex+Serif |
| IBM Plex Sans | — | SIL OFL 1.1 | https://fonts.google.com/specimen/IBM+Plex+Sans |
| JetBrains Mono | — | SIL OFL 1.1 | https://www.jetbrains.com/lp/mono/ |
| Tableau Hyper API | 0.0.26963 | Apache-2.0 | https://github.com/tableau/hyper-api |

Les fichiers de polices sont récupérés depuis les paquets `@fontsource` (MIT) ;
les polices elles-mêmes restent sous SIL OFL 1.1.

## Tableau components

- **Tableau Hyper API** (`tableauhyperapi` 0.0.26963) is an Apache-2.0 Tableau
  component used only to read `.hyper` extracts. It is installed as a Python
  dependency and is not modified or relicensed by this project.
- The `.twb`, `.twbx`, `.hyper` and `.tdsx` file formats are read for
  interoperability. No Tableau Reader binary, source code or graphical asset is
  redistributed by this repository.

“Tableau” is a trademark of Salesforce. Its use here is nominative, to describe
file-format compatibility. This project is not affiliated with or endorsed by
Tableau Software or Salesforce.

License texts: `licenses/MIT.txt`, `licenses/BSD-3-Clause.txt`,
`licenses/Apache-2.0.txt`, `licenses/OFL-1.1.txt`, `licenses/PSF-2.0.txt`.

## Map data

The default map style is provided by OpenFreeMap, using OpenStreetMap data
(ODbL). MapLibre displays the required attribution. The style can be replaced
with `MAP_STYLE_URL`.
