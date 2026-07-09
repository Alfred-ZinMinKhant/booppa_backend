from fastapi import APIRouter, Request, HTTPException
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import Report, User
from app.services.blockchain import BlockchainService
from app.services.pdf_service import PDFService
from app.services.booppa_ai_service import BooppaAIService
from app.services.storage import S3Service

from app.services.fulfillment.helpers import (
    _log_purchase_activity,
    _apply_subscription_score_lever,

    _create_stub_report,
    _alert_payment_fulfillment_issue,
    _maybe_fire_cover_sheet,
    _fire_strategy_6,
)
from app.services.fulfillment.single_products import _defer_rfp_to_intake

from app.services.email_service import EmailService
from app.billing.enforcement import enforce_tier
from app.core.models_v10 import Referral
from datetime import datetime, timedelta, timezone
import stripe
import logging
import json
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

router = APIRouter()


RFP_PRODUCT_TYPES = {"rfp_express", "rfp_complete"}
# Single-document notarization is one-time (pay-per-doc, grants a credit balance).
# The 10/50 batch tiers are now subscriptions (monthly quota) — see
# SUBSCRIPTION_PRODUCT_TYPES + ENTERPRISE_NOTARIZATION_LIMITS.
NOTARIZATION_PRODUCT_TYPES = {
    "compliance_notarization_1",
    "notarization_addon_1",
}
NOTARIZATION_CREDIT_AMOUNTS = {
    "compliance_notarization_1": 1,
    "notarization_addon_1": 1,
}
VENDOR_PROOF_PRODUCT_TYPES = {"vendor_proof"}
PDPA_PRODUCT_TYPES = {"pdpa_quick_scan", "pdpa_snapshot"}
SUBSCRIPTION_PRODUCT_TYPES = {
    "vendor_active_monthly",
    "vendor_active_annual",
    "pdpa_monitor_monthly",
    "pdpa_monitor_annual",
    "enterprise_monthly",
    "enterprise_pro_monthly",
    "standard_suite_monthly",
    "pro_suite_monthly",
    "evaluate_suppliers_monthly",
    "verify_supplier_evidence_monthly",
    "compliance_evidence_monthly",
    "tender_intelligence_monthly",
    "tender_intelligence_annual",
    "vendor_pro_monthly",
    "vendor_pro_annual",
    # Buyer ladder
    "buyer_starter_monthly",
    "buyer_starter_annual",
    "buyer_pro_monthly",
    "buyer_pro_annual",
    "buyer_enterprise_monthly",
    "buyer_enterprise_annual",
    # Batch notarization tiers are recurring monthly allowances.
    "compliance_notarization_10",
    "compliance_notarization_50",
    # CSP Compliance Pack recurring tiers (one-time grant handled separately).
    "csp_pack_monthly",
    "csp_monitoring_monthly",
}

# CSP one-time pack purchase — grants lifetime pack access (no recurring billing).
CSP_ONETIME_PRODUCT_TYPES = {"csp_pack_onetime"}

# Bundle → component mapping.
# Each bundle fans out to multiple fulfillment tasks.
# notarization_count = how many notarization tasks to queue (each for one document credit).
BUNDLE_COMPONENTS = {
    "vendor_trust_pack": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": None,
    },
    "rfp_accelerator": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 2,
        "rfp": "rfp_express",
    },
    "enterprise_bid_kit": {
        "vendor_proof": True,
        "pdpa": True,
        "notarization_count": 7,  # 2 from Trust Pack + 5 additional
        "rfp": "rfp_complete",
    },
    "compliance_evidence_pack": {
        "vendor_proof": False,
        "pdpa": True,
        "notarization_count": 1,
        "rfp": "rfp_complete",
        "cover_sheet": True,  # triggers cover sheet generation with 300s delay
    },
}

# Grace window after which the Compliance Evidence Pack cover sheet fires with
# PDPA + RFP only, when the buyer never completed the BCEP evidence-pack intake
# (so the 7-doc pack never reaches status="ready"). Keeps a buyer from being
# left without any cover sheet. See `_maybe_fire_cover_sheet`.
_COVER_SHEET_BCEP_GRACE_DAYS = 7


