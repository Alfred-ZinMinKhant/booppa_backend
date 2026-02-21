import hashlib
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.models import User, VendorScore, VerifyRecord, Proof, ProofView
from app.core.auth import create_access_token, create_refresh_token, verify_access_token, verify_refresh_token, verify_password
from app.services.scoring import VendorScoreEngine

router = APIRouter()

# Models
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class RefreshRequest(BaseModel):
    refreshToken: str

class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: Optional[str] = "VENDOR"
    avatar: Optional[str] = None

# Auth Endpoints for V6 Frontend
@router.post("/auth/login", tags=["bridge-auth"])
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    """Bridge layer auth for Next.js frontend (JSON body)"""
    user = db.query(User).filter(User.email == req.email).first()
    
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
        
    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    
    return {
        "token": access_token,
        "refreshToken": refresh_token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "name": user.full_name or user.email.split("@")[0].title(),
            "role": user.role,
            "avatar": None
        }
    }

@router.post("/auth/logout", tags=["bridge-auth"])
async def logout():
    return {"message": "Logout successful"}

@router.post("/auth/refresh", tags=["bridge-auth"])
async def refresh(req: RefreshRequest):
    payload = verify_refresh_token(req.refreshToken)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
        
    access_token = create_access_token(data={"sub": payload["sub"]})
    new_refresh_token = create_refresh_token(data={"sub": payload["sub"]})
    
    return {
        "token": access_token,
        "refreshToken": new_refresh_token
    }

@router.get("/auth/me", tags=["bridge-auth"])
async def get_me(token: str, db: Session = Depends(get_db)):
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
        
    user = db.query(User).filter(User.email == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.full_name or user.email.split("@")[0].title(),
        "role": user.role,
        "avatar": None
    }

def get_current_user_from_header(token: str, db: Session = Depends(get_db)):
    # Standard bearer token auth for bridge routes
    if token.startswith("Bearer "):
        token = token.split(" ")[1]
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.email == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

# Dashboard Endpoint
@router.get("/dashboard", tags=["bridge-dashboard"])
async def dashboard_overview(token: str, db: Session = Depends(get_db)):
    user = get_current_user_from_header(token, db)
    vendor_id = str(user.id)
    
    proof_count = db.query(Proof).join(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).count()
    
    verify_ids = [v[0] for v in db.query(VerifyRecord.id).filter(VerifyRecord.vendor_id == vendor_id).all()]
    if verify_ids:
        recent_views = db.query(ProofView).filter(ProofView.verify_id.in_(verify_ids)).order_by(ProofView.created_at.desc()).limit(10).all()
        active_users = len(set(v.domain for v in recent_views if v.domain))
    else:
        recent_views = []
        active_users = 0
        
    recent_activity = []
    for log in recent_views:
        recent_activity.append({
            "id": str(log.id),
            "type": "Enterprise Verification",
            "description": f"Domain {log.domain or 'unknown'} viewed your compliance proof",
            "timestamp": log.created_at.isoformat()
        })
        
    return {
        "stats": {
            "totalUsers": proof_count,
            "activeUsers": active_users,
            "revenue": proof_count * 50
        },
        "recentActivity": recent_activity
    }

# Scoring Endpoint
@router.get("/enterprise/score", tags=["bridge-scoring"])
async def get_score(token: str, db: Session = Depends(get_db)):
    user = get_current_user_from_header(token, db)
    vendor_id = str(user.id)
    
    # Calculate fresh score
    score_record = VendorScoreEngine.update_vendor_score(db, vendor_id)
    
    categories = [
        {"name": "Compliance", "score": score_record.compliance_score},
        {"name": "Visibility", "score": score_record.visibility_score},
        {"name": "Engagement", "score": score_record.engagement_score},
        {"name": "Procurement Interest", "score": score_record.procurement_interest_score},
    ]
    
    available_modules = [
        {"id": "module-dpia", "name": "DPIA Assessment Kit", "impact": 12, "price": 299, "currency": "SGD"},
        {"id": "module-breach", "name": "Breach Response Playbook", "impact": 18, "price": 499, "currency": "SGD"},
        {"id": "module-monitoring", "name": "Continuous Monitoring", "impact": 8, "price": 199, "currency": "SGD"}
    ]
    
    return {
        "overallScore": score_record.total_score,
        "categories": categories,
        "availableModules": available_modules
    }

# Verify Completion Endpoint
@router.post("/verify/{token}/complete", tags=["bridge-verify"])
async def complete_verification(token: str, db: Session = Depends(get_db)):
    evidence = db.query(Proof).filter(
        (str(Proof.id) == token) | (Proof.hash_value == token)
    ).first()
    
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found")
        
    # Example updating status
    verify_record = db.query(VerifyRecord).filter(VerifyRecord.id == evidence.verify_id).first()
    if verify_record:
        verify_record.last_refreshed_at = datetime.utcnow()
        db.commit()
    
    # In a full implementation, this should trigger a WebSocket event.
    # The socketio server would be imported here to emit.
    # from app.main import emit_verify_completed
    # emit_verify_completed(str(verify_record.vendor_id), str(evidence.id))
    
    return {
        "status": "completed",
        "message": "Verification completed successfully",
        "evidence_id": str(evidence.id)
    }
