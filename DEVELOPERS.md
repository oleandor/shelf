# Developing and running shelf

Everything in here is for people working on shelf or running an instance of
it. Using one — the web interface and the `shelf` CLI — is covered in the
[README](./README.md).

## Quick start (local dev)

Requires [pixi](https://pixi.sh) and Docker.

```bash
pixi install
pixi run up                  # everything, in one terminal
```

`up` starts Postgres + Redis + Gotenberg + an object store and waits on their healthchecks, applies migrations, installs the frontend's `node_modules` if they're missing, then runs the backend (`:8000`) and the vite dev server (`:5173`) side by side with prefixed output. Ctrl-C stops both servers; the containers stay up so the next `up` is quick. `pixi run dev-down` stops those when you're done.

Open http://localhost:5173. `SHELF_DEV_LOGIN_ENABLED` defaults to true, so the login page offers a dev-login form — enter any address and you're in, no identity provider required. It's the same thing as `POST /auth/dev-login`, and `/auth/providers` reports whether it's available so the SPA knows to show it. Turn it off anywhere others can reach it: it mints a session for any email presented.

### Dependencies

`pixi.toml` and `pixi.lock` decide every package version — locally, in CI, and
in the container images, which install the locked `prod` (or `gpu`) environment
rather than resolving anything of their own. Packages come from conda-forge
unless they aren't published there; each `[pypi-dependencies]` entry in
`pixi.toml` says which case it is. Neither `pyproject.toml` lists runtime
dependencies: they hold build metadata only, and a `pip install .` of this
repo deliberately installs no dependencies.

To change a dependency: edit `pixi.toml`, run `pixi lock`, commit the lockfile
with it. CI installs with `--locked` and fails on a stale lock, which is the
point — a dependency release cannot reach production without a diff someone
reviewed. It once did: SQLAlchemy 2.1.0 moved greenlet behind an extra, the
images resolved it from PyPI at build time, and every pod died on startup with
CI green on the lockfile's 2.0.49 throughout.

### Object store

Two S3 implementations are wired up as compose profiles and are interchangeable — shelf talks to both through the same presigned-URL path:

```bash
pixi run up --s3 garage      # default — https://garagehq.deuxfleurs.fr
pixi run up --s3 rustfs      # https://github.com/rustfs/rustfs
pixi run up --s3 none        # skip it; metadata works, uploads don't
```

Both publish the S3 API on `:3900` with the same bucket, region and credentials, so `SHELF_S3_*` doesn't change when you switch and the running one is the only difference. `up` stops the other before starting the one you asked for, since they'd otherwise collide on the port. It also puts a permissive CORS rule on the bucket: uploads go from the browser straight to a presigned URL, which is cross-origin, and Garage refuses the preflight until told otherwise. Garage creates its bucket and access key on first boot from `GARAGE_DEFAULT_*`; RustFS starts empty, so `up` creates the bucket itself. RustFS also serves a web console on `:9001`.

The dev credentials are committed in `compose.yaml` on purpose so a fresh checkout needs no setup. They're throwaways — anything exported in your shell wins, so point `SHELF_S3_*` at your own store and `up` leaves it alone.

### Ports

Every port the stack publishes is a common one — `:5173` for vite, `:8000` for the backend, `:5432`, `:6379`, `:3000`, `:3900` for the containers — so any of them may already be taken by another checkout, another project's compose stack, or a service you run anyway. `up` probes each one before it starts anything and moves to the next free number, printing where it landed:

```
    5173 is in use — running the frontend on 5174
    3900 is in use — publishing the S3 API on 3901
==> Open http://localhost:5174 — ...
```

Nothing else needs adjusting when one moves. The SPA reaches the API through vite's proxy, so its calls are same-origin regardless of port, and OIDC redirect URIs come from the backend's own `SHELF_PUBLIC_BASE_URL`. A moved container port is passed to compose and written to `./.env` — which compose reads on its own, so `dev-down`, `dev-logs` and `test-db-up` address the same containers — and the matching `SHELF_DATABASE_URL` / `SHELF_REDIS_URL` / `SHELF_GOTENBERG_URL` / `SHELF_S3_ENDPOINT` is set for the backend and the migrations. Ports one of your own containers already publishes are kept rather than re-picked, so re-running `up` doesn't recreate a working container.

Two things are outside that: `--web-port` / `--api-port` pin the two servers, in which case the port is used as given rather than scanned for; and `pixi run test` reads `backend/.env` directly, so a bumped Postgres needs `SHELF_DATABASE_URL` set there before the suite will connect — `up` says so when it happens.

### One piece at a time

The individual tasks are still there if you'd rather drive one piece at a time:

```bash
pixi run dev-up              # Postgres + Redis + Gotenberg via compose.yaml
pixi run alembic-up          # apply migrations
pixi run dev-api             # backend on http://localhost:8000

# In another terminal, for the SPA:
pixi run frontend-install
pixi run frontend-dev        # http://localhost:5173 (proxies /api and /auth to the backend)
```

### An admin account locally

`pixi run up` sets `SHELF_DEV_LOGIN_ROLE=admin` for the backend it launches, so
accounts made through the dev-login form are admins and the Admin tab is there
to look at. The setting defaults to `user` everywhere else, and deliberately —
dev login mints a session for any address presented, so defaulting it to
`admin` would turn "forgot to switch dev login off" into "anyone who can reach
this is an admin". Set it explicitly in `backend/.env` or the shell to override
what `up` picks.

Like `SHELF_ADMIN_EMAILS` it only ever grants. Env can hand out a role; only the
admin UI or `pixi run grant-admin --revoke` takes one back. A knob that demoted
would quietly strip, at the next sign-in, a role you'd set on purpose.

## Running the tests

The suite needs a live Postgres — the fixtures truncate real tables between tests rather than mocking the database. `test-all` handles that for you:

```bash
pixi run test-all       # starts Postgres, waits for it, migrates, runs pytest
```

It only starts the `postgres` service, waits on its healthcheck rather than sleeping, and is a no-op if the container is already up — so it behaves the same on Windows and Linux, and re-running it in a loop stays quick. `pixi run test` is the bare pytest if you already have a database up and migrated.

`pixi run lint` and `pixi run typecheck` need no services. `pixi run dev-down` stops the stack when you're done.

### Frontend

The SPA has its own suite — vitest and Testing Library on jsdom — which needs
no services at all: components under test talk to a stubbed `fetch`, never a
real API.

```bash
pixi run frontend-test            # once
pixi run frontend-test-watch      # on change
pixi run frontend-test-coverage   # with a v8 coverage report
```

Helpers live in `frontend/src/test/utils.tsx`: `renderWithProviders` wraps a
component in a router and a throwaway QueryClient, `mockFetch` stubs responses
by path prefix, and `makeMe` / `makeMeWithTwoAccounts` build the `/api/me`
shapes. An unstubbed request throws rather than hanging, so a test that reaches
for the network says so.

Full-page navigations go through `frontend/src/lib/navigation.ts` rather than
calling `window.location.assign` inline — partly to keep the "reload, don't
router-navigate" decision documented in one place, partly because jsdom won't
let a test intercept it otherwise.

### CLI

```bash
pixi run cli-test
```

The tests fake the client, so they need no server and no database.

### README screenshots

The screenshots in the README's web-interface section are generated, not
taken by hand. Each one is a *user story* in `scripts/ui_stories.py` — a
function that opens one screen in the state a user would see it — driven by
Playwright against the local dev stack:

```bash
pixi run up                          # in one terminal
pixi run ui-stories                  # in another: every story, into docs/screenshots/
pixi run ui-stories --list
pixi run ui-stories --story reader   # just one; repeatable
pixi run ui-stories --theme light    # <story>-light.png (the app defaults to dark)
```

Before the stories run, the script seeds a demo library through the SPA's own
session endpoints: an admin demo user, a shared Standards space with a viewer
and two editions of one standard, two papers with PDFs, collections, tags, a
highlight and a note. The PDFs are drawn with reportlab at seed time. Every
step looks before it creates, so re-running adds nothing. Seeding waits for the
worker to extract the PDFs' text, which search needs.

It runs in its own pixi environment, `ui` (Python, Playwright, reportlab,
and the CLI for its stories — none of the server), because Playwright's Node driver can't share a solve with
the OCR stack on linux-64. The Chromium it drives is downloaded into
`.pixi/ms-playwright` by `ui-stories-install`, which `ui-stories` depends on
and pixi caches: the first run on a machine downloads it, later runs skip it.

To add a story, write a function decorated with `@story("name", "summary")`
that navigates and waits for the screen to settle, then embed
`docs/screenshots/name.png` where the README describes it. A story that
returns a clip rect (`{"x", "y", "width", "height"}`) is cropped to it.

The CLI has the same, in `scripts/cli_stories.py`:

```bash
pixi run cli-stories                 # every CLI story, into docs/screenshots/cli-*.svg
pixi run cli-stories --story browse
pixi run stories                     # both sets: every figure in the README
```

It seeds the same demo library, mints an API token for the demo user (and
revokes it afterwards), then runs real `shelf` commands and records each one's
output as a terminal-window SVG with rich. `browse` is the terminal UI itself,
driven headless through textual's test pilot and saved with textual's own
screenshot support. A one-command story is a `command_story(...)` call;
anything that needs setup first — like writing the profiles `profiles-push`
pushes — is a `@story` function.

### CI

CI runs these on every pull request, each as its own status check: lint,
typecheck, and the backend suite; the frontend's typecheck and unit tests; and
a docs check that the `shelf-cli` install commands point at the version the PR
will release (see [Releases](#releases)).

## Configuration

Settings are environment variables under the `SHELF_` prefix, read via pydantic-settings; `backend/.env.example` lists the common ones. The defaults target local development and are not safe to deploy as-is.

```
SHELF_DATABASE_URL=postgresql+asyncpg://shelf:shelf@localhost:5432/shelf
SHELF_REDIS_URL=redis://localhost:6379/0

# Any S3-compatible store. Uploads and downloads are presigned, so file
# bytes go browser <-> store without passing through the API.
SHELF_S3_ENDPOINT=http://localhost:3900
# Set this when the browser cannot reach SHELF_S3_ENDPOINT - the API and the
# browser are on different networks, so the address the server uses to reach
# the bucket is not one a browser can resolve or load. Presigned URLs are
# signed for this host instead; server-side calls keep using SHELF_S3_ENDPOINT.
# Leave unset when both sides share a network, as they do in dev.
SHELF_S3_ENDPOINT_PUBLIC=
SHELF_S3_REGION=us-east-1
SHELF_S3_BUCKET=shelf
SHELF_S3_ACCESS_KEY_ID=
SHELF_S3_SECRET_ACCESS_KEY=

SHELF_GOTENBERG_URL=http://localhost:3000
SHELF_CORS_ORIGINS=["http://localhost:5173"]

# Pages allowed to embed shelf in an iframe, as CSP source expressions. Set,
# HTML responses carry `Content-Security-Policy: frame-ancestors <list>`.
# Unset (the default) sends no framing header. See "Embedding" below.
SHELF_FRAME_ANCESTORS="'self' https://viewer.example.com"

SHELF_SESSION_SECRET_KEY=<openssl rand -hex 32>
SHELF_SESSION_COOKIE_SECURE=true
SHELF_DEV_LOGIN_ENABLED=false
SHELF_PUBLIC_BASE_URL=https://shelf.example.com

# Emails promoted to admin on login. See "Admins" below.
SHELF_ADMIN_EMAILS=["you@example.com"]

# The API checks its dependencies once at startup and refuses to serve if
# one is misconfigured - see "Startup checks" below. Set to false only to
# start the app without them on purpose.
SHELF_PREFLIGHT_ENABLED=true

# NATS JetStream, for the background workers. Empty disables job publishing;
# the API still records the queued state on the row, so nothing is lost when
# no worker is running — the PDF just never gets its text until something
# re-queues it. `pixi run up` sets this for you.
SHELF_NATS_URL=nats://nats:4222
```

### Startup checks

The API verifies its dependencies once at startup and refuses to serve if
one is misconfigured, so a broken deployment fails at boot with the reason
in its logs rather than reporting itself healthy and failing later in a
browser. It checks that the database is reachable and migrated, that the
bucket exists and the credentials can address it, that the bucket answers
a CORS preflight for `SHELF_PUBLIC_BASE_URL` (uploads go browser-to-store,
so a missing rule breaks them and nothing reaches the server), that the
browser-facing store endpoint is not plaintext when the app is served over
HTTPS, and that every OIDC issuer resolves a discovery document.

Failures that look transient - connection refused, timeout, 5xx - are
retried before giving up, so a dependency that is slow to start does not
become a crash loop. Configuration faults are not retried.

`/readyz` re-runs the cheap subset per request and is the readiness probe;
`/health` stays a plain liveness check that never touches a dependency, so
an outage takes an instance out of rotation rather than restarting it.

Set `SHELF_PREFLIGHT_ENABLED=false` to start without them.

### Embedding

Shelf sends no framing header unless told to, so by default any page can
show it in an iframe. `SHELF_FRAME_ANCESTORS` makes that a decision: set, every
HTML response carries `Content-Security-Policy: frame-ancestors <list>`, and a
browser refuses to render shelf inside any page the list doesn't name.

```
# Shelf itself and one viewer may frame it; nothing else may.
SHELF_FRAME_ANCESTORS="'self' https://viewer.example.com"
# Nobody may.
SHELF_FRAME_ANCESTORS="'none'"
```

Entries are CSP source expressions — `'self'`, `'none'`, a scheme like
`https:`, or a host source such as `https://*.example.com:8443` — space
separated, or as a JSON list like `SHELF_CORS_ORIGINS`. `'self'` is not added
for you. `*` means any site, which is the same as leaving the setting unset.
Anything that isn't a single source expression (a bare `self`, a `;`
or `,`) stops the app at startup rather than shipping a policy that means
something else. Only HTML gets the header: it governs the document being
framed, and shelf's only document is the SPA's `index.html`.

A frame is cross-site whenever the embedding page's registrable domain
differs from shelf's, and then the browser withholds shelf's `SameSite=Lax`
session cookie: the frame shows the sign-in page, and signing in inside a
frame doesn't work with providers that forbid being framed themselves. The
setting decides who may frame shelf, not whether a session reaches the frame.
In the Helm chart the same list is `frameAncestors`.

## OIDC / SSO

Providers are a JSON list and none is special-cased — Authentik, Keycloak, Entra ID, Google, or anything else OIDC-compliant. Endpoints come from `{issuer}/.well-known/openid-configuration`.

```
SHELF_OIDC_PROVIDERS='[{"name":"authentik","issuer":"https://authentik.example.com/application/o/shelf/","client_id":"...","client_secret":"..."}]'
```

Register the redirect URI as `{SHELF_PUBLIC_BASE_URL}/auth/callback/{name}`, where `name` is the provider's label in the JSON above — so the example needs `https://shelf.example.com/auth/callback/authentik`. On first login shelf creates the user, the identity record, and a personal space; later logins match on `(idp, subject)`, so a user keeps their library if their email changes.

### Per-provider tuning

Two optional fields exist for providers that deviate from the common case. Both
default to what a standards-compliant provider expects, so Authentik, Keycloak,
Google and most others need neither.

| Field | Default | Set it when |
|---|---|---|
| `subject_claim` | `sub` | the provider's `sub` isn't stable for a user across applications |
| `link_prompt` | `select_account` | the provider doesn't implement that `prompt` value |

**`subject_claim`** picks the claim shelf keys identities on. Azure AD / Entra
needs `"oid"`: its `sub` is pairwise — a different value per application
registration — so the same person looks like a different subject to every app,
while `oid` is stable across the tenant.

Changing this on a running instance changes what gets matched in `identities`,
so existing users arrive as a new `(idp, subject)` pair. They're re-linked by
email on next login and keep their library, as long as the address still
matches.

**`link_prompt`** is the `prompt` sent when adding a second account. The default
`select_account` is standard OIDC and makes the provider show its account
picker; without it, a provider that keeps you signed in silently returns the
same account and linking a second one is impossible. A provider that doesn't
implement it returns `account_selection_required` — shelf reports that with the
fix in the message. Set `"login"` there instead (universally supported; forces
re-authentication so a different account can be entered), or `""` to send no
prompt.

```
# A provider on the defaults needs nothing extra:
SHELF_OIDC_PROVIDERS='[{"name":"authentik","issuer":"https://authentik.example.com/application/o/shelf/","client_id":"...","client_secret":"..."}]'

# Entra wants both:
SHELF_OIDC_PROVIDERS='[{"name":"entra","issuer":"https://login.microsoftonline.com/<tenant>/v2.0","client_id":"...","client_secret":"...","subject_claim":"oid"}]'
```

Several providers can be configured at once; the login page lists each, and the
account switcher sends "switch user" to whichever one the active account signed
in with.

Behind a reverse proxy, uvicorn needs `--proxy-headers --forwarded-allow-ips='*'` so redirect URIs are built as `https://…` and match what the IdP has registered. The Dockerfile already does this.

## Admins

A fresh instance has no admin. Name yourself in `SHELF_ADMIN_EMAILS` and log in;
the role is granted on login and then lives in the database, so removing the
address later doesn't take it away. `pixi run grant-admin <email>` does the same
to an existing user without a restart, and `--revoke` reverses it. The last
remaining admin can't be demoted through the UI, so an instance can't lock
itself out by accident.

Roles are read from the database on every request, so a change takes effect on
the next one rather than whenever the session happens to expire.

Attachments uploaded from now on are stored under `spaces/{space_id}/…`, so a
bucket policy or lifecycle rule can address one space's objects without going
through the database. Existing objects keep their original keys and are not
rewritten; keys are stored per row, so both layouts coexist.

## Background workers

Jobs go to NATS JetStream and are consumed by `python -m shelf.worker`, running from the same image as the API. Job state also lives on the database row, so a worker being down delays work rather than losing it.

`pixi run up` starts NATS and a worker alongside the API and the SPA, so a local
stack processes uploads end-to-end: upload a PDF (or a Word file, a deck, a
scan), and its text is extracted and searchable a few seconds later. Which consumers run depends on what the machine
has —

| Consumer | Does | Needs |
|---|---|---|
| `extract` | body text + per-page text, quality assessment | pypdf (always available) |
| `outline` | heading detection, generated table of contents | PyMuPDF (always available) |
| `ocr` | Tesseract pass over scanned PDFs | `tesseract` + `gs` on PATH |
| `convert` | renders non-PDF uploads to PDF, then hands them to `extract` | Gotenberg (`SHELF_GOTENBERG_URL`) for office files; images need nothing |

`convert` takes Word (`.docx`, `.doc`, `.odt`, `.rtf`), PowerPoint (`.pptx`,
`.ppt`, `.ppsx`, `.pps`, `.odp`), spreadsheets (`.xlsx`, `.xls`, `.ods`) and
images (`.png`, `.jpg`, `.tiff`, `.bmp`, `.gif`), decided by extension —
`shelf.services.conversion` is the list. Office files go to Gotenberg's
LibreOffice route (the compose stack runs one, as does the Helm chart unless
`gotenberg.enabled=false`); images are wrapped by PyMuPDF in the worker, come out
without text, and so go on to OCR. The upload itself is never changed: the PDF
is stored as a `convert` derivation beside it, which the reader opens, search
indexes, and OCR and outline start from, while **Download** still returns the
`.docx`. A file that won't convert keeps the reason on
`attachment_processing.convert_error`, shown on the attachment; rescanning it
from the admin extraction page tries again. `pixi run backfill-extract` also
queues every convertible upload that has never been converted — run it once
after the first deploy with this worker, since earlier uploads were skipped.

`ocr` is skipped with a note when those binaries are missing, which is the
normal case on Windows and macOS — pixi only installs them on linux-64. Override
with `pixi run up --consumers extract,ocr,outline,convert`, or run without a worker at
all via `pixi run up --no-worker`.

Anything uploaded while no queue was running has a row but no message, so
nothing ever told the worker about it. `pixi run up` re-publishes those on every
start (idempotent — WorkQueue retention drops the duplicate once acked), and
`pixi run backfill-extract` does the same by hand. A PDF stuck at
`extraction_status = 'pending'` with no worker running is exactly this case.

- **CPU tier** — text extraction, PDF quality scoring, Tesseract OCR, outline generation (a PyMuPDF font heuristic). Runs anywhere.
- **GPU tier** — olmOCR for documents the CPU tier scores as poor. Built separately from `Dockerfile.gpu` (`pixi run gpu-image-build`) because it pulls a multi-GB Torch/CUDA stack.

A GPU host outside the cluster should use `SHELF_WORKER_BACKEND=api`, which routes database writes through `/api/v1/worker/*` with a scoped token (`pixi run mint-worker-token <label>`) so that host never holds database credentials. Use `sql` only for in-cluster pods on a trusted network.

## The REST API

`/api/v1/*` is what API tokens (minted in Settings → API tokens) and the
`shelf` CLI talk to. It covers a whole import without touching the SPA:

| | |
|---|---|
| `POST /api/v1/items` | create an item with its metadata |
| `PATCH /api/v1/items/{id}` | set type and metadata fields |
| `GET /api/v1/items/{id}` | read one back |
| `PUT /api/v1/items/{id}/revision` | file it as one edition of a standard |
| `POST /api/v1/upload`, `/uploads/register`, `/uploads/{id}/complete` | attach files |
| `GET /api/v1/search`, `/collections` | find things |

`data` is the same opaque JSON the SPA writes, so whatever that item type's
form would capture goes straight in:

```bash
curl -X POST https://shelf.example.com/api/v1/items \
  -H "Authorization: Bearer $SHELF_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"item_type": "standard", "space_slug": "standards", "data": {
        "title": "Guidance on the design of widgets — Part 2: Plated widgets",
        "standardBody": "NX Standards",
        "designation": "NX-ACME 1234",
        "edition": "2015+A2:2020+NA:2020",
        "nationalAnnex": "NA:2020 (Ruritania)",
        "amendments": "AC:2017, A1:2018, A2:2020",
        "issuedOn": "2020-10-01"
      }}'
```

`PATCH` replaces `data` wholesale, matching the SPA's own PATCH. Pass
`"merge": true` to set individual keys and leave the rest standing — the mode a
script enriching existing records wants — where a key set to `null` is removed.

Filing a revision uses the same family lookup the SPA does, matched
case-insensitively on issuing body + designation, so editions loaded by an
importer and editions filed by hand land in one revision history.

## Deployment

The application image is published to the GitHub Container Registry on each
release tag, as `ghcr.io/krande/shelf:<version>` and `:latest`. It runs the API
and SPA by default, and the CPU worker with `python -m shelf.worker` (which is
how the Helm chart's worker Deployment uses it). It won't start usefully alone:
point it at PostgreSQL (`SHELF_DATABASE_URL`) and an S3-compatible store, and
at NATS (`SHELF_NATS_URL`) for background processing. The GPU worker is a
separate image built from `Dockerfile.gpu` and isn't published by CI.

The image bundles the built SPA and serves it from the same origin as the API, and installs the locked pixi environment, so what CI tested is what runs. A Helm chart is in [`deploy/helm/shelf/`](./deploy/helm/shelf/), with [`deploy/examples/values-example.yaml`](./deploy/examples/values-example.yaml) as a starting point. The chart expects a Kubernetes Secret holding at least `SHELF_DATABASE_URL` and `SHELF_SESSION_SECRET_KEY`.

## Releases

PRs use conventional commit titles and a `release-*` label;
[deputy](https://github.com/Krande/deputy) checks both, and merging a labelled
PR cuts the tag that publishes the image. Configuration is in
[`deputy.toml`](./deputy.toml).

The backend, the CLI and the frontend share one version number, bumped by the
release. The `pixi global install shelf-cli … --tag vX.Y.Z` commands in the
README and `cli/README.md` have to name a tag that exists and matches the API a
user is about to talk to, so they're pinned rather than pointed at `main` — and
a pin goes stale the moment the next release lands. Two things keep it current:

- The release rewrites them. `deputy.toml` runs
  `scripts/check_cli_version.py --write` as semantic-release's `build_command`,
  after the version bump and before the release commit, so the commit the new
  tag points at already carries the new tag in its install commands.
- `pixi run check-cli-version` compares every install command against the
  version in `cli/pyproject.toml`, and fails naming the file and line when one
  disagrees. The Docs workflow runs it on every PR, so a hand edit, or a new
  install snippet the rewrite can't find, is caught before merge rather than
  after the tag. A new file with an install command goes in both `DOCS` in the
  script and `assets` in `deputy.toml`.
