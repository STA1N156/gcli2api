#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
GO="${AGY_GO:-go}"
if ! "$GO" version | grep -q devel_298f53e483; then
  echo 'Set AGY_GO to Go revision 298f53e483; see native/README.md.' >&2
  exit 1
fi
if [ ! -f dist/index.html ]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
(cd native && GOTOOLCHAIN=local "$GO" build -trimpath -o bin/gcli2api .)
exec native/bin/gcli2api
