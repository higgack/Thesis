from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential
from .. import config

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def complete(
    model: str,
    system: str | list[dict],
    messages: list[dict],
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    resp = await _client.messages.create(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def cached_system(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
