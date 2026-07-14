"""applyr — local-first, single-user job application tracker."""

import sys

# CLI/MCP output uses em dashes, arrows, and box-drawing characters. On Windows,
# stdout/stderr default to the legacy ANSI code page (not UTF-8) whenever the
# process isn't attached to a real console — piped, redirected, or spawned by
# an MCP client over stdio — which silently mangles that output into "?".
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

__version__ = "0.1.0"
