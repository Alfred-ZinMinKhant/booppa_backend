import logging
from typing import Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)


class DeepSeekProvider:
    def __init__(self, api_key: str | None, model: str = "deepseek-chat") -> None:
        self.api_key = api_key
        self.model = model

    async def call_chat(self, messages: List[Dict]) -> Optional[str]:
        if not self.api_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                        "max_tokens": 1400,
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
            return data.get("choices", [{}])[0].get("message", {}).get("content")
        except Exception as exc:
            logger.error("DeepSeek API call failed: %s", exc)
            return None
