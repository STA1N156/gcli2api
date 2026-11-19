# Go backend

The HTTP API, existing React panel API, SQLite storage, account selection, OAuth,
quotas, format conversion and Antigravity wire engine run in one Go process.
There are no Python workers, loopback RPCs or per-worker conversation caches.

The engine and templates originate from the supplied `claude-all-in-one` archive,
through CPA `c661eca7`. CPA's public translator SDK is pinned to that commit.
Ordinary generation keeps `requestType: chat`; image generation uses `image_gen`.
Both use the same account engine and transport. Antitruncation registers and
consumes `output_reply` in Go. Real client tools and signed multi-turn history
remain intact. No attachment materialization is added.

Each account has a separate HTTP pool and proxy. Pools allow 100 idle connections
per host and 512 in total. Different sessions execute concurrently; requests for
one conversation use that conversation's lock. Selection, configuration and
session affinity read shared memory. SQLite state is batched once a second;
explicit panel changes are flushed before returning. Existing database records
remain usable. Snapshots live in `creds/.wire` or `AGY_WIRE_STATE_DIR`.

Build with Go revision `298f53e483ef15096a05b1d3017cb9d4207cb327`, which defines
the reference native TLS ClientHello. Docker builds this compiler automatically.
Bootstrap this revision using Go 1.27 for local builds:

```sh
git clone https://github.com/golang/go.git /path/to/agy-go
git -C /path/to/agy-go checkout 298f53e483ef15096a05b1d3017cb9d4207cb327
cd /path/to/agy-go/src
GOROOT_BOOTSTRAP=/path/to/go1.27 ./make.bash
# On Windows, set GOROOT_BOOTSTRAP and run make.bat.
cd /path/to/gcli2api/native
GOTOOLCHAIN=local /path/to/agy-go/bin/go build -trimpath -o bin/gcli2api .
```

Use one process per SQLite database and wire state directory. `WORKERS`, `HTTPX_*`,
Python GC and jemalloc variables are obsolete. `HOST`, `PORT`, passwords, account
proxies and endpoint settings retain their meaning. This migration targets the
existing SQLite deployment; external databases are not used.
