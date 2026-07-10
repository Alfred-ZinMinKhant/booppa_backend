import asyncio
import logging
from typing import Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

class RetryAPIRoute(APIRoute):
    """
    A custom APIRoute that intercepts database connection drops (OperationalError)
    and retries the request up to MAX_RETRIES times with exponential backoff.
    """
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            max_retries = 3
            backoff = 0.5
            
            for attempt in range(1, max_retries + 1):
                try:
                    return await original_route_handler(request)
                except OperationalError as e:
                    if attempt == max_retries:
                        logger.error(f"Database OperationalError on {request.url.path} after {max_retries} attempts.")
                        raise e
                    logger.warning(
                        f"Database connection dropped on {request.url.path}. "
                        f"Retrying ({attempt}/{max_retries}) in {backoff}s..."
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2  # Exponential backoff

        return custom_route_handler
