"""JD capture: paste text, or a single plain GET against a URL. No scraping.

The raw response is archived to the snapshots dir (postings vanish; the
snapshot is the provenance), and a cleaned markdown version goes into the DB.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_BLANK_LINES = re.compile(r"\n{3,}")
# script/style hold code, not job text — remove wholesale before conversion.
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


class CaptureError(Exception):
    pass


@dataclass
class CapturedJD:
    markdown: str
    jd_hash: str
    snapshot_path: str | None


def _finish(markdown: str, raw: str | None, snapshots_dir: Path | None) -> CapturedJD:
    markdown = _BLANK_LINES.sub("\n\n", markdown.strip())
    jd_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    snapshot_path: str | None = None
    if raw is not None and snapshots_dir is not None:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = snapshots_dir / f"{jd_hash}.html"
        path.write_text(raw, encoding="utf-8")
        snapshot_path = str(path)
    return CapturedJD(markdown=markdown, jd_hash=jd_hash, snapshot_path=snapshot_path)


def capture_text(text: str, snapshots_dir: Path | None = None) -> CapturedJD:
    if not text.strip():
        raise CaptureError("empty JD text")
    return _finish(text, text, snapshots_dir)


def capture_url(url: str, snapshots_dir: Path | None = None) -> CapturedJD:
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise CaptureError(f"fetch failed: {exc} — paste the JD text instead") from exc
    if resp.status_code != 200:
        raise CaptureError(
            f"GET {url} returned {resp.status_code} — paste the JD text instead"
        )
    raw = resp.text
    content_type = resp.headers.get("content-type", "")
    if "html" in content_type or "<html" in raw[:2000].casefold():
        from markdownify import markdownify

        cleaned = _SCRIPT_STYLE.sub("", raw)
        markdown = str(markdownify(cleaned, strip=["nav", "footer"]))
    else:
        markdown = raw
    captured = _finish(markdown, raw, snapshots_dir)
    if len(captured.markdown) < 200:
        raise CaptureError(
            "page fetched but yielded almost no text (bot wall or JS-rendered page) — "
            "paste the JD text instead"
        )
    return captured
