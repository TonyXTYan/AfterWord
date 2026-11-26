"""The GUI page's Content-Security-Policy: a per-response script nonce, and
no inline event handlers left in the page for it to break."""

import re
from pathlib import Path


UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"


def csp_of(r):
    return {p.split()[0]: p.split()[1:] for p in r.headers["content-security-policy"].split("; ")}


def test_page_sends_the_policy_with_a_nonce_on_every_script(gui):
    _, client = gui
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = csp_of(r)
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'self'"]
    assert csp["media-src"] == ["'self'", "blob:"]
    assert csp["img-src"] == ["'self'", "data:"]
    assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
    assert csp["font-src"] == ["'self'", "data:"]
    assert csp["base-uri"] == csp["form-action"] == csp["object-src"] == csp["frame-ancestors"] == ["'none'"]
    (src,) = csp["script-src"]
    nonce = re.fullmatch(r"'nonce-([A-Za-z0-9_-]{16,})'", src).group(1)
    tags = re.findall(r"<script\b[^>]*>", r.text)
    assert len(tags) >= 2
    assert all(t == f'<script nonce="{nonce}">' for t in tags)
    assert r.headers["x-frame-options"] == "DENY"


def test_nonce_differs_per_response(gui):
    _, client = gui
    a, b = client.get("/"), client.get("/")
    assert a.headers["content-security-policy"] != b.headers["content-security-policy"]
    assert a.text != b.text


def test_other_responses_keep_the_frame_ancestors_policy(gui):
    _, client = gui
    assert client.get("/api/status").headers["content-security-policy"] == "frame-ancestors 'none'"


def test_page_has_no_inline_event_handlers_or_javascript_urls():
    """A nonce policy blocks onclick="..." attributes and javascript: URLs; the page
    wires events with addEventListener (and h()'s on* props, which do the same)."""
    src = UI.read_text(encoding="utf-8")
    # Markup (HTML and template literals): an attribute named on<event> on a tag.
    bad = re.findall(r"<[a-zA-Z][^<>]*?\s(on[a-z]+)\s*=", src)
    assert bad == [], f"inline handlers: {bad}"
    # setAttribute("onclick", ...) would be the same thing built at runtime.
    assert not re.search(r"setAttribute\(\s*[\"']on", src)
    assert not re.search(r"javascript:", src, re.IGNORECASE)
    assert "eval(" not in src and "new Function(" not in src
