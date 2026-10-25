#!/usr/bin/env bash
# Baut ein installierbares Rad aus DIESEM Quellbaum, ohne dessen version-Feld
# anzufassen: kopiert in ein temporaeres Verzeichnis, setzt dort die Version,
# baut und legt sha256 + Git-Knoten neben das Rad.
#
# Aufruf:  deploy/build-cubolt-wheel.sh 0.4.0+herz.3 [/pfad/zur/ablage]
# Ausgabe: Rad-Pfad, sha256, Git-Knoten (auf stdout, letzte Zeile = Rad-Pfad)
#
# Das Bauwerkzeug (hatchling) wird bei Bedarf in ein Wegwerf-Verzeichnis
# installiert; die Systemumgebung bleibt unveraendert.
set -euo pipefail

VERSION="${1:?Aufruf: build-cubolt-wheel.sh <version> [ablage]}"
OUT="${2:-/home/cubolt/.hermes-mcp-gate/dist}"
PY="${PYTHON:-/usr/bin/python3.12}"

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/hermes-mcp-wheel-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

echo "quelle:   $SRC"
echo "version:  $VERSION"
echo "ablage:   $OUT"

mkdir -p "$OUT" "$WORK/tree"
cp -a "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/LICENSE" "$WORK/tree/"
cp -a "$SRC/src" "$WORK/tree/src"

# Version NUR im Kopie-Baum setzen (Projekt-Version, erste passende Zeile).
"$PY" - "$WORK/tree/pyproject.toml" "$VERSION" <<'PY'
import re
import sys

path, version = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
new, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', text, count=1)
if n != 1:
    raise SystemExit("pyproject.toml: version-Zeile nicht gefunden")
open(path, "w", encoding="utf-8").write(new)
print("pyproject-version gesetzt:", version)
PY

# Bauwerkzeug nur bei Bedarf, in ein Wegwerf-Ziel.
if [ ! -d "$WORK/tools/hatchling" ]; then
  echo "baue werkzeug nach: $WORK/tools"
  "$PY" -m pip install --quiet --target "$WORK/tools" hatchling
fi

PYTHONPATH="$WORK/tools" "$PY" -m pip wheel --no-deps --no-build-isolation \
  --quiet -w "$WORK/dist" "$WORK/tree"

WHEEL="$(ls "$WORK"/dist/*.whl | head -1)"
BASENAME="$(basename "$WHEEL")"
cp "$WHEEL" "$OUT/$BASENAME"

SHA="$(sha256sum "$OUT/$BASENAME" | cut -d' ' -f1)"
GITKNOTEN="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unbekannt)"
SCHMUTZ="$(git -C "$SRC" status --porcelain 2>/dev/null | head -5 || true)"

printf '%s  %s\n' "$SHA" "$BASENAME" > "$OUT/$BASENAME.sha256"
{
  echo "rad:        $BASENAME"
  echo "version:    $VERSION"
  echo "sha256:     $SHA"
  echo "git_knoten: $GITKNOTEN"
  echo "quelle:     $SRC"
  echo "gebaut_am:  $(date -Is)"
  if [ -n "$SCHMUTZ" ]; then
    echo "warnung:    Quellbaum hatte unversionierte Aenderungen:"
    printf '%s\n' "$SCHMUTZ"
  else
    echo "quellbaum:  sauber"
  fi
} > "$OUT/$BASENAME.info.txt"

cat "$OUT/$BASENAME.info.txt"
echo "rad_pfad: $OUT/$BASENAME"
