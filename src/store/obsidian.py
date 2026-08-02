import asyncio
import logging
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_VAULT: Path | None = (
    Path(config.OBSIDIAN_VAULT_PATH).resolve() if config.OBSIDIAN_VAULT_PATH else None
)
_REMOTE = config.OBSIDIAN_GIT_REMOTE
_GIT_LOCK = asyncio.Lock()
# Background sync task refs (prevent GC of fire-and-forget tasks).
_BG_SYNC_TASKS: set = set()
# Push circuit breaker: after a failed/hung push, skip pushes until this
# epoch so a broken/unreachable remote can't make every ingested note
# pay the push timeout. Commits still land locally; a later successful
# push ships the whole backlog at once.
_PUSH_DISABLED_UNTIL = 0.0
_PUSH_BACKOFF_SEC = 600

_FOLDERS = {
    "url": "Web",
    "pdf": "Papers",
    "youtube": "YouTube",
    "text": "Notes",
    "paper": "Papers",
}
_SAFE = re.compile(r"[^\w가-힣\- ]+")


def enabled() -> bool:
    return _VAULT is not None


def init() -> None:
    if not enabled():
        return
    _VAULT.mkdir(parents=True, exist_ok=True)
    if _REMOTE and not (_VAULT / ".git").exists():
        try:
            _git("clone", _REMOTE, str(_VAULT), cwd=None)
        except Exception:
            log.warning("git clone failed; initializing empty repo")
            _git("init", cwd=_VAULT)
            _git("remote", "add", "origin", _REMOTE)
    if (_VAULT / ".git").exists():
        _git("config", "user.name", "second-brain-bot")
        _git("config", "user.email", "bot@local")


def _git(*args: str, cwd: Path | None = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    where = cwd if cwd is not None else _VAULT
    return subprocess.run(
        ["git", *args],
        cwd=str(where) if where else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _slug(s: str) -> str:
    s = _SAFE.sub(" ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:80] or "untitled"


def _frontmatter(doc_id: str, title: str, source: str,
                 doc_type: str, tags: list[str]) -> str:
    safe_title = title.replace('"', "'")
    yaml_tags = "\n".join(f"  - {t}" for t in tags)
    return (
        "---\n"
        f"id: {doc_id}\n"
        f'title: "{safe_title}"\n'
        f"source: {source}\n"
        f"type: {doc_type}\n"
        f"ingested: {date.today().isoformat()}\n"
        f"tags:\n{yaml_tags}\n"
        "---\n\n"
    )


def _write_note_sync(*, doc_type: str, title: str, source: str,
                     summary: str, body: str, doc_id: str,
                     tags: list[str] | None) -> str:
    """Synchronous file write — ONLY call via asyncio.to_thread, never
    directly on the event loop. mkdir/write_text/os.replace are normally
    microseconds, but under disk contention (concurrent backlog drain,
    heavy sqlite writes elsewhere) they can stall long enough to freeze
    the loop itself — which blocks even asyncio.wait_for's own
    cancellation timer from firing (same 'unguarded sync work on the
    loop' bug class as pipeline.py's meta.py/wiki.enqueue fixes; this
    call site caused a 30min+ '저장'-stage stuck-ingest alert, 2026-08-01)."""
    folder = _VAULT / "SecondBrain" / _FOLDERS.get(doc_type, "Misc")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_slug(title)}.md"
    if path.exists():
        path = folder / f"{_slug(title)}-{doc_id[:6]}.md"

    fm = _frontmatter(doc_id, title, source, doc_type, tags or ["second-brain"])
    md = (
        f"{fm}# {title}\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Source\n\n{source}\n\n"
        f"## Original\n\n{body}\n"
    )
    # tmp+replace so a SIGKILL mid-write can't leave a truncated note.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, path)
    return path.relative_to(_VAULT).as_posix()


async def write_note(*, doc_type: str, title: str, source: str,
                     summary: str, body: str, doc_id: str,
                     tags: list[str] | None = None) -> str | None:
    if not enabled():
        return None
    rel = await asyncio.to_thread(
        _write_note_sync, doc_type=doc_type, title=title, source=source,
        summary=summary, body=body, doc_id=doc_id, tags=tags)

    # Fire-and-forget: the .md is already on disk (the note is saved);
    # commit/push runs in the background so a slow/hung remote can NEVER
    # block ingest. (A blocking await here serialized ingests behind a
    # 60s-hanging `git push` → small items waited 15min and timed out.)
    if _REMOTE:
        t = asyncio.create_task(_commit_push_bg(rel, title))
        _BG_SYNC_TASKS.add(t)
        t.add_done_callback(_BG_SYNC_TASKS.discard)
    return rel


async def _commit_push_bg(relpath: str, title: str) -> None:
    try:
        async with _GIT_LOCK:
            await asyncio.to_thread(_commit_push, relpath, title)
    except Exception:
        log.exception("obsidian background sync failed")


def _commit_push(relpath: str, title: str) -> None:
    if not (_VAULT / ".git").exists():
        return
    try:
        _git("add", relpath)
        _git("commit", "-m", f"add: {title[:60]}")
    except subprocess.CalledProcessError as e:
        log.debug("nothing to commit or commit failed: %s", e.stderr)
        return
    except subprocess.TimeoutExpired:
        log.warning("git commit timed out")
        return
    global _PUSH_DISABLED_UNTIL
    if time.time() < _PUSH_DISABLED_UNTIL:
        return  # push circuit-broken; commit stays local, syncs later
    try:
        # Shorter timeout than other git ops: a push is the only one that
        # talks to the network, so it's the one that hangs.
        _git("push", "origin", "HEAD", timeout=20)
    except Exception as e:
        # Catch BOTH CalledProcessError AND TimeoutExpired (the hang) —
        # the old code only caught the former, so a timed-out push raised
        # all the way up and the next note tried again immediately.
        _PUSH_DISABLED_UNTIL = time.time() + _PUSH_BACKOFF_SEC
        log.warning("git push failed (%s); pausing pushes %ds",
                    str(e)[:120], _PUSH_BACKOFF_SEC)


async def commit_subtree(subdir: str, message: str) -> None:
    """Commit (and push, if a remote is configured) everything under
    <vault>/<subdir> in ONE commit with a custom message. Used by the LLM
    Wiki nightly batch to version its pages — git gives the wiki free
    version history + a one-command rollback. Reuses the same git lock and
    push circuit-breaker as note writes, so a hung/unreachable remote can
    never block the batch (the commit lands locally; a later push ships
    the backlog)."""
    if not enabled() or not (_VAULT / ".git").exists():
        return
    async with _GIT_LOCK:
        await asyncio.to_thread(_commit_subtree_sync, subdir, message)


def _commit_subtree_sync(subdir: str, message: str) -> None:
    try:
        _git("add", subdir)
        _git("commit", "-m", message)
    except subprocess.CalledProcessError as e:
        # Most common path: nothing changed under the subtree → no commit.
        log.debug("wiki subtree: nothing to commit (%s)", e.stderr)
        return
    except subprocess.TimeoutExpired:
        log.warning("wiki subtree commit timed out")
        return
    global _PUSH_DISABLED_UNTIL
    if not _REMOTE or time.time() < _PUSH_DISABLED_UNTIL:
        return  # no remote, or push circuit-broken — commit stays local
    try:
        _git("push", "origin", "HEAD", timeout=20)
    except Exception as e:
        _PUSH_DISABLED_UNTIL = time.time() + _PUSH_BACKOFF_SEC
        log.warning("wiki subtree push failed (%s); pausing pushes %ds",
                    str(e)[:120], _PUSH_BACKOFF_SEC)
