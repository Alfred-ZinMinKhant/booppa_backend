import logging
from typing import Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)


class DeepSeekProvider:
    def __init__(self, api_key: str | None, model: str = "deepseek-chat") -> None:
        self.api_key = api_key
        self.model = model

    async def call_chat(
        self, messages: List[Dict], json_mode: bool = False
    ) -> Optional[str]:
        """Call the chat API. Returns the message content, or None on any failure.

        `json_mode` asks the provider to constrain output to a valid JSON object.
        Callers that parse the response as JSON should set it: without it the
        only thing asking for JSON is the prompt text, and a single line of
        preamble makes the whole response unparseable.
        """
        if not self.api_key:
            return None

        try:
            from app.core.http_client import get_deepseek_client
            async with get_deepseek_client() as client:
                response = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.2,
                        # A CEILING, not a reservation — billing is on tokens
                        # actually generated, so raising this costs nothing on
                        # responses that were already fitting. 1400 was below
                        # what a multi-violation compliance report needs: the
                        # response truncated mid-JSON, failed to parse, and was
                        # discarded entirely in favour of the static templates.
                        # We paid for all 1400 of those tokens and used none.
                        "max_tokens": 4000,
                        **(
                            {"response_format": {"type": "json_object"}}
                            if json_mode
                            else {}
                        ),
                    },
                )
            if response.status_code >= 400:
                logger.error(
                    "DeepSeek API error %s: %s",
                    response.status_code,
                    response.text,
                )
                return None
            data = response.json()
            choice = (data.get("choices") or [{}])[0]

            # A truncated response still returns 200 with partial content, so
            # this is the ONLY signal that the answer is incomplete. Callers
            # parsing JSON will just see a parse failure and silently fall back.
            if choice.get("finish_reason") == "length":
                logger.warning(
                    "DeepSeek response hit the max_tokens ceiling and was "
                    "truncated — downstream JSON parsing will fail and the "
                    "caller will fall back. Raise max_tokens or shrink the prompt."
                )

            return choice.get("message", {}).get("content")
        except Exception as exc:
            logger.error("DeepSeek API call failed: %s", exc)
            return None
