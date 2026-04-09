from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import List, Optional
from app.core.db import SessionLocal
from app.core.models import ResourceItem
from app.core.config import settings
import uuid
import secrets

router = APIRouter()
security = HTTPBasic()


def _admin_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    header = request.headers.get("x-admin-token")
    if settings.ADMIN_TOKEN:
        if header and secrets.compare_digest(header, settings.ADMIN_TOKEN):
            return True
    if settings.ADMIN_USER and settings.ADMIN_PASSWORD:
        if credentials:
            valid_user = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
            valid_pass = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
            if valid_user and valid_pass:
                return True
    raise HTTPException(status_code=401, detail="Unauthorized")


class ResourceItemIn(BaseModel):
    category: str
    title: str
    description: Optional[str] = None
    href: str
    sort_order: int = 0
    is_active: bool = True


@router.get("/")
def list_resources():
    """Public: return all active resource items grouped by category."""
    db = SessionLocal()
    try:
        items = (
            db.query(ResourceItem)
            .filter(ResourceItem.is_active == True)
            .order_by(ResourceItem.category, ResourceItem.sort_order, ResourceItem.created_at)
            .all()
        )
        grouped: dict = {}
        for item in items:
            grouped.setdefault(item.category, []).append({
                "id": str(item.id),
                "title": item.title,
                "description": item.description,
                "href": item.href,
                "sort_order": item.sort_order,
            })
        return {"categories": grouped}
    finally:
        db.close()


@router.post("/", dependencies=[Depends(_admin_auth)])
def create_resource(data: ResourceItemIn):
    db = SessionLocal()
    try:
        item = ResourceItem(
            id=uuid.uuid4(),
            category=data.category,
            title=data.title,
            description=data.description,
            href=data.href,
            sort_order=data.sort_order,
            is_active=data.is_active,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return {"id": str(item.id), "status": "created"}
    finally:
        db.close()


@router.put("/{item_id}", dependencies=[Depends(_admin_auth)])
def update_resource(item_id: str, data: ResourceItemIn):
    db = SessionLocal()
    try:
        item = db.query(ResourceItem).filter(ResourceItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        item.category = data.category
        item.title = data.title
        item.description = data.description
        item.href = data.href
        item.sort_order = data.sort_order
        item.is_active = data.is_active
        db.commit()
        return {"id": str(item.id), "status": "updated"}
    finally:
        db.close()


@router.delete("/{item_id}", dependencies=[Depends(_admin_auth)])
def delete_resource(item_id: str):
    db = SessionLocal()
    try:
        item = db.query(ResourceItem).filter(ResourceItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        db.delete(item)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()
