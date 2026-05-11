"""Text chunking for embedding.

Recursive boundary-preserving split: paragraph → sentence → token
slice. Tries to keep each chunk semantically intact so retrieval
returns coherent passages instead of mid-sentence shrapnel, which
matters more than the size cap exactness — search recall jumps
noticeably once chunks stop ending in '결정적인 영업' / '...에 대한'
fragments."""
import re

import tiktoken

from .. import config

_enc = tiktoken.get_encoding("cl100k_base")

_PARA_SPLIT = re.compile(r"\n{2,}")
_SENT_SPLIT = re.compile(r"(?<=[.!?。！?])\s+|\n")


def token_len(text: str) -> int:
    return len(_enc.encode(text))


def _slice_by_tokens(text: str, size: int, overlap: int) -> list[str]:
    """Last-resort cut for single units that don't fit even after
    paragraph/sentence splitting."""
    toks = _enc.encode(text)
    if len(toks) <= size:
        return [text]
    step = max(1, size - overlap)
    out: list[str] = []
    for start in range(0, len(toks), step):
        piece = toks[start:start + size]
        if not piece:
            break
        out.append(_enc.decode(piece))
        if start + size >= len(toks):
            break
    return out


def split(text: str, size: int = config.CHUNK_TOKENS,
          overlap: int = config.CHUNK_OVERLAP) -> list[str]:
    """Boundary-preserving split:
      1. paragraph (blank line)
      2. sentence (. ! ? 。 ! ?)
      3. token slice fallback

    Greedily packs units up to `size` tokens so we don't fragment a
    coherent paragraph just because the next one would tip over."""
    text = text.strip()
    if not text:
        return []
    if token_len(text) <= size:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0

    def flush():
        nonlocal buf, buf_tok
        if buf:
            chunks.append("\n\n".join(buf).strip())
            buf = []
            buf_tok = 0

    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    for para in paragraphs:
        pt = token_len(para)

        # A single paragraph is bigger than the cap — break it down
        # into sentences first, then token slices if still too big.
        if pt > size:
            flush()
            sentences = [s.strip() for s in _SENT_SPLIT.split(para) if s.strip()]
            sbuf: list[str] = []
            sbuf_tok = 0
            for sent in sentences:
                st = token_len(sent)
                if st > size:
                    # Even one sentence is huge (e.g. dense table) — token slice it
                    if sbuf:
                        chunks.append(" ".join(sbuf).strip())
                        sbuf = []
                        sbuf_tok = 0
                    chunks.extend(_slice_by_tokens(sent, size, overlap))
                elif sbuf_tok + st <= size:
                    sbuf.append(sent)
                    sbuf_tok += st
                else:
                    chunks.append(" ".join(sbuf).strip())
                    sbuf = [sent]
                    sbuf_tok = st
            if sbuf:
                chunks.append(" ".join(sbuf).strip())
            continue

        # Normal-size paragraph: pack into current buffer or flush + start new
        if buf_tok + pt <= size:
            buf.append(para)
            buf_tok += pt
        else:
            flush()
            buf.append(para)
            buf_tok = pt

    flush()
    return chunks
