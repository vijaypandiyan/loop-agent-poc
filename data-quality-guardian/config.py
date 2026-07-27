"""Central configuration: where the databases live and which LLM to use.

Kept tiny on purpose - every other module imports its paths from here so the
learning examples never hard-code a file name in two places.

Model selection is pluggable.  ADK talks to Gemini natively, and to *anything
else* through LiteLLM, so the same four agents can run on Gemini, or on
DeepSeek / Qwen / Llama hosted at https://build.nvidia.com (an OpenAI-compatible
endpoint), without changing a single line of agent code::

    # Gemini (default)
    export GEMINI_API_KEY=...

    # NVIDIA NIM (DeepSeek, Qwen, ...)
    export MODEL_PROVIDER=nvidia
    export NVIDIA_API_KEY=nvapi-...
    export ADK_MODEL=qwen/qwen3-next-80b-a3b-instruct

    # any other OpenAI-compatible server (vLLM, Ollama, Together, ...)
    export MODEL_PROVIDER=openai_compatible
    export OPENAI_API_BASE=http://localhost:8000/v1
    export OPENAI_API_KEY=sk-...
    export ADK_MODEL=qwen2.5-72b-instruct

NOTE: every agent here relies on **tool / function calling**, so pick a model
that supports it.  Verify the exact id first - ``python check_model.py`` lists
what your endpoint actually serves.  Known-good on build.nvidia.com:
``qwen/qwen3-next-80b-a3b-instruct``, ``meta/llama-3.3-70b-instruct``,
``nvidia/llama-3.3-nemotron-super-49b-v1.5``.
"""

import os
from pathlib import Path
from typing import Any

# Project root (the directory that contains this file).
ROOT = Path(__file__).resolve().parent

# Load a local .env file (if present) so you can keep keys out of your shell:
#     MODEL_PROVIDER=nvidia
#     NVIDIA_API_KEY=nvapi-...
#     ADK_MODEL=qwen/qwen3-next-80b-a3b-instruct
# Real environment variables always win over .env values.
try:
    from dotenv import load_dotenv  # ships with google-adk

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - dotenv is optional
    pass

# System of record: a plain SQLite file.
SQLITE_PATH = ROOT / "shop.db"

# Analytical mirror: an embedded Kuzu graph database.
KUZU_PATH = ROOT / "shop_graph"

# gemini | nvidia | openai_compatible
# Change this default (or set MODEL_PROVIDER in .env / your shell) to switch providers.
DEFAULT_PROVIDER = "gemini"
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", DEFAULT_PROVIDER).lower()

# Model name *without* the provider prefix.
DEFAULT_MODELS = {
    "gemini": "gemini-2.0-flash",
    # Verified present in GET https://integrate.api.nvidia.com/v1/models and
    # supports tool calling.  Check that endpoint before inventing a model id.
    "nvidia": "qwen/qwen3-next-80b-a3b-instruct",
    "openai_compatible": "gpt-4o-mini",
}
MODEL_NAME = os.environ.get("ADK_MODEL", DEFAULT_MODELS.get(MODEL_PROVIDER, "gemini-2.0-flash"))

NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Hosted free endpoints (build.nvidia.com in particular) return HTTP 503
# "ResourceExhausted" when a model is momentarily out of capacity.  LiteLLM can
# retry those for us, and optionally fail over to another model.
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "5"))
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
# e.g. ADK_FALLBACK_MODELS="meta/llama-3.3-70b-instruct,nvidia/llama-3.3-nemotron-super-49b-v1.5"
FALLBACK_MODELS = [
    m.strip() for m in os.environ.get("ADK_FALLBACK_MODELS", "").split(",") if m.strip()
]


def build_model() -> Any:
    """Return what an ``LlmAgent(model=...)`` expects for the chosen provider.

    Gemini needs only the model *string* (ADK has a built-in registry entry for
    it).  Everything else is wrapped in ``LiteLlm``, ADK's adapter for the ~100
    providers LiteLLM speaks.
    """
    if MODEL_PROVIDER == "gemini":
        return MODEL_NAME

    # Imported lazily so a Gemini-only user never needs litellm installed.
    from google.adk.models.lite_llm import LiteLlm

    if MODEL_PROVIDER == "nvidia":
        api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_NIM_API_KEY")
        if not api_key:
            raise RuntimeError("MODEL_PROVIDER=nvidia requires NVIDIA_API_KEY (nvapi-...)")
        # LiteLLM's OpenAI-compatible path; build.nvidia.com speaks that dialect.
        return LiteLlm(
            model=f"openai/{MODEL_NAME}",
            api_base=NVIDIA_BASE_URL,
            api_key=api_key,
            num_retries=LLM_RETRIES,
            timeout=LLM_TIMEOUT,
            fallbacks=[f"openai/{m}" for m in FALLBACK_MODELS] or None,
        )

    if MODEL_PROVIDER == "openai_compatible":
        return LiteLlm(
            model=f"openai/{MODEL_NAME}",
            api_base=os.environ.get("OPENAI_API_BASE"),
            api_key=os.environ.get("OPENAI_API_KEY"),
            num_retries=LLM_RETRIES,
            timeout=LLM_TIMEOUT,
        )

    raise RuntimeError(
        f"Unknown MODEL_PROVIDER '{MODEL_PROVIDER}' (use gemini | nvidia | openai_compatible)"
    )


def list_remote_models() -> list[str]:
    """GET <base>/models so we can fail fast on a typo'd model id.

    build.nvidia.com answers a bad model id with a bare ``404 page not found``,
    which is easy to mistake for a broken URL - hence this check.
    """
    import json
    import urllib.request

    if MODEL_PROVIDER == "nvidia":
        base, key = NVIDIA_BASE_URL, os.environ.get("NVIDIA_API_KEY", "")
    elif MODEL_PROVIDER == "openai_compatible":
        base, key = os.environ.get("OPENAI_API_BASE", ""), os.environ.get("OPENAI_API_KEY", "")
    else:
        return []

    req = urllib.request.Request(
        base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return sorted(m["id"] for m in json.load(resp).get("data", []))


def describe_model() -> str:
    return f"{MODEL_PROVIDER}:{MODEL_NAME}"


def missing_credentials() -> str | None:
    """Return a human-readable reason if the chosen provider is not configured."""
    if MODEL_PROVIDER == "gemini":
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            return "GEMINI_API_KEY is not set (or switch with MODEL_PROVIDER=nvidia)."
    elif MODEL_PROVIDER == "nvidia":
        if not (os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_NIM_API_KEY")):
            return "NVIDIA_API_KEY is not set (get one at https://build.nvidia.com)."
    elif MODEL_PROVIDER == "openai_compatible":
        if not os.environ.get("OPENAI_API_BASE"):
            return "OPENAI_API_BASE is not set."
    return None
