import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.models import (
    VendorScore, VerifyRecord, Proof, ProofView, 
    EnterpriseProfile, ActivityLog, LifecycleStatus, OrganizationType,
    GovernanceRecord, EnterpriseLead, LeadPriority
)

logger = logging.getLogger(__name__)

class VendorScoreEngine:
    WEIGHTS = {
        "COMPLIANCE": 0.30,
        "VISIBILITY": 0.20,
        "ENGAGEMENT": 0.20,
        "RECENCY": 0.15,
        "PROCUREMENT_INTEREST": 0.15
    }

    @classmethod
    def calculate_total(cls, components: dict) -> int:
        return round(
            components.get("complianceScore", 0) * cls.WEIGHTS["COMPLIANCE"] +
            components.get("visibilityScore", 0) * cls.WEIGHTS["VISIBILITY"] +
            components.get("engagementScore", 0) * cls.WEIGHTS["ENGAGEMENT"] +
            components.get("recencyScore", 0) * cls.WEIGHTS["RECENCY"] +
            components.get("procurementInterestScore", 0) * cls.WEIGHTS["PROCUREMENT_INTEREST"]
        )

    @classmethod
    def calculate_compliance_score(cls, db: Session, vendor_id: str) -> int:
        verifications = db.query(VerifyRecord).filter(
            VerifyRecord.vendor_id == vendor_id,
            VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE
        ).all()
        
        if not verifications:
            return 0
            
        total = 0
        weight_sum = 0
        for v in verifications:
            lvl = v.verification_level.value if v.verification_level else "BASIC"
            level_weight = 1.5 if lvl == "GOVERNMENT" else 1.3 if lvl == "PREMIUM" else 1.1 if lvl == "STANDARD" else 1.0
            total += (v.compliance_score or 0) * level_weight
            weight_sum += level_weight
            
        return min(round(total / weight_sum) if weight_sum > 0 else 0, 100)

    @classmethod
    def calculate_visibility_score(cls, db: Session, vendor_id: str) -> int:
        verify_ids = [v[0] for v in db.query(VerifyRecord.id).filter(VerifyRecord.vendor_id == vendor_id).all()]
        if not verify_ids:
            return 0
            
        unique_domains = db.query(ProofView.domain).filter(
            ProofView.verify_id.in_(verify_ids),
            ProofView.domain.isnot(None)
        ).distinct().count()
        
        base_score = min(unique_domains * 5, 50)
        gov_domains = db.query(ProofView).filter(
            ProofView.verify_id.in_(verify_ids),
            ProofView.domain.like("%.gov.sg")
        ).count()
        gov_bonus = min(gov_domains * 3, 30)
        
        total_views = db.query(ProofView).filter(ProofView.verify_id.in_(verify_ids)).count()
        views_bonus = min((total_views // 10) * 2, 20)
        
        return min(base_score + gov_bonus + views_bonus, 100)

    @classmethod
    def calculate_engagement_score(cls, db: Session, vendor_id: str) -> int:
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        proofs = db.query(Proof).join(VerifyRecord).filter(VerifyRecord.vendor_id == vendor_id).count()
        recent_activity = db.query(ActivityLog).filter(
            ActivityLog.user_id == vendor_id,
            ActivityLog.created_at >= thirty_days_ago
        ).count()
        
        score = min(proofs * 3, 30) + min(recent_activity * 2, 30)
        return min(score, 100)

    @classmethod
    def calculate_recency_score(cls, db: Session, vendor_id: str) -> int:
        last_activity = db.query(ActivityLog).filter(
            ActivityLog.user_id == vendor_id
        ).order_by(ActivityLog.created_at.desc()).first()
        
        if not last_activity:
            return 0
            
        hours_since = (datetime.utcnow() - last_activity.created_at).total_seconds() / 3600
        if hours_since <= 24: return 100
        if hours_since <= 72: return 80
        if hours_since <= 168: return 60
        if hours_since <= 720: return 40
        if hours_since <= 2160: return 20
        return 0

    @classmethod
    def calculate_procurement_interest_score(cls, db: Session, vendor_id: str) -> int:
        verify_ids = [v[0] for v in db.query(VerifyRecord.id).filter(VerifyRecord.vendor_id == vendor_id).all()]
        if not verify_ids:
            return 0
            
        domains = [d[0] for d in db.query(ProofView.domain).filter(
            ProofView.verify_id.in_(verify_ids),
            ProofView.domain.isnot(None)
        ).distinct().all()]
        
        if not domains:
            return 0
            
        enterprises = db.query(EnterpriseProfile).filter(EnterpriseProfile.domain.in_(domains)).all()
        if not enterprises:
            return 0
            
        avg_intent = sum(e.procurement_intent_score or 0 for e in enterprises) / len(enterprises)
        active_procurements = sum(1 for e in enterprises if e.active_procurement)
        procurement_bonus = min(active_procurements * 10, 30)
        
        return min(round(avg_intent) + procurement_bonus, 100)

    @classmethod
    def update_vendor_score(cls, db: Session, vendor_id: str, correlation_id: str = None) -> VendorScore:
        logger.info(f"Updating vendor score for vendor={vendor_id}")
        
        components = {
            "complianceScore": cls.calculate_compliance_score(db, vendor_id),
            "visibilityScore": cls.calculate_visibility_score(db, vendor_id),
            "engagementScore": cls.calculate_engagement_score(db, vendor_id),
            "recencyScore": cls.calculate_recency_score(db, vendor_id),
            "procurementInterestScore": cls.calculate_procurement_interest_score(db, vendor_id),
        }
        total_score = cls.calculate_total(components)
        
        score_record = db.query(VendorScore).filter(VendorScore.vendor_id == vendor_id).first()
        if not score_record:
            score_record = VendorScore(
                vendor_id=vendor_id,
                compliance_score=components["complianceScore"],
                visibility_score=components["visibilityScore"],
                engagement_score=components["engagementScore"],
                recency_score=components["recencyScore"],
                procurement_interest_score=components["procurementInterestScore"],
                total_score=total_score
            )
            db.add(score_record)
        else:
            score_record.compliance_score = components["complianceScore"]
            score_record.visibility_score = components["visibilityScore"]
            score_record.engagement_score = components["engagementScore"]
            score_record.recency_score = components["recencyScore"]
            score_record.procurement_interest_score = components["procurementInterestScore"]
            score_record.total_score = total_score
            score_record.last_calculation = datetime.utcnow()
            score_record.calculation_count += 1
            
        # Add governance record
        gov_record = GovernanceRecord(
            event_type='SCORE_UPDATED',
            entity_type='VENDOR',
            entity_id=str(vendor_id),
            correlation_id=correlation_id or f"score_{int(datetime.utcnow().timestamp())}",
            metadata_json={"components": components, "totalScore": total_score}
        )
        db.add(gov_record)
        db.commit()
        db.refresh(score_record)
        return score_record

class EnterpriseBehavioralEngine:
    @classmethod
    def process_enterprise_view(cls, db: Session, domain: str, proof_view_id: str, correlation_id: str = None):
        profile = db.query(EnterpriseProfile).filter(EnterpriseProfile.domain == domain).first()
        if not profile:
            is_gov = domain.endswith('.gov.sg')
            org_type = OrganizationType.GOVERNMENT if is_gov else OrganizationType.UNKNOWN
            profile = EnterpriseProfile(domain=domain, organization_type=org_type, is_government=is_gov)
            db.add(profile)
            db.commit()
            db.refresh(profile)
            
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        
        total_views = db.query(ProofView).filter(ProofView.domain == domain).count()
        unique_vendors = db.query(ProofView.verify_id).filter(ProofView.domain == domain).distinct().count()
        recent_views = db.query(ProofView).filter(
            ProofView.domain == domain,
            ProofView.created_at >= thirty_days_ago
        ).count()
        
        visit_freq = max(round(recent_views / 30), 1)
        
        profile.total_views = total_views
        profile.unique_vendors_viewed = unique_vendors
        profile.visit_frequency = visit_freq
        profile.last_activity = datetime.utcnow()
        
        # Calculate behavioral score
        score = min(visit_freq * 10, 40) + min(unique_vendors * 5, 30) + min((total_views // 10) * 2, 20)
        if profile.is_government:
            score += 10
        profile.behavioral_score = min(score, 100)
        
        # Calculate intent score
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        views_7d = db.query(ProofView).filter(ProofView.domain == domain, ProofView.created_at >= seven_days_ago).count()
        vendors_7d = db.query(ProofView.verify_id).filter(ProofView.domain == domain, ProofView.created_at >= seven_days_ago).distinct().count()
        
        intent = 0
        if recent_views > 0:
            acceleration = views_7d / (recent_views / 4)
            intent += min(acceleration * 20, 40)
        intent += min(vendors_7d * 10, 30)
        intent += min(views_7d * 2, 30)
        profile.procurement_intent_score = min(int(intent), 100)
        
        db.commit()
        
        # Detect procurement window
        cls.detect_procurement_window(db, profile, correlation_id)
        
        return profile

    @classmethod
    def detect_procurement_window(cls, db: Session, profile: EnterpriseProfile, correlation_id: str = None):
        forty_eight_hours_ago = datetime.utcnow() - timedelta(hours=48)
        recent_views = db.query(ProofView).filter(
            ProofView.domain == profile.domain,
            ProofView.created_at >= forty_eight_hours_ago
        ).all()
        
        if len(recent_views) < 3:
            if profile.active_procurement:
                profile.active_procurement = False
                profile.procurement_window_end = datetime.utcnow()
                db.commit()
            return

        # Basic vendors processing and window detection (simplified for Python)
        vendors = set(v.verify_id for v in recent_views) # Ideally would group by vendor_id via VerifyRecord
        
        if len(vendors) >= 2:
            if not profile.active_procurement:
                profile.active_procurement = True
                profile.procurement_window_start = datetime.utcnow()
                db.add(GovernanceRecord(
                    event_type='PROCUREMENT_WINDOW',
                    entity_type='ENTERPRISE',
                    entity_id=str(profile.id),
                    correlation_id=correlation_id or f"window_{int(datetime.utcnow().timestamp())}",
                    metadata_json={"vendors": len(vendors), "views": len(recent_views), "domain": profile.domain}
                ))
                db.commit()
                # In full V6, this triggers a BullMQ queue to re-evaluate vendors
        elif profile.active_procurement:
            profile.active_procurement = False
            profile.procurement_window_end = datetime.utcnow()
            db.commit()
