from __future__ import annotations

import os
import sys
import httpx

DEFAULT_BASE_URL = "http://192.168.22.102:28001/v1"
DEFAULT_MODEL = "bge-m3"
EXPECTED_DIM = 1024


def main() -> int:
    base_url = os.getenv("EMBEDDING_BASE_URL") or DEFAULT_BASE_URL
    model = os.getenv("EMBEDDING_MODEL") or DEFAULT_MODEL
    url = f"{base_url.rstrip('/')}/embeddings"

    print(f"Testing NAS embedding endpoint: {url} (model={model})")
    payload = {
        "input": "Chemical synthesis reaction yield and 324 nm UV detection.",
        "model": model,
    }

    timeout = float(os.getenv("EMBEDDING_TIMEOUT") or "30.0")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                print(f"[FAIL] Server returned status code {resp.status_code}: {resp.text}")
                return 1
            data = resp.json()
            vector = data["data"][0]["embedding"]
            dim = len(vector)
            print(f"[PASS] Successfully received embedding vector! Dimension={dim} (Expected={EXPECTED_DIM})")
            if dim != EXPECTED_DIM:
                print(f"[WARN] Dimension mismatch! Expected {EXPECTED_DIM}, got {dim}")
                return 1
            return 0
    except httpx.RequestError as exc:
        print(f"[SKIP] NAS endpoint unreachable or timed out: {exc}")
        print("Note: This smoke test is optional and depends on local NAS connectivity.")
        # Keep SKIP distinct from PASS for CI/reporting.  Exit code 2 is a
        # deliberate non-success status meaning "not verifiable here".
        return 2


if __name__ == "__main__":
    sys.exit(main())
