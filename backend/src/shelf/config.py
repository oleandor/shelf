from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .frame_ancestors import parse_sources, validate_sources


class OIDCProvider(BaseModel):
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: list[str] = ["openid", "email", "profile"]
    # Which claim identifies the user. `sub` is correct for most
    # providers and is the default.
    #
    # Azure AD / Entra wants "oid": its `sub` is pairwise, a different
    # value per application registration, so the same person signing in
    # to two apps looks like two different subjects. `oid` is stable for
    # the user across the whole tenant.
    #
    # Changing this on a live instance changes what gets matched in
    # `identities`, so existing users arrive as a new (idp, subject) pair
    # and are re-linked by the email fallback in `upsert_user_from_claims`
    # — they keep their library as long as the address still matches.
    subject_claim: str = "sub"

    # `prompt` sent when linking a second account (/auth/link/{provider}).
    #
    # "select_account" is standard OIDC (Core 1.0 §3.1.2.1) and is what
    # makes a provider offer its account picker rather than silently
    # re-using the session it already has — without it, linking a second
    # account is unreachable on any provider that keeps you signed in.
    #
    # Not every provider implements it, though, and a spec-compliant one
    # that cannot returns `account_selection_required`. Set "login" there
    # (universally supported: forces re-authentication, so a different
    # account can be entered) or "" to send no prompt at all.
    link_prompt: str = "select_account"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHELF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://shelf:shelf@localhost:5432/shelf"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str = "http://localhost:3900"
    # Endpoint used only when signing URLs that are handed to a browser.
    # A presigned URL is bound to the host it was signed for, so it has to
    # name a host the browser can actually reach - which is not always the
    # address the server uses to talk to the same bucket (container network
    # aliases, private service names, split-horizon DNS). Empty means "same
    # as s3_endpoint", which is correct whenever both sides share a network.
    s3_endpoint_public: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "shelf"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    gotenberg_url: str = "http://localhost:3000"

    cors_origins: list[str] = ["http://localhost:5173"]

    # Pages allowed to embed shelf in a frame, as CSP source expressions:
    #   SHELF_FRAME_ANCESTORS="'self' https://viewer.example.com"
    # (a JSON list works too, as for SHELF_CORS_ORIGINS). Set, every HTML
    # response carries `Content-Security-Policy: frame-ancestors <list>`
    # and browsers refuse to render shelf inside any other page. Unset —
    # the default — sends no framing header, so any page may frame shelf,
    # as it always could. `'self'` is not implied. See frame_ancestors.py.
    frame_ancestors: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("frame_ancestors", mode="before")
    @classmethod
    def _parse_frame_ancestors(cls, raw: object) -> object:
        return parse_sources(raw)

    @field_validator("frame_ancestors")
    @classmethod
    def _check_frame_ancestors(cls, sources: list[str]) -> list[str]:
        return validate_sources(sources)

    session_secret_key: str = "dev-secret-do-not-use-in-production"
    session_ttl_seconds: int = 24 * 3600
    session_cookie_name: str = "shelf_session"
    session_cookie_secure: bool = False

    dev_login_enabled: bool = True

    # Role handed to accounts that sign in through /auth/dev-login.
    #
    # Defaults to "user", and deliberately so: dev_login_enabled is on by
    # default, and an instance that forgot to turn it off already mints a
    # session for any address presented. Making those sessions admin by
    # default would turn that footgun into an instant-admin one.
    #
    # `pixi run up` sets this to "admin" for the backend it launches, so a
    # fresh checkout can actually reach the Admin tab without hand-editing
    # env first. Only consulted when dev_login_enabled is true.
    dev_login_role: str = "user"

    # Emails promoted to the admin role on login. Bootstrap only: it
    # promotes and never demotes, so roles changed in the UI stick and
    # editing this list can't accidentally strip someone. A fresh
    # instance has no admin at all — set this once to get the first one,
    # then manage the rest in-app. `pixi run grant-admin <email>` does
    # the same thing without a restart.
    #   SHELF_ADMIN_EMAILS='["you@example.com"]'
    admin_emails: list[str] = Field(default_factory=list)

    # Upper bound on identities linked to one browser session (the
    # account switcher). Keeps the session cookie small and bounds how
    # much one stolen cookie is worth.
    max_linked_accounts: int = 8

    # Public origin for the API, used to construct OIDC redirect URIs.
    public_base_url: str = "http://localhost:8000"

    # Path to the built SPA. Empty / non-existent dir disables SPA hosting
    # (used in dev where vite serves the frontend on a separate port).
    frontend_dir: str = "/app/frontend"

    # JSON-encoded list of OIDC providers, e.g.
    #   SHELF_OIDC_PROVIDERS='[{"name":"authentik","issuer":"...",
    #     "client_id":"...","client_secret":"..."}]'
    oidc_providers: list[OIDCProvider] = Field(default_factory=list)

    # NATS JetStream URL used for the extraction job queue. Empty
    # disables publishing entirely (dev/tests stay clean) and the
    # worker is expected not to be running. The same env var feeds
    # both the API publisher and the worker subscriber so a stale
    # mismatch isn't possible.
    nats_url: str = ""

    # Startup dependency checks (see preflight.py). Enabled by default: a
    # deployment that cannot reach its database, bucket or OIDC provider is
    # broken, and failing at boot with the reason beats reporting healthy
    # and failing later in someone's browser. Turn off only where the app
    # is started without its dependencies on purpose.
    preflight_enabled: bool = True
    # Transient failures (connection refused, timeout, 5xx) are retried this
    # many times before giving up, so a dependency that is slow to come up
    # does not turn into a crash loop. Configuration faults never retry.
    preflight_retries: int = 5
    preflight_retry_delay_seconds: float = 2.0
    preflight_timeout_seconds: float = 10.0

    # Container image tag, e.g. "sha-143a581". Set by the Dockerfile
    # at build time from DOCKER_IMAGE_TAG (CI sets it to the short SHA).
    # "dev" means a local pixi run rather than a baked image. Surfaced
    # via /api and written into attachment_processing.ocr_engine /
    # .outline_engine so the per-row UI can show which build handled
    # a given run — useful when an OCR job fails and you want to know
    # if the running pod matches the version where the bug was fixed.
    image_tag: str = "dev"


settings = Settings()
