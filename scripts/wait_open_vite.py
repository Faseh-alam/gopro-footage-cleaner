"""Wait until the Vite dev server is ready, then open /review in the browser.

Detects Vite by probing /@vite/client (reliable even when the port is not 8081).
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
import webbrowser

TIMEOUT_SEC = 90.0
# Prefer Lovable/Vite defaults, then walk nearby ports if the preferred one is taken.
CANDIDATE_PORTS = (
    list(range(8080, 8101))
    + list(range(5173, 5201))
    + [3000, 4173, 5172]
)


def is_vite(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/@vite/client"
    try:
        with urllib.request.urlopen(url, timeout=0.45) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main() -> int:
    deadline = time.time() + TIMEOUT_SEC
    while time.time() < deadline:
        for port in CANDIDATE_PORTS:
            if is_vite(port):
                open_url = f"http://localhost:{port}/review"
                print(f"Vite ready on port {port}; opening {open_url}")
                webbrowser.open(open_url)
                return 0
        time.sleep(0.4)

    print("Timed out waiting for Vite dev server.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
