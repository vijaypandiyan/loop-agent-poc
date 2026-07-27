"""Preflight check: is the endpoint reachable and is ADK_MODEL a real model?

    python check_model.py

Useful when main.py dies with a raw HTTP error.  A `404 page not found` almost
always means the *base URL* is wrong (it must end with ``/v1``); a JSON 404
means the *model id* is wrong; 503 ResourceExhausted means the model is simply
busy right now.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402


def main() -> None:
    if config.MODEL_PROVIDER == "gemini":
        print("MODEL_PROVIDER=gemini - nothing to check here; just make sure GEMINI_API_KEY is set.")
        return

    if config.MODEL_PROVIDER == "nvidia":
        base = config.NVIDIA_BASE_URL
        import os

        key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_NIM_API_KEY", "")
    else:
        import os

        base = os.environ.get("OPENAI_API_BASE", "")
        key = os.environ.get("OPENAI_API_KEY", "")

    print(f"provider  : {config.MODEL_PROVIDER}")
    print(f"base url  : {base}")
    print(f"model     : {config.MODEL_NAME}")
    print(f"api key   : {'set (' + key[:6] + '...)' if key else 'MISSING'}")

    if not base.rstrip("/").endswith("/v1"):
        print("\n!! base url does not end with /v1 - that is what causes '404 page not found'.")

    url = base.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        print(f"\nGET {url} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
        return
    except Exception as exc:  # pragma: no cover
        print(f"\nGET {url} failed: {exc}")
        return

    ids = sorted(m["id"] for m in data.get("data", []))
    print(f"\nendpoint reachable, {len(ids)} models available.")
    for fb in config.FALLBACK_MODELS:
        print(f"fallback '{fb}': {'OK' if fb in ids else 'NOT FOUND (remove it!)'}")
    if config.MODEL_NAME in ids:
        print(f"OK: '{config.MODEL_NAME}' exists. Any failure now is capacity (503) or tooling.")
    else:
        print(f"NOT FOUND: '{config.MODEL_NAME}' is not in the list. Closest matches:")
        needle = config.MODEL_NAME.split("/")[-1].split("-")[0].lower()
        for mid in ids:
            if needle in mid.lower():
                print(f"  {mid}")


if __name__ == "__main__":
    main()
