from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from .. import config

_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    resp = await _client.embeddings.create(model=config.EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]
