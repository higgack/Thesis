from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential
from .. import config

_client = genai.Client(api_key=config.GOOGLE_API_KEY)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def complete(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    resp = await _client.aio.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return resp.text or ""
