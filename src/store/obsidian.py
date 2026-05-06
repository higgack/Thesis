import asyncio
import logging
import re
import subprocess
from datetime import date
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_VAULT: Path | None = (
    Path(config.OBSIDIAN_VAULT_PATH).resolve() if config.OBSIDIAN_VAULT_PATH else None
)
_REMOTE = config.OBSIDIAN_GIT_REMOTE
_GIT_LOCK = asyncio.Lock()

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


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    where = cwd if cwd is not None else _VAULT
    return subprocess.run(
        ["git", *args],
        cwd=str(where) if where else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
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


async def write_note(*, doc_type: str, title: str, source: str,
                     summary: str, body: str, doc_id: str,
                     tags: list[str] | None = None) -> str | None:
    if not enabled():
        return None
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
    path.write_text(md, encoding="utf-8")
    rel = path.relative_to(_VAULT).as_posix()

    if _REMOTE:
        async with _GIT_LOCK:
            await asyncio.to_thread(_commit_push, rel, title)
    return rel


def _commit_push(relpath: str, title: str) -> None:
    if not (_VAULT / ".git").exists():
        return
    try:
        _git("add", relpath)
        _git("commit", "-m", f"add: {title[:60]}")
    except subprocess.CalledProcessError as e:
        log.debug("nothing to commit or commit failed: %s", e.stderr)
        return
    try:
        _git("push", "origin", "HEAD")
    except subprocess.CalledProcessError as e:
        log.warning("git push failed: %s", e.stderr)
