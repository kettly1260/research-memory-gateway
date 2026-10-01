"""Pytest configuration for the gateway test suite.

Contains one narrow, Windows-only workaround.  Pytest's tmpdir plugin creates a
``pytest-current`` symlink inside its base temp directory and resolves it during
teardown; on Windows hosts that run the suite under a sandboxed filesystem the
symlink is a reparse point that ``Path.resolve()`` refuses to traverse, so
teardown raises ``OSError: [WinError 448]`` *after* every test has already run.
That turns a fully green suite into a non-zero exit.

Swallowing that single housekeeping error keeps local Windows runs honest
without touching test behaviour.  It is a no-op on Linux/macOS, which is where CI
runs, so CI still exercises the real cleanup path.

Note on HTTP proxies: this is deliberately **not** the place to configure
``NO_PROXY``.  Setting it globally changes behaviour for every HTTP client in the
process, including the MCP Streamable HTTP client, whose long-lived SSE stream
depends on the environment's proxy on some hosts.  Tests that talk to a local
server over a short-lived HTTP client opt out per client with
``httpx.Client(..., trust_env=False)`` instead.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover - platform specific
    try:
        import _pytest.pathlib as _pytest_pathlib
        import _pytest.tmpdir as _pytest_tmpdir

        _original_cleanup = _pytest_pathlib.cleanup_dead_symlinks

        def _tolerant_cleanup(root) -> None:  # type: ignore[no-untyped-def]
            try:
                _original_cleanup(root)
            except OSError:
                # WinError 448 on a sandbox-created reparse point; purely
                # cosmetic housekeeping, never a test failure.
                pass

        _pytest_pathlib.cleanup_dead_symlinks = _tolerant_cleanup
        # `tmpdir` binds the name at import time, so patch it there as well.
        if hasattr(_pytest_tmpdir, "cleanup_dead_symlinks"):
            _pytest_tmpdir.cleanup_dead_symlinks = _tolerant_cleanup
    except Exception:  # pragma: no cover - defensive
        pass
