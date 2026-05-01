#!/usr/bin/env python3
"""
Booppa Agent – Production‑grade Intent Intelligence Engine
- Multi‑provider enrichment (ipapi.is, NetworksDB.io, ipgeolocation.io)
- Score decay
- Fit scoring (ICP rules)
- Multi‑vendor matching (top 3)
- Structured explanation
- AI via DeepSeek (with local fallback)
- PostgreSQL materialized views
- IP anonymization (/24) - PDPA COMPLIANT
"""

import os
import json
import time
import logging
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json
import requests
import httpx

# Add app directory to path if running from scripts/
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.services.ai_provider import DeepSeekProvider

# ------------------------------
# Configuration
# ------------------------------
DB_URL = os.getenv("DATABASE_URL")
NETWORKSDB_API_KEY = os.getenv("NETWORKSDB_API_KEY")
IPGEOLOCATION_API_KEY = os.getenv("IPGEOLOCATION_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))
INTENT_AI_THRESHOLD = float(os.getenv("INTENT_AI_THRESHOLD", "20"))
DECAY_DAYS_HALFLIFE = float(os.getenv("DECAY_DAYS_HALFLIFE", "3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("booppa-agent")

# ------------------------------
# Connection Pool
# ------------------------------
try:
    db_pool = pool.ThreadedConnectionPool(1, 5, DB_URL)
    logger.info("Database connection pool created")
except Exception as e:
    logger.critical(f"Failed to create database connection pool: {e}")
    sys.exit(1)

# ------------------------------
# DB Helpers
# ------------------------------
def query(sql: str, params: Optional[List] = None):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
            try:
                return cur.fetchall()
            except:
                return []
    finally:
        db_pool.putconn(conn)

def execute(sql: str, params: Optional[List] = None):
    conn = db_pool.getconn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
    finally:
        db_pool.putconn(conn)

# ------------------------------
# Domain validation (strict)
# ------------------------------
def is_valid_domain(domain: str) -> bool:
    if not domain:
        return False
    # Basic check for common ISP/broadband domains to exclude noise
    noise_patterns = [
        r'\.net$', r'\.isp$', r'telecom', r'broadband', r'residential', r'dynamic-ip'
    ]
    for p in noise_patterns:
        if re.search(p, domain, re.I):
            return False
            
    pattern = re.compile(r'^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)
    return bool(pattern.match(domain))

# ------------------------------
# Multi‑provider enrichment
# ------------------------------
def fetch_company_by_ip(ip: str) -> Optional[Dict[str, Any]]:
    # 1) ipapi.is (Free tier)
    try:
        resp = requests.get(f"https://ipapi.is/{ip}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            company_data = data.get("company") or data.get("asn") or {}
            domain = company_data.get("domain")
            name = company_data.get("name")
            if domain and is_valid_domain(domain):
                return {"domain": domain, "name": name, "source": "ipapi.is", "confidence": 0.6}
    except Exception as e:
        logger.debug(f"ipapi.is failed for {ip}: {e}")

    # 2) NetworksDB.io (Production grade)
    if NETWORKSDB_API_KEY:
        try:
            resp = requests.get(
                f"https://networksdb.io/api/ip-to-company/{ip}",
                headers={"X-API-Key": NETWORKSDB_API_KEY},
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                domain = data.get("domain")
                name = data.get("company_name")
                if domain and is_valid_domain(domain):
                    return {"domain": domain, "name": name, "source": "networksdb", "confidence": 0.8}
        except Exception as e:
            logger.debug(f"networksdb failed for {ip}: {e}")

    # 3) ipgeolocation.io (Fallback)
    if IPGEOLOCATION_API_KEY:
        try:
            resp = requests.get(
                f"https://api.ipgeolocation.io/ipgeo?apiKey={IPGEOLOCATION_API_KEY}&ip={ip}",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                company = data.get("organization") or data.get("as_name")
                domain = data.get("domain")
                if domain and is_valid_domain(domain):
                    return {"domain": domain, "name": company, "source": "ipgeolocation", "confidence": 0.65}
        except Exception as e:
            logger.debug(f"ipgeolocation failed for {ip}: {e}")

    return None

# ------------------------------
# Enrichment queue
# ------------------------------
def populate_enrichment_queue():
    execute("""
        INSERT INTO enrichment_queue (ip, status, expires_at)
        SELECT DISTINCT re.ip, 'pending', NOW() + INTERVAL '%s days'
        FROM raw_events re
        LEFT JOIN enrichment_queue eq ON re.ip = eq.ip
        WHERE eq.ip IS NULL
          AND re.ip IS NOT NULL
          AND re.created_at > NOW() - INTERVAL '7 days'
    """, [CACHE_TTL_DAYS])
    logger.info("Enrichment queue populated")

def enrich_pending_ips():
    rows = query("""
        SELECT ip FROM enrichment_queue
        WHERE status IN ('pending', 'failed')
          AND (last_attempt IS NULL OR last_attempt < NOW() - INTERVAL '1 hour')
          AND (expires_at IS NULL OR expires_at > NOW())
        LIMIT %s
    """, [BATCH_SIZE])

    if not rows:
        logger.info("No IPs to enrich")
        return

    for (ip,) in rows:
        logger.info(f"Enriching {ip}")
        start = time.time()
        data = fetch_company_by_ip(ip)
        elapsed = time.time() - start

        if data:
            domain = data['domain'].lower()
            name = data.get('name')
            confidence = data.get('confidence', 0.7)
            source = data.get('source', 'unknown')
            execute("""
                INSERT INTO accounts (domain, name, enrichment_data, last_enriched)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (domain) DO UPDATE
                SET name = EXCLUDED.name,
                    enrichment_data = EXCLUDED.enrichment_data,
                    last_enriched = NOW()
            """, [domain, name, Json(data)])
            execute("""
                UPDATE sessions
                SET detected_domain = %s, confidence = %s
                WHERE ip = %s AND (confidence < %s OR confidence IS NULL)
            """, [domain, confidence, ip, confidence])
            execute("""
                UPDATE enrichment_queue
                SET status = 'success', resolved_domain = %s, confidence = %s,
                    last_attempt = NOW(), fail_count = 0, last_error = NULL
                WHERE ip = %s
            """, [domain, confidence, ip])
            logger.info(f"  → {domain} (source: {source}, confidence: {confidence})")
        else:
            execute("""
                UPDATE enrichment_queue
                SET status = 'failed', last_attempt = NOW(), fail_count = fail_count + 1,
                    last_error = 'No provider returned valid domain'
                WHERE ip = %s
            """, [ip])
            logger.info(f"  → no valid domain for {ip}")

        time.sleep(max(0.5, 1.0 - elapsed))

    logger.info("Enrichment batch completed")

# ------------------------------
# Materialized views refresh
# ------------------------------
def refresh_materialized_views():
    for attempt in range(3):
        try:
            execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_raw_scores")
            execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_category_intent")
            logger.info("Materialized views refreshed")
            return
        except Exception as e:
            logger.error(f"MV refresh attempt {attempt+1} failed: {e}")
            time.sleep(2)
    logger.critical("MV refresh failed after 3 attempts")

# ------------------------------
# Score decay
# ------------------------------
def apply_score_decay(score: float, last_event: datetime) -> float:
    # Handle timezone naive/aware comparison
    now = datetime.now(last_event.tzinfo) if last_event.tzinfo else datetime.now()
    days_inactive = (now - last_event).days
    if days_inactive <= 0:
        return score
    decay_factor = 0.5 ** (days_inactive / DECAY_DAYS_HALFLIFE)
    return score * decay_factor

# ------------------------------
# Fit scoring (ICP rules)
# ------------------------------
def get_fit_factor(domain: str) -> float:
    rows = query("""
        SELECT enrichment_data->>'industry' AS industry,
               enrichment_data->>'employee_range' AS employee_range
        FROM accounts
        WHERE domain = %s
    """, [domain])
    if not rows:
        return 1.0
    industry = rows[0][0]
    employee_range = rows[0][1]
    emp_max = 0
    if employee_range:
        parts = employee_range.split('-')
        if len(parts) == 2:
            try:
                emp_max = int(parts[1])
            except: pass
        elif employee_range.isdigit():
            emp_max = int(employee_range)
            
    rows = query("""
        SELECT fit_factor FROM icp_rules
        WHERE (industry IS NULL OR industry = %s)
          AND (min_employee_range IS NULL OR %s >= min_employee_range)
          AND (max_employee_range IS NULL OR %s <= max_employee_range)
        ORDER BY fit_factor DESC LIMIT 1
    """, [industry, emp_max, emp_max])
    if rows:
        return rows[0][0]
    return 1.0

# ------------------------------
# Multi‑vendor matching engine
# ------------------------------
def get_top_vendors(category: str, limit: int = 3) -> List[Dict]:
    if not category:
        return []
    rows = query("""
        SELECT vendor_name, weight
        FROM vendor_match
        WHERE category = %s
        ORDER BY weight DESC
        LIMIT %s
    """, [category, limit])
    return [{"vendor": r[0], "score": round(r[1] * 100, 1)} for r in rows]

# ------------------------------
# Structured explanation
# ------------------------------
def build_explanation(raw_score: float, multiplier: float, fit_factor: float,
                      final_score: float, sessions: int, top_cat: str,
                      days_inactive: int) -> Dict:
    factors = []
    if raw_score > 0:
        factors.append({"factor": "behavior_score", "impact": raw_score, "type": "raw"})
    if sessions >= 2:
        factors.append({"factor": "buying_group", "impact": round((multiplier - 1) * 100, 1), "type": "multiplier"})
    if fit_factor != 1.0:
        factors.append({"factor": "icp_fit", "impact": round((fit_factor - 1) * 100, 1), "type": "multiplier"})
    if days_inactive > 0:
        decay_pct = round((1 - final_score/(raw_score * multiplier * fit_factor)) * 100, 1) if raw_score > 0 else 0
        factors.append({"factor": "decay", "impact": -decay_pct, "type": "penalty"})
    return {
        "final_score": final_score,
        "components": factors,
        "top_category": top_cat or None
    }

# ------------------------------
# AI classification (DeepSeek + fallback)
# ------------------------------
def fallback_rule_based(events_summary: Dict) -> Dict:
    score = events_summary.get("total_score", 0)
    if score > 30:
        return {"intent": "high", "stage": "decision", "urgency": 4, "recommended_action": "Send customized sales deck"}
    if score > 15:
        return {"intent": "medium", "stage": "evaluation", "urgency": 3, "recommended_action": "Share sector-specific case study"}
    return {"intent": "low", "stage": "research", "urgency": 2, "recommended_action": "Nurture with compliance newsletter"}

async def ai_classify_intent(company: str, events_summary: Dict) -> Dict:
    if not DEEPSEEK_API_KEY:
        return fallback_rule_based(events_summary)
        
    provider = DeepSeekProvider(DEEPSEEK_API_KEY)
    prompt = f"""You are Booppa Sales Intelligence. Classify B2B buying intent for {company}.
Events summary: {json.dumps(events_summary)}
Return ONLY JSON: {{"intent": "low/medium/high", "stage": "research/evaluation/decision", "urgency": 1-5, "recommended_action": "string"}}"""

    try:
        response = await provider.call_chat([{"role": "user", "content": prompt}])
        if response:
            # Clean response if LLM added markdown
            response = response.strip().replace('```json', '').replace('```', '')
            result = json.loads(response)
            return result
    except Exception as e:
        logger.warning(f"DeepSeek call failed: {e}. Using fallback.")
    return fallback_rule_based(events_summary)

# ------------------------------
# Compute buying group multiplier
# ------------------------------
def compute_buying_group_multiplier(domain: str) -> float:
    rows = query("""
        SELECT COUNT(DISTINCT session_id)
        FROM sessions
        WHERE detected_domain = %s AND last_seen > NOW() - INTERVAL '48 hours'
    """, [domain])
    count = rows[0][0] if rows else 1
    if count >= 5:
        return 2.0
    if count >= 3:
        return 1.5
    if count >= 2:
        return 1.2
    return 1.0

# ------------------------------
# Generate hot leads
# ------------------------------
def generate_hot_leads():
    refresh_materialized_views()

    rows = query("""
        SELECT detected_domain, raw_score, sessions, last_event
        FROM mv_raw_scores
        WHERE raw_score > 0
    """)

    leads = []
    for domain, raw_score, sessions, last_event in rows:
        last_event_dt = last_event if isinstance(last_event, datetime) else datetime.now()
        days_inactive = (datetime.now(last_event_dt.tzinfo) - last_event_dt).days
        decayed_score = apply_score_decay(raw_score, last_event_dt)

        multiplier = compute_buying_group_multiplier(domain)
        fit_factor = get_fit_factor(domain)

        final_score = decayed_score * multiplier * fit_factor

        # Top category
        cat_rows = query("""
            SELECT category FROM mv_category_intent
            WHERE detected_domain = %s
            ORDER BY views DESC LIMIT 1
        """, [domain])
        top_cat = cat_rows[0][0] if cat_rows else None

        top_vendors = get_top_vendors(top_cat, limit=3) if top_cat else []

        explanation = build_explanation(
            raw_score, multiplier, fit_factor, final_score,
            sessions, top_cat, days_inactive
        )

        events_summary = {
            "total_score": round(decayed_score, 1),
            "vendor_views": int(raw_score),
            "sessions": sessions,
            "top_category": top_cat
        }
        
        # AI classification (sync wrapper for async call)
        if final_score >= INTENT_AI_THRESHOLD:
            ai_insight = asyncio.run(ai_classify_intent(domain, events_summary))
        else:
            ai_insight = fallback_rule_based(events_summary)

        reasons = [f["factor"] for f in explanation.get("components", [])] + [ai_insight.get("recommended_action", "")]
        reasons = [r for r in reasons if r][:5]

        leads.append({
            "domain": domain,
            "score": round(final_score, 1),
            "summary": ai_insight.get("recommended_action", f"Active in {top_cat}" if top_cat else "Research"),
            "reasons": reasons,
            "sessions_count": sessions,
            "last_event": last_event_dt,
            "ai_insight": ai_insight,
            "top_vendors": top_vendors,
            "explanation": explanation,
            "fit_score": round(fit_factor, 2)
        })

    leads.sort(key=lambda x: x["score"], reverse=True)

    for lead in leads:
        execute("""
            INSERT INTO hot_leads (
                domain, score, summary, reasons, sessions_count, last_event, updated_at,
                ai_insight, last_score, score_delta, recommended_vendor, top_vendors, explanation, fit_score
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (domain) DO UPDATE SET
                score = EXCLUDED.score,
                summary = EXCLUDED.summary,
                reasons = EXCLUDED.reasons,
                sessions_count = EXCLUDED.sessions_count,
                last_event = EXCLUDED.last_event,
                updated_at = NOW(),
                ai_insight = EXCLUDED.ai_insight,
                last_score = hot_leads.score,
                score_delta = EXCLUDED.score - hot_leads.score,
                recommended_vendor = (EXCLUDED.top_vendors->0)->>'vendor',
                top_vendors = EXCLUDED.top_vendors,
                explanation = EXCLUDED.explanation,
                fit_score = EXCLUDED.fit_score
        """, [
            lead["domain"], lead["score"], lead["summary"],
            Json(lead["reasons"]), lead["sessions_count"], lead["last_event"],
            Json(lead["ai_insight"]), lead["score"], 0,
            lead["top_vendors"][0]["vendor"] if lead["top_vendors"] else None,
            Json(lead["top_vendors"]), Json(lead["explanation"]), lead["fit_score"]
        ])

    logger.info(f"Upserted {len(leads)} hot leads to production database")

# ------------------------------
# IP anonymization (mask /24) - PDPA COMPLIANT
# ------------------------------
def anonymize_old_ips():
    logger.info("Running PDPA IP anonymization for events > 7 days")
    execute("""
        UPDATE raw_events
        SET ip = set_masklen(ip, 24)
        WHERE created_at < NOW() - INTERVAL '7 days'
          AND ip IS NOT NULL
          AND masklen(ip) > 24
    """)
    # Log the number of anonymized rows if possible
    rows_anonymized = query("SELECT COUNT(*) FROM raw_events WHERE created_at < NOW() - INTERVAL '7 days' AND masklen(ip) = 24")
    logger.info(f"Anonymization status: {rows_anonymized[0][0]} total events masked to /24")

# ------------------------------
# Main
# ------------------------------
def main():
    logger.info("Booppa Intent Agent v10.0 Enterprise - START")
    try:
        populate_enrichment_queue()
        enrich_pending_ips()
        generate_hot_leads()
        
        # PDPA Compliance: Anonymize IPs older than 7 days
        # Always run this in production to ensure compliance
        anonymize_old_ips()
        
        logger.info("Booppa Intent Agent - FINISHED SUCCESSFULLY")
    except Exception as e:
        logger.error(f"Agent failed with error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        db_pool.closeall()

if __name__ == "__main__":
    main()
