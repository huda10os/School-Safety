"""Fetch pages that sit behind a JavaScript bot check, using the agent-browser CLI.

Only used when a plain download returns a challenge page (for example the
"Client Challenge" interstitial on Apptegy-hosted district sites). Requires
`npm install -g agent-browser`. Each call uses a named session so cookies from
the challenge persist across pages of the same site; cookies() exposes them
so fetch.Fetcher downloads can reuse them where the site accepts that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

CHALLENGE_MARKERS = ("<title>Client Challenge</title>", "cf-challenge", "Just a moment...")
# A few concurrent sessions (one Chrome each, a few hundred MB) keep memory
# predictable while bot-checked sites no longer queue behind a single browser.
BROWSER_SLOTS = int(os.environ.get("TXBOARDS_BROWSER_SLOTS", "3"))
_lock = threading.BoundedSemaphore(BROWSER_SLOTS)


def available() -> bool:
    return shutil.which("agent-browser") is not None


def is_challenge(html: str) -> bool:
    head = html[:5000]
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _run(session: str, *args: str, timeout: int = 90) -> str:
    completed = subprocess.run(["agent-browser", "--session", session, *args],
                               capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"agent-browser {args[0]} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout


def page_html(url: str, session: str = "txboards", wait_ms: int = 4000) -> tuple[str, str]:
    """Open url, wait for the challenge to clear, return (final_url, html)."""
    with _lock:
        try:
            _run(session, "open", url)
        except RuntimeError as exc:
            # Sessions launched at the same moment occasionally fail to start
            # ("Could not configure browser"); one retry after a pause clears it.
            if "Could not configure browser" not in str(exc):
                raise
            time.sleep(5)
            _run(session, "open", url)
        _run(session, "wait", str(wait_ms))
        final_url = _run(session, "eval", "location.href").strip().strip('"')
        html = json.loads(_run(session, "eval", "JSON.stringify(document.documentElement.outerHTML)"))
        if isinstance(html, str) and html.startswith('"'):
            html = json.loads(html)
        return final_url, html


def cookies(session: str = "txboards") -> dict:
    with _lock:
        raw = _run(session, "cookies", "get", "--json")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    items = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = items.get("cookies", [])
    return {c["name"]: c["value"] for c in items if isinstance(c, dict) and "name" in c}


def close(session: str = "txboards") -> None:
    with _lock:
        subprocess.run(["agent-browser", "--session", session, "close"], capture_output=True, timeout=30)
