"""
scout_outreach_templates.py
Booppa Smart Care LLC — SCOUT Agents, outreach email generation

Kept close to the original three scripts' templates — that copy was
solid and, importantly, already correctly enforced the free-scan
disclosure policy (dimension NAMES only, never remediation detail). The
change here: generation happens once, at PENDING_APPROVAL time, and the
result is STORED on ScoutProspect (outreach_subject / outreach_body_html)
rather than regenerated on every send attempt — so what a human approves
is exactly what gets sent, word for word, not a fresh draft that could
differ if regenerated later.

Output is real HTML now (branded_email_html), not the original plain-text
templates — needed since these now go through send_html_email, not a
.txt file a human copy-pastes.

Spam Control Act (Cap. 311A) compliance — added 2026-08-07, see
AUDIT_2026-08-07.md P0-3. Every message these functions produce is an
unsolicited commercial message, so all three must carry:
  * an "<ADV>" subject prefix (Second Schedule para 3), via ``adv_subject``;
  * an unsubscribe facility *inside the body* (para 2(a)) — the SMTP
    List-Unsubscribe header alone does not satisfy this;
  * the sender's registered postal address (para 2(b)).
The last two come from ``branded_email_html(unsubscribe_url=…,
postal_address=…)``, which needs the recipient address — so these
functions now require ``prospect_data["contact_email"]`` and return
``("", "")`` without it. A prospect with no address cannot be emailed
anyway, so refusing to render is strictly better than rendering an
advertisement that has no working opt-out in it.
"""

from app.core.config import settings
from app.services.email_layout import adv_subject, branded_email_html, email_button
from app.services.email_suppression import unsubscribe_url

# No pipeline captures a contact person's name — only ``contact_email`` (see
# scout_shared.extract_contact_email). The templates used to open with the
# literal "Dear [Name]," and shipped it unfilled, because nothing could fill it
# and no endpoint could edit the stored body. A neutral salutation is honest;
# a reviewer who knows the person can personalise it through
# PATCH /scout/pending/{id} before approving.
_SALUTATION = "<p>Hello,</p>"


def _render(prospect_data: dict, subject: str, body_inner: str, preheader: str) -> tuple[str, str]:
    """Wrap an outreach body in the marketing frame, or refuse to render.

    Returns ``("", "")`` when the message cannot be made compliant — no
    recipient address to build an unsubscribe link for, or no configured
    postal address. Callers store the empty result and the send task skips
    the prospect; nothing half-compliant is ever produced.
    """
    to_email = (prospect_data.get("contact_email") or "").strip()
    postal = (settings.COMPANY_POSTAL_ADDRESS or "").strip()
    if not to_email or not postal:
        return "", ""
    return adv_subject(subject), branded_email_html(
        body_inner,
        title=subject,
        preheader=preheader,
        unsubscribe_url=unsubscribe_url(to_email),
        postal_address=postal,
    )


def generate_vendor_outreach(prospect_data: dict, sector_top_name: str, sector_top_score: int) -> tuple[str, str]:
    """Returns (subject, body_html). prospect_data is a ScoutProspect.raw_data dict."""
    findings = prospect_data.get("findings", {})
    failing = [
        (cfg["label"]) for key, cfg in {
            "cookie_banner": {"label": "Cookie Consent Banner"},
            "privacy_policy": {"label": "Privacy Policy Present"},
            "dpo_contact": {"label": "DPO Contact Visible"},
        }.items()
        if not findings.get(key, {}).get("present", True)
    ][:2]
    # Only positively-detected gaps are named. This block used to pad a short
    # list with "a PDPA compliance gap" / "your data protection policy" and then
    # present both under "The two most urgent gaps identified" — asserting
    # findings the scan never made. Same absence-of-evidence rule the PDPA
    # product already follows: nothing detected means the section is dropped.
    gaps_block = ""
    if failing:
        items = "".join(f"<li>{label}</li>" for label in failing)
        heading = (
            "The most urgent gap identified on your public-facing site:"
            if len(failing) == 1
            else "The two most urgent gaps identified on your public-facing site:"
        )
        gaps_block = f"<p>{heading}</p><ol>{items}</ol>"

    rank_line = ""
    if prospect_data.get("sector_rank"):
        rank_line = f"<p>In our current index, {prospect_data['clean_name']} is ranked #{prospect_data['sector_rank']} of verified {prospect_data.get('sector', '')} vendors in Singapore.</p>"

    compare_line = ""
    if sector_top_name and sector_top_score > prospect_data.get("trust_score", 0):
        compare_line = f"<p>The top-verified {prospect_data.get('sector', '')} vendor in our index currently has a Trust Score of {sector_top_score}/100.</p>"

    upsell = ""
    # Gated on failing checks, not on ``len(findings)`` — findings always holds
    # one entry per check performed, so the old condition fired on every TIER1
    # prospect and told vendors with a clean scan that gaps had been found.
    if prospect_data.get("priority_tier") == "TIER1" and len(failing) >= 2:
        upsell = ("<p>Given the number of gaps found, you may also want to look at the "
                   "Compliance Bundle (S$799 one-time) — the full PDPA governance pack, "
                   "an RFP-ready compliance dossier, and a blockchain-anchored cover sheet "
                   "in one deliverable.</p>")

    subject = f"{prospect_data['clean_name']} — Trust Score {prospect_data.get('trust_score', 0)}/100 on the GeBIZ Vendor Index"
    body_inner = f"""
    {_SALUTATION}
    <p>I recently completed an automated PDPA compliance scan of active GeBIZ vendors in Singapore.</p>
    <p><strong>{prospect_data['clean_name']}</strong> received a Trust Score of {prospect_data.get('trust_score', 0)}/100.</p>
    {rank_line}{compare_line}
    {gaps_block}
    <p>The Booppa PDPA Quick Scan gives you the full picture in under 5 minutes: every finding
    with remediation steps, PDPC enforcement precedents, and an audit-ready PDF report.</p>
    {email_button("https://booppa.io/pdpa-quick-scan", "Run the PDPA Quick Scan — S$299")}
    {upsell}
    <p>Worth a 15-minute call this week?</p>
    """
    return _render(prospect_data, subject, body_inner, "Automated PDPA compliance signal")


