import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None

GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip() or None
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip() or None

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)
(DATA_DIR / "chroma").mkdir(exist_ok=True)

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gemini-2.5-flash-lite")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-2.5-flash")
DEEP_MODEL = os.getenv("DEEP_MODEL", "gemini-2.5-pro")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

CHUNK_TOKENS = 500
CHUNK_OVERLAP = 60
TOP_K = 5
SUMMARY_MAX_TOKENS = 600
HINT_SUMMARY_MIN_CHARS = 200
HINT_SUMMARY_MAX_CHARS = 2000
