# Contributing

The backend lives in `native/`; the unchanged React panel lives in `frontend/`.
Use the Go compiler revision documented in `native/README.md` to preserve TLS
compatibility. Run `gofmt` on changed Go files and build the server before sending
a change. Use focused tests for the affected request, storage or panel path.

```sh
cd native
go test -run 'TestGoBackendMigration|TestCachedTransportSkipsCreationLock' .
go build -o bin/gcli2api .
```

Run `npm run build` in `frontend/` when changing the panel. Never commit real
credentials, databases, captured private requests or build outputs. Keep tests
against local mock endpoints. The repository's CNC-1.0 license remains in effect.
