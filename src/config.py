import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None
# Dedicated study-notes (체화) channel. When set, posts here route to the
# study-notes pipeline (src/notes/) instead of the brain ingest.
STUDY_CHANNEL_ID = os.getenv("STUDY_CHANNEL_ID", "").strip() or None
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "").strip() or None

# Knowledge-graph backend (kg.db). KG_AUTO=1 → each ingested doc's summary
# is extracted into fact triples in the background (one cheap Flash-Lite
# call/doc). KG_DAILY_BUDGET_KRW caps the daily auto-extract spend so a
# bulk channel import can't run up a surprise bill — past the cap, the
# hook skips silently until KST midnight. Manual /kg_extract is unaffected.
KG_AUTO = os.getenv("KG_AUTO", "1") == "1"
KG_DAILY_BUDGET_KRW = float(os.getenv("KG_DAILY_BUDGET_KRW", "300"))

# Optional now: only used when GEMINI_BACKEND=aistudio (the default).
# A Vertex deployment authenticates via ADC, so a vertex-only .env
# without GOOGLE_API_KEY must still import cleanly.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

# ── Gemini transport backend ─────────────────────────────────────────
# "aistudio" (default) → Google AI Studio Gemini API via api_key.
# "vertex"             → Vertex AI (aiplatform) via ADC. Same models and
#   the same generate_content / embed_content surface (the google-genai
#   SDK abstracts the transport), but Vertex bills to the project's Cloud
#   Billing account — and, unlike the AI Studio Gemini API, its usage is
#   eligible for the GCP Free Trial credit. Flip via .env; no code change.
GEMINI_BACKEND = os.getenv("GEMINI_BACKEND", "aistudio").strip().lower()
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "").strip()
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1").strip()


def make_genai_client():
    """The single google-genai client factory every module uses instead
    of genai.Client(...). Centralised so the AI Studio → Vertex switch is
    one env var, not seven edits. Lazy import keeps google-genai off the
    import path for callers that only need config constants."""
    from google import genai
    if GEMINI_BACKEND == "vertex":
        return genai.Client(
            vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION
        )
    return genai.Client(api_key=GOOGLE_API_KEY)

OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "").strip() or None
OBSIDIAN_GIT_REMOTE = os.getenv("OBSIDIAN_GIT_REMOTE", "").strip() or None

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "files").mkdir(exist_ok=True)
(DATA_DIR / "chroma").mkdir(exist_ok=True)

# Off-disk daily backup target (GCS bucket name, no gs:// prefix). The full
# VM disk is snapshotted weekly to save cost; this backs up the small,
# irreplaceable KB state (★/메모/알람 marks, Q&A·notes·KG·wiki·cost) DAILY
# off-disk at near-zero cost so the 7-day snapshot window can't lose it.
# Unset → backup job no-ops (see docs/backup.md for one-time bucket setup).
BACKUP_BUCKET = os.getenv("BACKUP_BUCKET", "").strip()

# YouTube cookies (burner account) to dodge the GCP-IP "not a bot" wall.
# Drop a Netscape-format cookies.txt at data/yt_cookies.txt (or point
# YT_COOKIES_FILE elsewhere); yt-dlp uses it when present. Absent → normal
# (cookie-less) fetch, so this is a no-op until the file exists.
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "").strip() or str(
    DATA_DIR / "yt_cookies.txt")

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gemini-2.5-flash-lite")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-2.5-flash")
DEEP_MODEL = os.getenv("DEEP_MODEL", "gemini-2.5-pro")
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")

# Chunk size in tokens. 1000 (was 700 → 400) reduces total chunks by
# ~30% on top of prior savings — less embedding cost + storage, and
# 1000-token windows are still squarely in Gemini-embedding-001's
# sweet spot so retrieval quality holds.
# Env-overridable for fast revert if retrieval quality regresses.
CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "1000"))
# NOTE: CHUNK_OVERLAP only applies to the token-slice fallback in
# chunker._slice_by_tokens (used when a SINGLE paragraph/sentence
# exceeds CHUNK_TOKENS — dense tables, one giant run-on). The common
# path greedily packs whole paragraphs with NO inter-chunk overlap;
# cross-boundary recall there comes from boundary preservation
# (chunks end on paragraph/sentence breaks, not mid-thought), not from
# overlap. If split-fact recall ever proves weak, the fix is to add a
# small sentence carry-over between packed chunks (cost: more chunks =
# more embedding), not to bump this value.
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150"))
TOP_K = 10
SUMMARY_MAX_TOKENS = 1000
HINT_SUMMARY_MIN_CHARS = 200
HINT_SUMMARY_MAX_CHARS = 2000

# ── LLM Wiki (P1–P5): additive synthesis/accumulation layer ──────────
# DORMANT BY DEFAULT. With WIKI_ENABLED=0 the wiki layer is completely
# inert — no enqueue, no nightly merge, no query-first — so the system
# behaves byte-for-byte as before. See docs/WIKI.md for the cost model,
# risk table, rollback runbook, and usage guide.
WIKI_ENABLED = os.getenv("WIKI_ENABLED", "0") == "1"
# Answer path (P2): lead Q&A with the synthesized page. Separate, stricter
# gate so pages can be built + reviewed before they influence answers.
WIKI_QUERY_FIRST = os.getenv("WIKI_QUERY_FIRST", "1") == "1"
# Merge model — flash by default (NOT pro). Every merge is tagged
# purpose="wiki" so /usage + cost.db isolate the wiki's real spend.
WIKI_MERGE_MODEL = os.getenv("WIKI_MERGE_MODEL", ANSWER_MODEL)
# DEPRECATED: the synthesis batch now runs hourly (on the hour, KST) instead
# of once nightly, so this is no longer read by the scheduler. Kept defined
# for .env back-compat (setting it is a harmless no-op).
WIKI_BATCH_HOUR = int(os.getenv("WIKI_BATCH_HOUR", "4"))
# Importance gate: docs whose summary is shorter than this are archived as
# notes but NOT wiki-merged (keeps low-signal forwards from spending tokens).
WIKI_MIN_SUMMARY_CHARS = int(os.getenv("WIKI_MIN_SUMMARY_CHARS", "600"))
# Per-run cost caps.
WIKI_MAX_TOPICS_PER_RUN = int(os.getenv("WIKI_MAX_TOPICS_PER_RUN", "25"))
WIKI_MAX_DOCS_PER_TOPIC = int(os.getenv("WIKI_MAX_DOCS_PER_TOPIC", "6"))
WIKI_MAX_PAGE_CHARS = int(os.getenv("WIKI_MAX_PAGE_CHARS", "30000"))
WIKI_DOC_SUMMARY_CHARS = int(os.getenv("WIKI_DOC_SUMMARY_CHARS", "1200"))
WIKI_MERGE_MAX_TOKENS = int(os.getenv("WIKI_MERGE_MAX_TOKENS", "12000"))
WIKI_CONSOLIDATION_CHARS = int(os.getenv("WIKI_CONSOLIDATION_CHARS", "30000"))
WIKI_BATCH_THROTTLE_SEC = float(os.getenv("WIKI_BATCH_THROTTLE_SEC", "0.5"))
WIKI_PARALLEL = int(os.getenv("WIKI_PARALLEL", "3"))
# Daily spend circuit breaker (KST). When today's wiki cost reaches this
# many ₩, the batch BLOCKS for the rest of the KST day and fires an
# actionable alert; queued docs resume next day. 0 = no cap.
WIKI_DAILY_BUDGET_KRW = float(os.getenv("WIKI_DAILY_BUDGET_KRW", "1000"))
