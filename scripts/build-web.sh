#!/usr/bin/env bash
# Build the web app.
#
# The source lives on the (exFAT) Seagate drive, but exFAT has no symlinks and
# macOS litters it with ._ AppleDouble files, which breaks node_modules. So we
# mirror the source into ~/.ithaca/web-build on the internal disk, install and
# build there, and copy the finished dist/ back onto the drive.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ITHACA_HOME:-$HOME/.ithaca}/web-build"
mkdir -p "$BUILD"
rsync -a --delete --exclude node_modules --exclude dist --exclude '._*' --exclude .DS_Store "$ROOT/web/" "$BUILD/"
cd "$BUILD"
if [ ! -d node_modules ] || [ package.json -nt node_modules/.installed ] || [ package-lock.json -nt node_modules/.installed ]; then
  npm install --no-audit --no-fund
  touch node_modules/.installed
  # keep the lockfile in the repo in step with what was installed
  cmp -s package-lock.json "$ROOT/web/package-lock.json" || cp package-lock.json "$ROOT/web/package-lock.json"
fi
if [ "${1:-}" = "dev" ]; then
  exec npx vite --host 127.0.0.1
fi
npm run build
rm -rf "$ROOT/web/dist"
mkdir -p "$ROOT/web/dist"
rsync -a --exclude '._*' dist/ "$ROOT/web/dist/"
echo "Built web app → $ROOT/web/dist"
