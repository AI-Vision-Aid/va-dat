# Deploying to Coolify

The app is a single long-running container serving both the website
(`index.html`, `styles.css`) and the audit API from
`entry_points/api_server.py`.

## Quick start

```bash
docker compose up --build                    # http://localhost:8000
HOST_PORT=8789 docker compose up --build     # if 8000 is already in use
```

In Coolify: create a **Docker Compose** resource pointed at this repo, set the
environment variables below, attach a domain, and deploy.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `HOST` | No | Set to `0.0.0.0` by the image; do not override |
| `PORT` | No | Defaults to 8000; Coolify may inject its own |
| `HOST_PORT` | No | Local-only: host port for `docker compose up` (default 8000) |

**No provider API keys are configured, deliberately.** Each user pastes their
own key into the web form, so the deployment never bills a shared account.
Per-request keys take priority over the environment
(`api_server.py:_resolve_api_key`).

Consequence worth knowing: **a request with no resolvable key silently runs a
dry run** — programmatic checks only, no LLM findings, no CSV report, and a
`200 OK` either way. The only signal is `summary.dry_run` in the response. A
user who forgets to paste a key gets a partial-looking audit rather than an
error.

### The `.env` interpolation trap

Do **not** add lines like this back into `docker-compose.yml`:

```yaml
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}     # DON'T
```

Compose automatically loads `./.env` from the project directory to resolve
`${...}`. On any developer machine with a local `.env`, that line silently
injects their personal key into the container, and every anonymous audit bills
it. This was verified the hard way: with that line present, a container built
from an image containing no `.env`, started from a shell exporting no key,
still made live API calls.

`.dockerignore` keeps `.env` out of the *image*; interpolation reads it from
the *host* at run time. Different mechanism — `.dockerignore` cannot stop it.
Coolify deployments are not affected (`.env` is gitignored, so it never reaches
the clone), but local `docker compose up` is.

## Proxy configuration — read this one

`POST /api/audit`, `/api/audit/url`, and `/api/audit/url/nested` return an
**NDJSON stream** that stays open for the whole audit, emitting progress events
as it goes. Two default proxy behaviours will break this:

1. **Response buffering.** A proxy that buffers will hold every progress event
   until the audit finishes, so the browser shows a frozen progress bar and then
   all results at once. Disable buffering for `/api/`.
2. **Read timeouts.** A single-page audit runs 17+ sequential LLM calls and
   commonly takes 1–3 minutes; a nested crawl audits every discovered page and
   can run far longer. Anything under ~10 minutes risks cutting audits off
   mid-run. Traefik's default is generous, but Coolify installs vary — verify.

In Coolify, set these as Traefik labels on the service, or raise the equivalent
values in the proxy settings:

```yaml
labels:
  - traefik.http.routers.audit.middlewares=audit-buffering@docker
  - traefik.http.middlewares.audit-buffering.buffering.maxResponseBodyBytes=0
  - traefik.http.services.audit.loadbalancer.responseForwarding.flushInterval=100ms
```

`flushInterval` is the important one — it forces Traefik to flush each chunk
through rather than accumulating it.

If you front this with nginx instead:

```nginx
location /api/ {
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

## Sizing

Audits are I/O-bound on the LLM API, not CPU-bound. 512 MB RAM and 0.5 vCPU is
enough for light use. The server is a stdlib `ThreadingHTTPServer`, so each
concurrent audit holds a thread and its own temp directory — fine for a handful
of simultaneous users, not for public high traffic. The `tmpfs` mount is capped
at 512 MB; a large nested crawl of very large pages could approach that, so
raise it if you see failures writing temp files.

## Serving

`entry_points/api_server.py` is the single server: it serves `index.html` and
`styles.css` at `/`, and handles the audit endpoints under `/api/`. There is
deliberately no second, serverless copy of the audit logic — the project
previously carried two, and they drifted until one silently skipped the LLM
deduplication pass. Keep the logic in one place.
