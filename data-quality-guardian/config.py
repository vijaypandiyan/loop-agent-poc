"""Central configuration: where the databases live and which model to use.

Kept tiny on purpose - every other module imports its paths from here so the
learning examples never hard-code a file name in two places.
"""

import os
from pathlib import Path

# Project root (the directory that contains this file).
ROOT = Path(__file__).resolve().parent

# System of record: a plain SQLite file.
SQLITE_PATH = ROOT / "shop.db"

# Analytical mirror: an embedded Kuzu graph database (a directory on disk).
KUZU_PATH = ROOT / "shop_graph"

# Gemini model used by every LlmAgent. Override with ADK_MODEL if you like.
MODEL = os.environ.get("ADK_MODEL", "gemini-2.0-flash")
