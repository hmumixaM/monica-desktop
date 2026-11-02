# Shared contracts

The checked-in TypeScript types mirror the public FastAPI request/response
shapes used by the web app and import adapters. The source of truth remains
the FastAPI OpenAPI document at `/openapi.json`.

To refresh generated clients after the API is running:

```sh
curl -fsS http://localhost:8000/openapi.json > packages/contracts/openapi.json
```

The repository intentionally keeps raw provider payloads behind `unknown` so
that provider schema additions do not force a database migration or a web
release.
