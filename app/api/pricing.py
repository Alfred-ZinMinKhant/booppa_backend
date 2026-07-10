from app.core.route_classes import RetryAPIRoute
from fastapi import APIRouter, HTTPException
from app.services.pricing import get_all_products, get_product

router = APIRouter(route_class=RetryAPIRoute)


@router.get("/products")
def list_products(type: str | None = None) -> dict:
    """Return all product SKUs from the single source of truth in pricing.py.

    Optional `type` filter: one-time | bundle | subscription.
    """
    items = get_all_products()
    if type:
        items = [p for p in items if p.get("type") == type]
    return {"total": len(items), "items": items}


@router.get("/products/{slug}")
def get_one(slug: str) -> dict:
    product = get_product(slug)
    if not product:
        raise HTTPException(status_code=404, detail=f"No product with slug {slug}")
    return product
