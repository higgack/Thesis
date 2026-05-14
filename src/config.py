import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "").strip() or None

GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]

OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "").strip() or None
OBSIDIAN_GIT_REMOTE = os.getenv("OBSIDIAN_GIT_REMOTE", "").strip() or None

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)
(DATA_DIR / "chroma").mkdir(exist_ok=True)

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gemini-2.5-flash-lite")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-2.5-flash")
DEEP_MODEL = os.getenv("DEEP_MODEL", "gemini-2.5-pro")
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")

# Chunk size in tokens. 700 instead of 400 reduces total chunks by
# ~40% (less embedding cost + storage), and the larger window keeps
# more local context per chunk so retrieval is at least as good — both
# BGE-M3 and Gemini embedding handle 512+ tokens well. Overlap scales
# proportionally to preserve cross-boundary recall.
# Env-overridable for fast revert if retrieval quality regresses.
CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "700"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
TOP_K = 15
SUMMARY_MAX_TOKENS = 1000
HINT_SUMMARY_MIN_CHARS = 200
HINT_SUMMARY_MAX_CHARS = 2000