# Subscription tier → VerifyRecord.verification_level mapping.
# Paid plans elevate the compliance multiplier (BASIC 1.0× → STANDARD 1.1× →
# PREMIUM 1.3× → GOVERNMENT 1.5×, see scoring.py:92). The mapping is conservative
# on purpose — enterprise_pro is the only plan that grants GOVERNMENT tier.
_PLAN_TO_VERIFICATION_LEVEL = {
    "vendor_active": "STANDARD",
    "pdpa_monitor": "STANDARD",
    "evaluate_suppliers": "STANDARD",
    "standard_suite": "STANDARD",
    "tender_intelligence": "STANDARD",
    "vendor_pro": "STANDARD",
    "enterprise": "PREMIUM",
    "pro_suite": "PREMIUM",
    "verify_supplier_evidence": "PREMIUM",
    "compliance_evidence": "PREMIUM",
    "enterprise_pro": "GOVERNMENT",
    # Buyer ladder — mirrors evaluate_suppliers / verify_supplier_evidence.
    # Note: buyer-side plans don't elevate the holder's own vendor verification
    # (most holders are buyers, not vendors) but the mapping is needed so
    # the score-lever code path is a no-op rather than a KeyError.
    "buyer_starter": "STANDARD",
    "buyer_pro": "STANDARD",
    "buyer_enterprise": "PREMIUM",
}
_LEVEL_RANK = {"BASIC": 0, "STANDARD": 1, "PREMIUM": 2, "GOVERNMENT": 3}




                    features = [
                        _feature(
                            "MAS TRM — all 13 domains + baseline PDF",
                            "We've initialised all 13 MAS Technology Risk Management control domains for your "
                            "organisation and are emailing you a baseline assessment PDF shortly. Review and "
                            "work each domain in your TRM workspace.",
                            "Open TRM workspace", "https://www.booppa.io/vendor/trm",
                        ),
                        _feature(
                            "AI gap analysis (DeepSeek)",
                            "Run an AI-assisted gap analysis on any TRM domain — describe your current controls and "
                            "get a gap narrative, risk rating, and compliance status.",
                            "Run a gap analysis", "https://www.booppa.io/vendor/trm",
                        ),
                        _feature(
                            f"{notar} notarizations / month",
                            f"Your plan includes {notar} blockchain document notarizations every month. Upload any "
                            "compliance document to anchor a tamper-proof SHA-256 proof.",
                            "Notarize a document", "https://www.booppa.io/notarization",
                        ),
                        _feature(
                            "RESTful API + webhooks",
                            "Programmatic access to your compliance data. Create an API key and configure webhooks "
                            "to push events into your own systems.",
                            "Create an API key", "https://www.booppa.io/vendor/api-keys",
                        ),
                    ]
                    if is_pro:
                        features += [
                            _feature(
                                "SSO — SAML 2.0 + OIDC",
                                "Connect your identity provider so your team signs in with corporate credentials.",
                                "Configure SSO", "https://www.booppa.io/vendor/sso",
                            ),
                            _feature(
                                "White-label reports",
                                "Your reports and evidence packs now carry your own branding instead of Booppa's.",
                                "Manage branding", "https://www.booppa.io/settings",
                            ),
                            _feature(
                                "Multi-subsidiary management",
                                "Manage compliance across multiple legal entities from one account, each with its "
                                "own evidence and controls.",
                                "Manage subsidiaries", "https://www.booppa.io/vendor/subsidiaries",
                            ),
                        ]

                    onboarding_html = f"""
                    <html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
                    <div style="max-width:600px;margin:0 auto;">
                      <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
                        <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
                        <h1 style="margin:0;color:#10b981;font-size:22px;">{suite_label} — you're all set</h1>
                      </div>
                      <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
                        <p style="color:#cbd5e1;line-height:1.6;margin:0 0 18px;">
                          Your <strong>{suite_label}</strong> subscription is now active. Here's everything it unlocks and where to start:
                        </p>
                        <table style="width:100%;border-collapse:collapse;">{''.join(features)}</table>
                        <div style="text-align:center;margin:26px 0 6px;">
                          <a href="https://www.booppa.io/vendor/dashboard" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
                        </div>
                        <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
                      </div>
                    </div></body></html>"""

                    sent_ob = await EmailService().send_html_email(
                        to_email=customer_email,
                        subject=f"Welcome to {suite_label} — here's everything included",
                        body_html=onboarding_html,
                    )
                    if not sent_ob:
                        logger.error(
                            f"[Subscription] Suite onboarding email rejected by provider "
                            f"for {customer_email} ({new_plan})"
                        )
                    else:
                        logger.info(
                            f"[Subscription] Sent {suite_label} onboarding email to {customer_email}"
                        )
                except Exception as ob_err:
                    logger.warning(
                        f"[Subscription] Suite onboarding email failed for {customer_email}: {ob_err}"
                    )

        elif new_plan in ("buyer_starter", "buyer_pro", "buyer_enterprise") and customer_email:
            # Buyer-tier onboarding email — itemise the due-diligence features
            # the tier unlocks with a direct CTA each. Only ships features that
            # are actually wired (see HANDOFF audit); marketed-but-unbuilt items
            # (custom risk weights, native Slack/Teams, custom frameworks) are
            # intentionally omitted so the welcome email has no dead links.
            try:
                from app.billing.enforcement import scan_limit_for, max_seats_for
                from app.core.models_v8 import ENTERPRISE_NOTARIZATION_LIMITS

                labels = {
                    "buyer_starter": "Buyer Essentials",
                    "buyer_pro": "Buyer Professional",
                    "buyer_enterprise": "Buyer Enterprise",
                }
                buyer_label = labels[new_plan]
                quick = scan_limit_for(new_plan, "QUICK") or 0
                deep = scan_limit_for(new_plan, "DEEP") or 0
                evidence = scan_limit_for(new_plan, "EVIDENCE") or 0
                notar = ENTERPRISE_NOTARIZATION_LIMITS.get(new_plan, 1)
                seats = max_seats_for(new_plan)
                seats_txt = "Unlimited seats with RBAC" if seats is None else (
                    f"{seats} seats with role-based access" if seats > 1 else "1 user seat"
                )
                dash = "https://www.booppa.io/procurement/dashboard"

                def _bf(title: str, desc: str, cta: str, url: str) -> str:
                    return f"""
                    <tr><td style="padding:14px 0;border-bottom:1px solid #1e293b;">
                      <p style="margin:0 0 4px;color:#fff;font-weight:bold;font-size:15px;">{title}</p>
                      <p style="margin:0 0 10px;color:#94a3b8;font-size:13px;line-height:1.5;">{desc}</p>
                      <a href="{url}" style="color:#10b981;font-weight:bold;text-decoration:none;font-size:13px;">{cta} &rarr;</a>
                    </td></tr>"""

                feats = []
                scan_line = f"Quick Scan on {quick} vendors/month (ACRA + MAS watchlist + PDPA flag)"
                if deep:
                    scan_line = (f"{quick} Quick Scans + {deep} Deep Scans/month "
                                 "(11-dimension PDPA + certifications + financial risk)")
                feats.append(_bf("Vendor scans", scan_line, "Start scanning", dash))
                if evidence:
                    feats.append(_bf(
                        f"Evidence Scan — {evidence} vendors/month",
                        "Level-3 blockchain evidence retrieval + complete vendor dossier.",
                        "Run an Evidence Scan", dash,
                    ))
                feats.append(_bf(
                    "Compliance dashboard",
                    "Traffic-light status across every vendor you scan, with automatic alerts when one enters critical status.",
                    "Open dashboard", dash,
                ))
                feats.append(_bf(
                    "Vendor directory",
                    "Browse the vendor network with advanced filters (sector, size, certifications).",
                    "Browse vendors", dash,
                ))
                if deep:
                    feats.append(_bf(
                        "Comparison engine + drift tracking",
                        "Compare vendors side-by-side across Deep Scan parameters, with automatic change alerts as their compliance drifts.",
                        "Compare vendors", "https://www.booppa.io/compare",
                    ))
                export_desc = ("CSV export of scan results for tender spreadsheets."
                               if not deep else
                               "CSV export plus exportable Deep Scan PDF reports for shortlists and tender minutes.")
                feats.append(_bf("Exports", export_desc, "Export results", dash))
                if new_plan == "buyer_enterprise":
                    feats.append(_bf(
                        "Multi-subsidiary management",
                        "Manage due diligence across multiple BUs / legal entities from one account.",
                        "Manage subsidiaries", "https://www.booppa.io/vendor/subsidiaries",
                    ))
                    feats.append(_bf(
                        "White-label reports",
                        "Board- and regulator-ready reports carrying your own branding.",
                        "Manage branding", "https://www.booppa.io/settings",
                    ))
                    feats.append(_bf(
                        "RESTful API + webhooks",
                        "Programmatic access for ERP integration. Create an API key and configure webhooks.",
                        "Create an API key", "https://www.booppa.io/vendor/api-keys",
                    ))
                elif new_plan == "buyer_pro":
                    feats.append(_bf(
                        "Webhook integrations",
                        "Push scan + drift events into your own systems (email, or any incoming-webhook URL such as Slack or Teams).",
                        "Configure webhooks", "https://www.booppa.io/vendor/api-keys",
                    ))
                feats.append(_bf(
                    f"{notar} notarization{'s' if notar != 1 else ''} / month",
                    "Anchor any compliance document on the blockchain with a tamper-proof SHA-256 proof.",
                    "Notarize a document", "https://www.booppa.io/notarization",
                ))

                onboarding_html = f"""
                <html><body style="font-family:Arial,sans-serif;background:#0a0f1e;color:#e5e5e5;padding:32px;">
                <div style="max-width:600px;margin:0 auto;">
                  <div style="background:#0f172a;padding:24px 28px;border-radius:12px 12px 0 0;">
                    <p style="margin:0 0 4px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-size:11px;">BOOPPA · Subscription active</p>
                    <h1 style="margin:0;color:#10b981;font-size:22px;">{buyer_label} — you're all set</h1>
                  </div>
                  <div style="background:#0d1424;padding:28px;border:1px solid #1e293b;border-top:none;border-radius:0 0 12px 12px;">
                    <p style="color:#cbd5e1;line-height:1.6;margin:0 0 8px;">
                      Your <strong>{buyer_label}</strong> subscription is now active — {seats_txt}. Here's everything it unlocks and where to start:
                    </p>
                    <table style="width:100%;border-collapse:collapse;">{''.join(feats)}</table>
                    <div style="text-align:center;margin:26px 0 6px;">
                      <a href="{dash}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:bold;">Go to your dashboard &rarr;</a>
                    </div>
                    <p style="color:#475569;font-size:11px;text-align:center;margin-top:20px;">Questions? Reply to this email or visit booppa.io/support.</p>
                  </div>
                </div></body></html>"""

                sent_ob = await EmailService().send_html_email(
                    to_email=customer_email,
                    subject=f"Welcome to {buyer_label} — here's everything included",
                    body_html=onboarding_html,
                )
                if not sent_ob:
                    logger.error(
                        f"[Subscription] Buyer onboarding email rejected by provider "
                        f"for {customer_email} ({new_plan})"
                    )
                else:
                    logger.info(
                        f"[Subscription] Sent {buyer_label} onboarding email to {customer_email}"
                    )
            except Exception as ob_err:
                logger.warning(
                    f"[Subscription] Buyer onboarding email failed for {customer_email}: {ob_err}"
                )

        # Record activation in ActivityLog so Engagement + Recency move.
        _log_purchase_activity(
            db,
            user.id,
            activity_type="SUBSCRIPTION_ACTIVATED",
            description=f"Subscription activated: {new_plan}",
            extra={"product_type": product_type, "plan": new_plan},
        )

        # Elevate verification level for the duration of this subscription so
        # the trust-score compliance multiplier reflects the paid tier.
        try:
            _apply_subscription_score_lever(db, user.id, new_plan)
        except Exception as lever_err:
            logger.warning(
                f"[Subscription] Score lever apply failed for {customer_email}: {lever_err}"
            )

    except Exception as e:
        logger.error(f"[Subscription] Activation error for {product_type}: {e}")
        db.rollback()
    finally:
        db.close()


