"""
OneMap Service
==============
Resolves Singapore postal codes to geographical coordinates and planning areas using the OneMap API.
"""
from __future__ import annotations

import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)

ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

async def fetch_onemap_location(postal_code: str) -> dict[str, Any]:
    """Fetch physical location details from OneMap API using postal code."""
    if not postal_code:
        return {"checked": False, "found": False}
        
    result = {
        "checked": True,
        "found": False,
        "postal_code": postal_code,
        "latitude": None,
        "longitude": None,
        "planning_area": None,
        "address": None,
        "error": None
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                ONEMAP_SEARCH_URL,
                params={
                    "searchVal": postal_code,
                    "returnGeom": "Y",
                    "getAddrDetails": "Y",
                    "pageNum": "1"
                }
            )
            resp.raise_for_status()
            data = resp.json()
            
            if data and data.get("found", 0) > 0 and data.get("results"):
                top_match = data["results"][0]
                result["found"] = True
                result["latitude"] = top_match.get("LATITUDE")
                result["longitude"] = top_match.get("LONGITUDE")
                result["planning_area"] = top_match.get("PLANNING_AREA")
                result["address"] = top_match.get("ADDRESS")
                
    except Exception as e:
        logger.warning(f"Failed to fetch OneMap location for {postal_code}: {e}")
        result["error"] = str(e)
        
    return result
