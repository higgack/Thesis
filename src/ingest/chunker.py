import tiktoken
from .. import config

_enc = tiktoken.get_encoding("cl100k_base")


def split(text: str, size: int = config.CHUNK_TOKENS,
          overlap: int = config.CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    tokens = _enc.encode(text)
    if len(tokens) <= size:
        return [text]
    step = size - overlap
    chunks = []
    for start in range(0, len(tokens), step):
        piece = tokens[start:start + size]
        if not piece:
            break
        chunks.append(_enc.decode(piece))
        if start + size >= len(tokens):
            break
    return chunks


def token_len(text: str) -> int:
    return len(_enc.encode(text))
