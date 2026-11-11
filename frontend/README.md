# AN control panel

Based on [su-kaka/agy2api](https://github.com/su-kaka/agy2api), revision
`f725755` (2026-11-06). Licensed under the repository's CNC-1.0 license.

The React/Vite panel uses this fork's existing filename-based API. It retains
quota summaries, runtime logs, and the current retry/session
settings without importing upstream backend changes.

```sh
npm install
npm run demo   # http://127.0.0.1:3000, in-memory example data only
npm run dev    # local Python service on port 7861
npm run build  # production assets in ../dist
```

The demo never connects to an online panel, imports real accounts, or sends
model requests. Production builds omit its data adapter. Docker builds the
frontend automatically; local Python runs require `npm run build` first.
