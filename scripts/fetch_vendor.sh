#!/bin/sh
# Télécharge les bibliothèques JS et les polices utilisées par le lecteur.
# Les fichiers ne sont pas versionnés : ils sont récupérés à l'installation
# (ou au build Docker), avec une version et une empreinte SHA-256 fixées.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$ROOT/app/static"
mkdir -p "$DEST/fonts"

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    fi
}

fetch() {
    url=$1
    out=$2
    expected=$3
    tmp="$out.tmp.$$"
    printf '  %s\n' "$(basename "$out")"
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 "$url" -o "$tmp"
    actual=$(hash_file "$tmp")
    if [ "$actual" != "$expected" ]; then
        rm -f "$tmp"
        echo "ERREUR : empreinte SHA-256 invalide pour $out" >&2
        echo "  attendu : $expected" >&2
        echo "  obtenu  : $actual" >&2
        exit 1
    fi
    mv "$tmp" "$out"
    chmod 644 "$out"
}

echo "Téléchargement des bibliothèques JavaScript…"
fetch "https://cdn.jsdelivr.net/npm/vega@5.33.1/build/vega.min.js" \
      "$DEST/vega.min.js" \
      "463f3db6a40b20e9747b4ed38f37ed0add508838f9141b1cf8366784b07b30c8"
fetch "https://cdn.jsdelivr.net/npm/vega-lite@5.23.0/build/vega-lite.min.js" \
      "$DEST/vega-lite.min.js" \
      "58c27358e26f2d319cf62f45bc17a4c8362f08645001df2ec8d341eee4097c7f"
fetch "https://cdn.jsdelivr.net/npm/vega-embed@6.29.0/build/vega-embed.min.js" \
      "$DEST/vega-embed.min.js" \
      "12d02acfbe3ec59ef9a37dd4822a2e04e2961b5bbb671bbe661d2221715b99da"
fetch "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js" \
      "$DEST/maplibre-gl.js" \
      "be9633c4d870e26fb37f1cfe5c5a77181667114003ea16207ac7850d8da8add1"
fetch "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css" \
      "$DEST/maplibre-gl.css" \
      "576b085fdd9487a65a19215328c1e086c07ce5bf6da09b666b3806d3d008dae9"
fetch "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js" \
      "$DEST/html2canvas.min.js" \
      "e87e550794322e574a1fda0c1549a3c70dae5a93d9113417a429016838eab8cb"

echo "Téléchargement des polices…"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.0.0/files/ibm-plex-sans-latin-400-normal.woff2" \
      "$DEST/fonts/ibm-plex-sans-400.woff2" \
      "db71f8a28ad8501544fb4e7668e3c6d0b731760b6f20de3525ebaeba597f1922"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-sans@5.0.0/files/ibm-plex-sans-latin-600-normal.woff2" \
      "$DEST/fonts/ibm-plex-sans-600.woff2" \
      "31535a91ce3f6b8ed3ddedadab1e49957e2220263a640df1a3f14f6fdfe15eb6"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-serif@5.0.0/files/ibm-plex-serif-latin-400-normal.woff2" \
      "$DEST/fonts/ibm-plex-serif-400.woff2" \
      "256774147c18fde1089393e4008316d583dd0fe5f5aacc9438b23640ce1c552a"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/ibm-plex-serif@5.0.0/files/ibm-plex-serif-latin-600-normal.woff2" \
      "$DEST/fonts/ibm-plex-serif-600.woff2" \
      "d09590fc991dd3ccddaca937365488b575f929f7a04977ce06620b83f2da0cb0"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.0.0/files/jetbrains-mono-latin-400-normal.woff2" \
      "$DEST/fonts/jetbrains-mono-400.woff2" \
      "7b53d2b16b4a98a46e88cc425ca753f2c93c61ead8f2954f877e81b6f668e6ec"
fetch "https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.0.0/files/jetbrains-mono-latin-600-normal.woff2" \
      "$DEST/fonts/jetbrains-mono-600.woff2" \
      "95fbf574b6078d2f8f0ef7769d2128911d2d4123baafacdbd8096c56c181feec"

echo "Assets tiers prêts dans app/static/."