def generate_buyer_outreach(prospect_data: dict) -> tuple[str, str]:
    tier_pitch = {
        "BUYER_ENTERPRISE_FIT": ("Buyer Enterprise (S$799/mo)", [
            "100 Quick Scan + 100 Deep Scan across your entire supplier panel",
            "multi-subsidiary management for different BUs / legal entities",
            "unlimited-seat RBAC and REST API for ERP integration",
        ]),
        "BUYER_PROFESSIONAL_FIT": ("Buyer Professional (S$399/mo)", [
            "50 Quick Scan + Deep Scan on 20 suppliers/month",
            "drift tracking with automatic alerts on status changes",
            "side-by-side supplier comparison for tender shortlists",
        ]),
        "BUYER_ESSENTIALS_FIT": ("Buyer Essentials (S$149/mo)", [
            "10 Quick Scan/month (ACRA + international sanctions lists + PDPA flag)",
            "traffic-light compliance dashboard",
            "automatic alert when a supplier enters critical status",
        ]),
    }
    pitch = tier_pitch.get(prospect_data.get("fit_tier"))
    if not pitch:
        return "", ""
    product, bullets = pitch
    bullets_html = "".join(f"<li>{b}</li>" for b in bullets)

    regulated_line = ""
    if prospect_data.get("regulated_signal"):
        regulated_line = "<p>Given your sector, traceable supplier due-diligence evidence is likely already a requirement, not just an operational convenience.</p>"

    subject = f"{prospect_data['agency_name']} — {prospect_data['award_count']} GeBIZ awards last year, how much time does supplier due diligence cost?"
    body_inner = f"""
    {_SALUTATION}
    <p>Public GeBIZ data shows {prospect_data['agency_name']} awarded {prospect_data['award_count']}
    contracts last year to {prospect_data.get('distinct_vendors', 0)} different suppliers.</p>
    {regulated_line}
    <p>Booppa {product} replaces manual supplier verification with instant, verifiable compliance evidence:</p>
    <ul>{bullets_html}</ul>
    <p>Worth a 15-minute comparison this week?</p>
    """
    return _render(
        prospect_data, subject, body_inner,
        f"{prospect_data['award_count']} GeBIZ awards last year",
    )


def generate_csp_outreach(prospect_data: dict) -> tuple[str, str]:
    urgency_line = ""
    if prospect_data.get("licence_age_tier") == "NEW":
        urgency_line = ("<p>Since your CSP licence was issued recently, this is also the moment "
                         "regulators expect a fully documented AML/CFT Programme to already be in "
                         "place under the CSP Act 2024 — not a future project.</p>")

    findings = prospect_data.get("findings", {})
    missing = [
        cfg.get("label", key) for key, cfg in findings.items() if not cfg.get("present", True)
    ][:3]
    gaps_line = f"<p>Areas we could not confirm on your public site: {', '.join(missing)}.</p>" if missing else ""

    subject = f"{prospect_data['clean_name']} — AML/CFT Readiness Check (CSP Act 2024)"
    body_inner = f"""
    {_SALUTATION}
    <p>I ran an independent public-signal review of AML/CFT programme readiness across
    ACRA-licensed Corporate Service Providers in Singapore.</p>
    <p><strong>{prospect_data['clean_name']}</strong> scored {prospect_data.get('aml_readiness_score', 0)}/100
    on public readiness signals (this reflects what is visible on your public site, not an audit
    of your actual internal programme).</p>
    {urgency_line}{gaps_line}
    <p>The Booppa CSP Compliance Pack builds your complete, blockchain-notarized AML/CFT/PF
    Programme: 8 AI-generated documents, a client CDD/EDD tracker, STR decision framework,
    nominee director and UBO registers, risk-based scoring, a compliance calendar, and a
    completion certificate for your ACRA licence renewal — in one deliverable.</p>
    {email_button("https://booppa.io/csp-compliance-pack", "S$3,999 one-time, or S$299/month")}
    <p>Worth a 15-minute call this week?</p>
    """
    return _render(prospect_data, subject, body_inner, "AML/CFT readiness signal")
