"""Fetch a filing from SEC.gov by its EDGAR link.

Only documents under https://www.sec.gov/Archives/edgar/data/ are fetched. The link is
taken apart and rebuilt (scheme, host, port, path only), every redirect is checked the
same way *before* it's followed, the body is read in chunks up to a size cap and a
total deadline, and only HTML is accepted. So the server can't be pointed at anything
else, or tied up by a huge or slow response.

SEC requires automated requests to say who is asking: set SEC_USER_AGENT to a name and
contact email, e.g. "Jane Doe jane@example.com" (https://www.sec.gov/os/accessing-edgar-data).
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DOC_PATH = re.compile(r"^/Archives/edgar/data/\d+/\d+/[\w.-]+\.html?$")
INDEX_PAGE = re.compile(r"-index(-headers)?\.html?$", re.I)
CHUNK = 64 * 1024
DEADLINE = 60       # seconds for the whole download, not per read
TIMEOUT = 15        # seconds per socket operation


def canonical(url) -> str:
    """The rebuilt link if it's an EDGAR filing document; otherwise a ValueError she can act on."""
    raw = url.strip() if isinstance(url, str) else ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        parts, port = None, None
    if parts and INDEX_PAGE.search(parts.path):
        raise ValueError("That's the filing's index page. Open it and copy the link to the DEF 14A "
                         "document itself (it ends in .htm).")
    if not (parts and parts.scheme.lower() == "https" and (parts.hostname or "").lower() == "www.sec.gov"
            and parts.username is None and parts.password is None and port in (None, 443)
            and DOC_PATH.match(parts.path)):
        raise ValueError("Paste the filing's link on SEC.gov: it starts with "
                         "https://www.sec.gov/Archives/edgar/data/ and ends in .htm.")
    return "https://www.sec.gov" + parts.path        # no query, fragment, userinfo or port


class _OnlyEdgar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            newurl = canonical(newurl)
        except ValueError:
            raise ValueError("SEC.gov redirected that link somewhere else, so it wasn't followed.") from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_OnlyEdgar)


def fetch(url: str, limit: int) -> tuple[str, str]:
    """Download the filing. Returns (canonical url, HTML). Raises ValueError with a message
    for the analyst (a status code and short reason, never the response body)."""
    url = canonical(url)
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in agent:
        raise ValueError("SEC.gov needs to know who is downloading. Set SEC_USER_AGENT to your name "
                         "and email (e.g. SEC_USER_AGENT=\"Jane Doe jane@example.com\") and restart "
                         "the server.")
    # No Accept-Encoding: a small compressed body can't unpack into something huge.
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "text/html"})
    start = time.monotonic()
    try:
        with _opener.open(req, timeout=TIMEOUT) as res:
            ctype = res.headers.get_content_type()
            if ctype not in ("text/html", "application/xhtml+xml"):
                raise ValueError(f"That link isn't an HTML document ({ctype}).")
            charset = res.headers.get_content_charset() or "utf-8"
            body = bytearray()
            # read1 does at most one socket read per call (read(n) would keep waiting until it has
            # all n bytes), and each socket read may take only the time that's left, so a server
            # sending a byte every few seconds can't stretch the download past the deadline.
            read = getattr(res, "read1", res.read)
            sock = getattr(getattr(getattr(res, "fp", None), "raw", None), "_sock", None)
            while len(body) <= limit:
                left = DEADLINE - (time.monotonic() - start)
                if left <= 0:
                    raise ValueError(f"SEC.gov took over {DEADLINE} seconds to send it; try again.")
                if sock is not None:
                    sock.settimeout(min(TIMEOUT, left))
                chunk = read(CHUNK)
                if not chunk:
                    break
                body += chunk
    except urllib.error.HTTPError as e:
        hint = " SEC.gov refused the request; check SEC_USER_AGENT." if e.code in (403, 429) else ""
        raise ValueError(f"SEC.gov answered {e.code} for that link.{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"Couldn't reach SEC.gov ({type(getattr(e, 'reason', e)).__name__}).") from None
    if len(body) > limit:
        raise ValueError(f"That document is over {limit // (1024 * 1024)} MB, so it wasn't fetched.")
    try:
        return url, bytes(body).decode(charset, errors="ignore")
    except LookupError:
        return url, bytes(body).decode("utf-8", errors="ignore")
