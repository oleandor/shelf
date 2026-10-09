"""Who may embed shelf in a frame — `SHELF_FRAME_ANCESTORS`.

Unset, shelf sends no framing header at all and any page can put it in an
iframe, which is how it has always behaved. Set, every HTML response
carries `Content-Security-Policy: frame-ancestors <sources>`, so the
browser refuses to render shelf inside any page not on the list.

Only HTML responses get it. `frame-ancestors` governs the document being
framed, and the only documents shelf serves are the SPA's index.html;
JSON, PDFs and assets are fetched, never framed, so the header would do
nothing there.

The sources are CSP source expressions, written as CSP writes them —
`'self'`, `'none'`, `https:`, `https://viewer.example.com`,
`https://*.example.com:8443`. `'self'` is not implied: a list without it
forbids shelf from framing itself.
"""

import json
import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_KEYWORDS = {"'self'", "'none'"}
# scheme-source (`https:`) or host-source (`[scheme://]host[:port][/path]`,
# host optionally `*` or `*.`-prefixed), per CSP Level 3 §2.3.1. Strict on
# purpose: the value lands in a response header, so anything that could
# end the directive (`;`), start another source list (`,`), or break the
# header (whitespace, quotes, control characters) is refused.
_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]*"
_SOURCE = re.compile(
    rf"(?:{_SCHEME}:"
    rf"|(?:{_SCHEME}://)?(?:\*|(?:\*\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)"
    r"(?::(?:[0-9]+|\*))?(?:/[A-Za-z0-9._~%!$&()*+=:@/-]*)?)"
)


def parse_sources(raw: object) -> object:
    """Accept the setting as a JSON list, like `SHELF_CORS_ORIGINS`, or as
    a CSP-style space-separated string, which is what an operator copying
    the directive is likely to paste. Anything else is passed through for
    pydantic to reject."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if text.startswith("["):
        return json.loads(text)
    return text.split()


def validate_sources(sources: list[str]) -> list[str]:
    """Refuse anything that is not a single CSP source expression, so a
    typo fails at startup rather than shipping a header that silently
    allows, or blocks, more than intended."""
    for source in sources:
        if source.lower() in {"self", "none"}:
            raise ValueError(
                f"frame_ancestors: write {source!r} as '{source.lower()}', "
                "in single quotes; bare, CSP reads it as a host name"
            )
        if source.lower() not in _KEYWORDS and not _SOURCE.fullmatch(source):
            raise ValueError(
                f"frame_ancestors: {source!r} is not a CSP source expression"
            )
    if len(sources) > 1 and any(s.lower() == "'none'" for s in sources):
        raise ValueError("frame_ancestors: 'none' cannot be combined with other sources")
    return sources


def header_value(sources: list[str]) -> str:
    return "frame-ancestors " + " ".join(sources)


class FrameAncestorsMiddleware:
    """Adds the CSP header to HTML responses.

    Pure ASGI rather than `BaseHTTPMiddleware`, which would sit between
    every streamed response (attachment downloads, ZIP exports) and the
    client for the sake of one header on index.html.
    """

    def __init__(self, app: ASGIApp, sources: list[str]) -> None:
        self.app = app
        self.value = header_value(sources)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if headers.get("content-type", "").lower().startswith("text/html"):
                    headers.append("Content-Security-Policy", self.value)
            await send(message)

        await self.app(scope, receive, send_with_header)
