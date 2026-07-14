from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from applyr.ingest import jd_capture
from applyr.ingest.jd_capture import CaptureError, capture_text, capture_url


def test_capture_text_hashes_and_snapshots(tmp_path: Path) -> None:
    captured = capture_text("Senior Backend Engineer\n\n\n\n\nKubernetes required.", tmp_path)
    assert len(captured.jd_hash) == 64
    assert "\n\n\n" not in captured.markdown  # blank lines collapsed
    assert captured.snapshot_path is not None
    assert Path(captured.snapshot_path).exists()


def test_capture_text_is_deterministic(tmp_path: Path) -> None:
    a = capture_text("same text", tmp_path)
    b = capture_text("same text", tmp_path)
    assert a.jd_hash == b.jd_hash


def test_capture_empty_raises() -> None:
    with pytest.raises(CaptureError):
        capture_text("   ")


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "text/html"}


def test_capture_url_converts_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = (
        "<html><body><h1>Backend Engineer</h1>"
        + "<p>We need Kubernetes, Python and Postgres experience. "
        + "You will build payment infrastructure at scale. " * 10
        + "</p><script>tracking()</script></body></html>"
    )

    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        return _FakeResponse(html)

    monkeypatch.setattr(jd_capture.httpx, "get", fake_get)
    captured = capture_url("https://example.com/job", tmp_path)
    assert "Backend Engineer" in captured.markdown
    assert "tracking()" not in captured.markdown
    assert captured.snapshot_path is not None
    assert "<h1>" in Path(captured.snapshot_path).read_text(encoding="utf-8")


def test_capture_url_bot_wall_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        return _FakeResponse("<html><body>Just a moment...</body></html>")

    monkeypatch.setattr(jd_capture.httpx, "get", fake_get)
    with pytest.raises(CaptureError, match="paste the JD"):
        capture_url("https://example.com/job", tmp_path)


def test_capture_url_non_200_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kw: Any) -> _FakeResponse:
        return _FakeResponse("nope", status_code=403)

    monkeypatch.setattr(jd_capture.httpx, "get", fake_get)
    with pytest.raises(CaptureError, match="403"):
        capture_url("https://example.com/job", tmp_path)
