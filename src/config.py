import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip() or None
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip() or None

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)
(DATA_DIR / "chroma").mkdir(exist_ok=True)

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "claude-haiku-4-5")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "claude-sonnet-4-6")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")

CHUNK_TOKENS = 500
CHUNK_OVERLAP = 60
TOP_K = 5
SUMMARY_MAX_TOKENS = 600
