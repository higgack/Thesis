from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential
from .. import config

_client = genai.Client(api_key=config.GOOGLE_API_KEY)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
async def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    if not texts:
        return []
    resp = await _client.aio.models.embed_content(
        model=config.EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return [e.values for e in resp.embeddings]
