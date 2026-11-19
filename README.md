# GCLI2API — Go backend

Antigravity API gateway with the existing React control panel. The complete
runtime is Go: model requests, OpenAI/Claude/Gemini conversion, account selection,
OAuth, quotas, SQLite, session caches and panel endpoints.

## Run

```sh
docker build -t gcli2api .
docker run --rm -p 7861:7861 -e PASSWORD=change-me -v ./creds:/app/creds gcli2api
```

Open the service URL to use the panel. Existing `creds/credentials.db` data is
loaded directly. Back up this directory before replacing an existing service.

For local builds, set `AGY_GO` to the pinned Go compiler described in
[native/README.md](native/README.md), then run `start.sh` or `start.ps1`.
The frontend requires Node.js only when building its assets.

## Endpoints

- OpenAI: `/antigravity/v1/chat/completions`
- Claude: `/antigravity/v1/messages`
- Gemini: `/antigravity/v1beta/models/{model}:generateContent` and `:streamGenerateContent`
- Model list: `/antigravity/v1/models` or `/antigravity/v1beta/models`

Prefix a model with `抗截断/` to return the reply through the hidden `output_reply`
tool. Client-owned tool choices and parallel real tool calls remain available.

## Runtime

One Go process shares account selection and conversation caches. Every account
has its own proxy and connection pool, with up to 100 idle connections per host.
SQLite is read at startup; runtime counters and cooldown changes are batched.
The native wire engine retains the reference request structure, system templates,
signed history and TLS implementation. Ordinary generation uses `chat`.

Existing password, endpoint and retry settings remain in the panel. `WORKERS`,
`HTTPX_*`, Python GC and jemalloc variables are no longer used. This version uses
SQLite; it does not connect to external PostgreSQL, MongoDB or Redis databases.

See [.env.example](.env.example) for environment settings. Local start scripts
do not pull, reset, push or deploy repository changes.
