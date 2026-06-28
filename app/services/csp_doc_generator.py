"""
Booppa CSP Compliance Pack — Document Generator (DeepSeek)
Generates the CSP-specific compliance documents (see CSP_DOCUMENT_CATALOG, 8 documents).

Documents produced:
  1.  AML/CFT/PF Programme (master document)
  2.  CDD Procedures Manual
  3.  STR Policy and Decision Framework
  4.  Nominee Director Assessment Procedure
  5.  Risk-Based Approach Policy
  6.  Client Onboarding Kit (CDD questionnaire + video call checklist)
  7.  Regulatory Compliance Calendar (all deadlines)
  8.  Record Keeping Policy (5-year retention)

All reference:
  - CSP Act 2024 + CSP Regulations 2025 (effective 9 June 2025)
  - ACRA Guidelines for Registered CSPs
  - FATF Recommendations
  - MAS AML/CFT Notice (where MAS-licensed)
  - Singapore Corruption, Drug Trafficking & Other Serious Crimes Act (CDSA)
"""

from __future__ import annotations
import hashlib
import logging, os
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MAX_TOKENS        = 4096
_IN  = 0.014
_OUT = 0.280


def _call(system: str, user: str) -> Tuple[str, int, int]:
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("pip install openai")
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL, max_tokens=MAX_TOKENS, temperature=0.15,
        messages=[{"role":"system","content":system},{"role":"user","content":user}],
    )
    return resp.choices[0].message.content or "", resp.usage.prompt_tokens, resp.usage.completion_tokens


SYSTEM = """You are a Singapore AML/CFT compliance specialist with 20 years of experience
advising Corporate Service Providers (CSPs) registered with ACRA. You have deep expertise in:
- Corporate Service Providers Act 2024 and CSP Regulations 2025 (effective 9 June 2025)
- ACRA's AML/CFT/PF Guidelines for Registered CSPs
- FATF Recommendations as implemented in Singapore
- Singapore Corruption, Drug Trafficking and Other Serious Crimes Act (CDSA)
- Terrorism (Suppression of Financing) Act (TSOFA)
- PDPA 2012 (Amendment 2021)

Your documents are:
- Specific to the CSP's profile, services, and client base
- Immediately implementable — no placeholders except [CUSTOMISE]
- Referenced to exact statutory provisions (CSP Act s.X, CSP Regs s.X)
- Written in clear Singapore English suitable for ACRA regulatory review
- Practical — a compliance officer can follow them on day one

Return ONLY the document content. No preamble, no meta-commentary."""


def _ctx(profile: Dict, clients: List[Dict]) -> str:
    services = [k.replace("offers_","").replace("_"," ").title()
                for k in ["offers_company_formation","offers_nominee_director",
                          "offers_nominee_shareholder","offers_registered_address",
                          "offers_corp_secretarial","offers_shelf_company"]
                if profile.get(k)]
    client_types = list(set(c.get("client_type","unknown") for c in clients[:20]))
    high_risk = sum(1 for c in clients if c.get("risk_rating") in ("high","very_high"))
    peps = sum(1 for c in clients if c.get("is_pep"))
    remote = sum(1 for c in clients if c.get("is_remote_onboarding"))
    return f"""
=== CSP PROFILE ===
Legal Name:      {profile.get('legal_name','[CSP Name]')}
UEN:             {profile.get('uen','XXXXXXXXX')}
ACRA Status:     {profile.get('acra_reg_status','not_started').upper()}
ACRA Reg No:     {profile.get('acra_reg_number','Pending')}
RQI:             {profile.get('rqi_name','[To be designated]')} ({profile.get('rqi_qualification','')})
Services:        {', '.join(services) or 'Not specified'}
AML Officer:     {profile.get('aml_compliance_officer','[To be designated]')}

=== CLIENT BASE ===
Total clients:   {len(clients)}
Client types:    {', '.join(client_types) or 'Not specified'}
High-risk:       {high_risk}
PEPs:            {peps}
Remote clients:  {remote} (require video call verification)

Document date:   {datetime.now(timezone.utc).strftime('%d %B %Y')}
"""


# ── 1. AML/CFT/PF PROGRAMME ──────────────────────────────────────────────────

def gen_aml_programme(profile: Dict, clients: List[Dict]) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    services = [k.replace("offers_","").replace("_"," ").title()
                for k in ["offers_company_formation","offers_nominee_director",
                          "offers_nominee_shareholder","offers_shelf_company"]
                if profile.get(k)]
    prompt = f"""{_ctx(profile, clients)}

Generate a complete AML/CFT/PF Programme for {name}.
This is the master compliance document required under the CSP Act 2024.

SECTION 1 — EXECUTIVE COMMITMENT AND GOVERNANCE
1.1 Senior management commitment statement (signed by CEO/MD)
1.2 Board oversight of AML/CFT/PF compliance
1.3 Compliance officer designation: {profile.get('aml_compliance_officer','[Name]')}
1.4 RQI: {profile.get('rqi_name','[Name]')} — role in AML/CFT governance
1.5 Accountability structure and escalation path

SECTION 2 — RISK-BASED APPROACH (CSP Act s.13)
2.1 Business risk assessment — {name}'s specific risks given services: {', '.join(services) or 'general corporate services'}
2.2 Customer risk factors: PEPs, high-risk countries (FATF grey/black list), complex structures, nominee arrangements
2.3 Product/service risk factors: company formation, shelf company sales, nominee arrangements
2.4 Geographic risk factors: cross-border transactions, offshore structures
2.5 Delivery risk factors: non-face-to-face transactions, intermediaries
2.6 Risk rating methodology: composite scoring → LOW / MEDIUM / HIGH / VERY_HIGH
2.7 Inherent vs residual risk

SECTION 3 — CUSTOMER DUE DILIGENCE (CSP Act s.13 + CSP Regs s.15-20)
3.1 CDD trigger: before providing ANY service
3.2 Standard CDD — individuals: identity verification documents, address, purpose
3.3 Standard CDD — corporate: registration docs, constitution, directors, shareholders, UBOs
3.4 Ongoing monitoring: frequency by risk rating (HIGH = quarterly, MEDIUM = semi-annual, LOW = annual)
3.5 Periodic review triggers: risk change, suspicious activity, regulatory update
3.6 Non-face-to-face specific measures: live video call requirement (CSP Regs s.20)
     - Who must be present on video: proposed director or ≥50% shareholder or authorised representative
     - What must be verified: identity documents, live presence, no script reading
     - Recording: reference number retained
3.7 What to do when CDD fails: decline service + assess STR

SECTION 4 — ENHANCED DUE DILIGENCE (CSP Regs s.21)
4.1 EDD triggers: PEPs, FATF grey/black list countries, complex/opaque structures,
     large transactions, nominee arrangements, unusual patterns
4.2 EDD measures: enhanced source of funds/wealth, senior management approval,
     additional document verification, increased monitoring frequency
4.3 PEP-specific EDD: family members, close associates, ongoing monitoring
4.4 EDD approval matrix: who approves by risk level

SECTION 5 — SUSPICIOUS TRANSACTION REPORTING (CSP Act s.18, CDSA s.39)
5.1 What constitutes a suspicious transaction
5.2 Internal escalation procedure (staff → compliance officer → senior management)
5.3 STR filing procedure with STRO (Suspicious Transaction Reporting Office)
     - Filing portal: SONAR (go.gov.sg/stro)
     - Timeline: file promptly, no statutory deadline but delays = risk
5.4 NON-FILING: must document rationale even when deciding NOT to file
5.5 TIPPING-OFF PROHIBITION: criminal offence under CDSA s.48A and TSOFA
     - Never inform client that STR has been filed
     - Never share information that would alert client to investigation
     - Staff must be trained — penalty: fine + imprisonment
5.6 Ongoing monitoring of subject client after STR filing

SECTION 6 — RECORD KEEPING (CSP Act s.27)
6.1 Retention period: minimum 5 YEARS from end of business relationship
6.2 What to retain: CDD documents, transaction records, STR decisions, risk assessments
6.3 Format: original or certified copies; electronic acceptable with integrity controls
6.4 Access: must be retrievable within reasonable time upon ACRA/law enforcement request
6.5 Destruction: only after 5-year period; certificate of destruction for NRIC-containing docs

SECTION 7 — NOMINEE DIRECTOR MANAGEMENT (CSP Act s.15-17)
7.1 Fit and proper assessment before any arrangement
     Criteria: criminal history, bankruptcy, past directorship conduct, capability
7.2 Assessment process: criminal record check, bankruptcy check, ACRA director history
7.3 Ongoing annual review of all active nominees
7.4 ACRA disclosure: nominee status filed with ACRA (CLLPMA 2024)
7.5 Register maintenance: nominee register updated within [X] days of any change
7.6 Prohibited: arranging nominees who fail fit and proper assessment

SECTION 8 — STAFF TRAINING (CSP Act s.9)
8.1 RQI mandatory training: must complete before CSP registration
8.2 All staff training: AML/CFT/PF foundations annually
8.3 Role-specific training: CDD, EDD, STR by function
8.4 New staff: training within 30 days of joining
8.5 Training records: retained 5 years
8.6 Training providers: ACRA, SIATP, or approved providers

SECTION 9 — PROGRAMME REVIEW
Annual review minimum; trigger-based review upon regulatory change, enforcement action, or material business change.
Next scheduled review: [12 months from today]
Approved by: {profile.get('aml_compliance_officer','[AML Compliance Officer]')}"""
    return _call(SYSTEM, prompt)


# ── 2. CDD PROCEDURES MANUAL ─────────────────────────────────────────────────

def gen_cdd_manual(profile: Dict, clients: List[Dict]) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    has_remote = any(c.get("is_remote_onboarding") for c in clients)
    prompt = f"""{_ctx(profile, clients)}

Generate a complete Customer Due Diligence (CDD) Procedures Manual for {name}.
Step-by-step operational guide for compliance officers and front-line staff.
Reference: CSP Act 2024 s.13, CSP Regulations 2025 s.15-20.

1. CDD TRIGGER CHECKLIST
   Complete before providing ANY of: company formation, nominee director/shareholder arrangement,
   registered office address, corporate secretarial services, shelf company transfer.

2. CLIENT IDENTIFICATION — INDIVIDUAL
   Step-by-step:
   a. Collect: full legal name, NRIC/passport (copy front + back), current address, DOB, nationality
   b. Verify: check photo match, document not expired, address confirmation (utility bill < 3 months)
   c. For foreign nationals: passport + supplementary address verification
   d. NRIC handling: collect as identity verification ONLY, not for ongoing authentication (PDPA Sep 2024)
   e. Sanctions screening: run against UN, OFAC, EU, MAS sanctions lists
   f. PEP screening: check against PEP database (provider: [CUSTOMISE])
   g. Adverse media: Google + subscription database check
   h. Document everything: who checked, when, what was found

3. CLIENT IDENTIFICATION — CORPORATE
   Step-by-step:
   a. Collect: ACRA BizFile extract (< 30 days old), constitution/M&AA, list of directors and shareholders
   b. Identify all directors: CDD on each as individual above
   c. Identify all shareholders ≥25%: CDD on each (individual or corporate)
   d. Trace ownership to Ultimate Beneficial Owner (natural person ≥25% or control)
   e. If ownership structure is complex (multi-layer): trace each layer
   f. If no UBO identified at 25%: apply rule to person with highest interest OR senior managing official
   g. Verify corporate registration in country of incorporation
   h. Sanctions/PEP screen all directors and UBOs

4. NON-FACE-TO-FACE CDD (Remote Clients)
   {'Mandatory for remote clients per CSP Regulations 2025 s.20' if has_remote else 'Procedure for future remote clients'}
   LIVE VIDEO CALL — MANDATORY for:
   - Company incorporation (all cases, non-face-to-face)
   - Shelf company sale or transfer
   - Any remote onboarding where CSP cannot physically verify identity

   Video call requirements:
   a. Who must be present: at least ONE of — proposed director (not nominee) / proposed shareholder ≥50% / authorised rep
   b. What to verify on video: identity document (live, camera-facing), face match to document, real-time presence
   c. Prohibited substitutes: pre-recorded video, telephone call, written statement alone
   d. Recording: reference number + date + attendees logged; recording optional but reference mandatory
   e. Additional document: certified true copy of ID from notary/lawyer/bank
   f. If video call cannot be completed: treat as CDD failure → assess STR

5. SOURCE OF FUNDS AND PURPOSE
   For all clients:
   - Purpose of business relationship (what services needed, why)
   - Source of funds for company formation fees
   High-risk/EDD trigger:
   - Source of wealth (full picture of how client accumulated wealth)
   - Expected transactions (volume, frequency, counterparties)

6. ONGOING MONITORING
   Risk-based frequency:
   - VERY HIGH risk: monthly review
   - HIGH risk: quarterly review
   - MEDIUM risk: semi-annual review
   - LOW risk: annual review
   Trigger-based review: material change in client's business, suspicious activity, negative news

7. CDD FAILURE PROCEDURE
   If CDD cannot be completed (client refuses, documents unsatisfactory, inconsistencies):
   a. DO NOT provide any services
   b. Document the failure and reason in writing
   c. Assess whether circumstances are suspicious (if yes → STR)
   d. DO NOT tip off the client that you are considering an STR
   e. Escalate to compliance officer within 24 hours
   f. Log decision (file STR / not file) with rationale

8. CDD RECORD KEEPING
   Retain ALL CDD documents for minimum 5 years after end of business relationship.
   Format: original or certified copy; electronic acceptable.
   Location: [CUSTOMISE — secure document management system]"""
    return _call(SYSTEM, prompt)


# ── 3. STR POLICY AND DECISION FRAMEWORK ────────────────────────────────────

def gen_str_policy(profile: Dict) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    officer = profile.get("aml_compliance_officer","[AML Compliance Officer]")
    prompt = f"""{_ctx(profile, [])}

Generate a complete STR Policy and Decision Framework for {name}.
References: CSP Act 2024 s.18, CDSA s.39, TSOFA s.8, MAS AML/CFT Notices.
This document must be followed exactly — incorrect handling = criminal liability.

1. WHAT IS AN STR
   A report filed with STRO when there are reasonable grounds to suspect a transaction
   is connected to: Money Laundering (ML) / Terrorism Financing (TF) / Proliferation Financing (PF)
   Legal basis: CDSA s.39 (ML), TSOFA s.8 (TF)

2. TRIGGERS FOR ASSESSMENT
   MANDATORY assessment when:
   - CDD cannot be completed for any reason
   - Client refuses to provide information
   - Transaction pattern is unusual (amount, frequency, parties)
   - Client identity is inconsistent or suspicious
   - Sanctions hit on client, UBO, or nominee
   - Adverse media linking client to financial crime
   - Complex/opaque corporate structure with no clear business rationale
   - Instructions to conduct unusual transactions without clear commercial purpose

3. INTERNAL ESCALATION PROCEDURE
   Step 1 — Staff: identify concern → complete Internal Suspicious Activity Report (ISAR)
             Do NOT take any action that could tip off the client
             Do NOT proceed with the transaction
   Step 2 — Within 24 hours: submit ISAR to {officer}
   Step 3 — {officer} reviews within 48 hours: gather additional info if safe to do so
   Step 4 — Decision: file STR / not file STR (BOTH require documented rationale)
   Step 5 — If filing: submit via SONAR portal (go.gov.sg/stro)
   Step 6 — If not filing: document rationale in writing and retain

4. FILING PROCEDURE
   Platform: STRO SONAR (go.gov.sg/stro) — secure online portal
   Information required in STR:
   - Reporter details ({name}, ACRA reg number)
   - Subject details (client name, ID, address)
   - Transaction details (amount, currency, date, counterparties)
   - Nature of suspicion (specific facts, not general concerns)
   - Actions taken (e.g. declined service, froze account)
   Timeline: file as soon as practicable — no fixed statutory deadline but delays create risk
   Authorised filer: {officer} or designated deputy
   After filing: do not communicate externally about the filing; await STRO response

5. NON-FILING DOCUMENTATION (MANDATORY)
   Even when deciding NOT to file an STR, document:
   - Date of assessment
   - Facts considered
   - Reasons for concluding no reasonable grounds for suspicion
   - Who made the decision and their authority
   - Retained in AML/CFT records for 5 years minimum

6. TIPPING-OFF PROHIBITION — CRIMINAL OFFENCE
   Under CDSA s.48A and TSOFA:
   ❌ NEVER tell the client an STR has been filed
   ❌ NEVER share information that would alert client to investigation
   ❌ NEVER allow the client to withdraw assets after STR decision to file
   ❌ NEVER discuss with unauthorized parties (colleagues without need-to-know)
   Penalty: fine up to S$250,000 and/or imprisonment up to 3 years
   
   Safe harbour: continuing normal business activity (without alerting client) is permitted

7. POST-STR PROCEDURES
   After filing:
   - Do not immediately terminate relationship (may alert client)
   - Seek legal advice on whether/how to offboard client
   - Increase monitoring of client if relationship continues
   - Await any STRO/CAD/MAS contact and cooperate fully

8. STAFF TRAINING ON STR
   All CDD-handling staff: annual STR training
   Topics: recognition of red flags, escalation procedure, tipping-off prohibition
   Training records retained 5 years
   Assessment: pass/fail test on tipping-off scenarios

9. DECISION LOG
   Maintain an STR Decision Register (separate from client files):
   Date | Client ID (anonymised) | Trigger | Decision | Rationale | Filed by | STRO Reference
   Access: restricted to AML Compliance Officer and senior management
   Retention: 5 years minimum"""
    return _call(SYSTEM, prompt)


# ── 4. NOMINEE DIRECTOR ASSESSMENT PROCEDURE ─────────────────────────────────

def gen_nominee_procedure(profile: Dict) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    prompt = f"""{_ctx(profile, [])}

Generate a complete Nominee Director and Shareholder Assessment Procedure for {name}.
References: CSP Act 2024 s.15-17, CLLPMA 2024, ACRA Guidelines.
This procedure must be followed for EVERY nominee arrangement.

1. OVERVIEW AND LEGAL BASIS
   Under CSP Act 2024 s.15: {name} must not arrange for any person to act as nominee director
   unless that person has been assessed as FIT AND PROPER.
   Penalty for non-compliance: up to S$100,000 fine.
   Nominee status is made PUBLIC by ACRA (nominee status, not nominator identity).
   Nominator identity is filed CONFIDENTIALLY with ACRA.

2. FIT AND PROPER CRITERIA (ACRA Guidelines)
   A nominee director must NOT:
   - Have a criminal record (especially financial crimes, fraud, dishonesty)
   - Be an undischarged bankrupt
   - Have been disqualified as a company director
   - Have a history of serious regulatory breaches as a director
   - Lack the basic capability to serve as a director
   
   A nominee director MUST:
   - Be a natural person (not corporate)
   - Be at least 18 years old
   - Understand their role and responsibilities as a director
   - Not be subject to any court order restricting management participation

3. PRE-APPOINTMENT ASSESSMENT CHECKLIST
   □ Collect full legal name, NRIC/passport, DOB, address
   □ Criminal record check: request police certificate (ICA or home country authority)
   □ Bankruptcy check: Ministry of Law insolvency search (go.gov.sg/mlaw) or IPTO
   □ ACRA director history: BizFile search for past/current directorships
   □ Sanctions screening: UN, OFAC, EU, MAS lists
   □ PEP check: is nominee (or family member) a politically exposed person?
   □ Reference check: character references if no prior relationship [optional but recommended]
   □ Nominee declaration: written acknowledgment of nominee status and obligations
   □ CDD completed on the NOMINATOR (who is the real controller)

4. ASSESSMENT OUTCOME AND DOCUMENTATION
   APPROVED: all checks clear → proceed to arrangement → document in nominee register
   REJECTED: any disqualifying factor → decline arrangement → document reason → do not tip off
   UNDER REVIEW: escalate to AML Compliance Officer → senior management decision within 5 business days

5. ANNUAL REVIEW
   All active nominees must be reviewed annually:
   - Repeat bankruptcy check
   - ACRA director history check
   - Re-screen sanctions/PEP lists
   - Confirm nominee still understands and accepts role
   - Update register if any changes
   Review due dates tracked in Booppa Compliance Calendar.

6. ACRA DISCLOSURE OBLIGATIONS (CLLPMA 2024)
   From 16 June 2025: all nominee directors and shareholders must be filed with ACRA.
   Filing deadline: within [X] days of appointment (check current ACRA BizFile requirement)
   Information to file: nominee status, nominator identity (confidential), effective date
   Nominee status (not nominator) made publicly visible on BizFile.
   Failure to file: fine up to S$25,000.

7. NOMINEE REGISTER MAINTENANCE
   {name} must maintain:
   a. Register of Nominee Directors: name, company, nominator, appointment date, assessment date, outcome
   b. Register of Nominee Shareholders: name, company, shares, nominator, appointment date
   Update within 7 days of any change (appointment, cessation, material change).
   Retain all records 5 years after cessation of nominee arrangement.

8. CEASING A NOMINEE ARRANGEMENT
   If nominee fails annual review or circumstances change:
   - Notify company's board of necessity to replace nominee
   - Allow reasonable transition period (typically 30-60 days)
   - File cessation with ACRA via BizFile
   - Update internal register
   - Do not abruptly terminate in a way that causes regulatory breach for the company"""
    return _call(SYSTEM, prompt)


# ── 5. RISK-BASED APPROACH POLICY ────────────────────────────────────────────

def gen_risk_policy(profile: Dict, clients: List[Dict]) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    client_count = len(clients)
    high_risk = sum(1 for c in clients if c.get("risk_rating") in ("high","very_high"))
    prompt = f"""{_ctx(profile, clients)}

Generate a Risk-Based Approach (RBA) Policy for {name}.
References: CSP Act 2024 s.13, FATF Recommendations 1 and 10, ACRA Guidelines for Registered CSPs.

{name} currently serves {client_count} clients, of whom {high_risk} are rated HIGH or VERY_HIGH risk.

1. POLICY STATEMENT
   {name} adopts a risk-based approach to AML/CFT/PF compliance — allocating resources
   proportionate to the risk posed by each client, service, and transaction.
   This is not a box-ticking exercise: higher-risk clients receive more scrutiny, lower-risk
   clients receive proportionate measures.

2. BUSINESS-WIDE RISK ASSESSMENT
   {name}'s inherent risk profile given services offered:
   [Assess each service category for ML/TF/PF risk — company formation, nominee services, etc.]
   
   Key risk factors specific to {name}:
   - Service mix risk: [high if nominee/shelf company services offered]
   - Geographic exposure: cross-border clients
   - Client type diversity: individual vs corporate vs foreign entities
   - Delivery channel: face-to-face vs remote

3. CUSTOMER RISK SCORING METHODOLOGY
   Score each factor 1-5:
   
   COUNTRY RISK (weight: 25%)
   1 = Singapore / FATF member with strong AML
   3 = Moderate-risk jurisdiction
   5 = FATF grey list / non-cooperative jurisdiction / high corruption
   
   INDUSTRY/BUSINESS RISK (weight: 20%)
   1 = Regulated industry (banking, legal, accounting)
   3 = Cash-intensive business
   5 = High-risk industry (cryptocurrency, gaming, precious metals)
   
   PRODUCT/SERVICE RISK (weight: 20%)
   1 = Standard corporate secretarial
   3 = Nominee arrangements
   5 = Shelf company acquisition, complex multi-jurisdiction structure
   
   DELIVERY RISK (weight: 15%)
   1 = In-person, face-to-face
   3 = Remote with video verification
   5 = Remote without verification, third-party intermediary
   
   CUSTOMER TYPE (weight: 20%)
   1 = Individual SG resident, regulated professional
   3 = Foreign individual, private company
   5 = PEP, politically sensitive, adverse media
   
   COMPOSITE SCORE → RISK RATING:
   1.0-2.0 = LOW → annual review
   2.1-3.0 = MEDIUM → semi-annual review
   3.1-4.0 = HIGH → quarterly review + EDD
   4.1-5.0 = VERY HIGH → monthly review + EDD + senior approval

4. RISK TRIGGERS FOR IMMEDIATE REASSESSMENT
   - Adverse media discovery
   - Sanctions hit
   - Client behaviour change (unusual transactions, secretive about purpose)
   - Change in UBO or control structure
   - STR filed or internal SAR raised
   - Regulatory inquiry about client

5. PORTFOLIO RISK MANAGEMENT
   Maximum exposure guidelines [CUSTOMISE]:
   - HIGH risk clients: maximum [X]% of client portfolio
   - VERY HIGH risk: maximum [X]% with senior management approval for each
   - PEP clients: maximum [X], all require CEO approval

6. REVIEW AND UPDATE
   Annual review of business-wide risk assessment.
   Update when: new services offered, new client segments, regulatory changes, enforcement trends."""
    return _call(SYSTEM, prompt)


# ── 6. CLIENT ONBOARDING KIT ─────────────────────────────────────────────────

def gen_onboarding_kit(profile: Dict) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    prompt = f"""{_ctx(profile, [])}

Generate a complete Client Onboarding Kit for {name}.
This is what {name} sends to every new client before commencing services.
Must be practical, clear, and compliant with CSP Act CDD requirements.

PART A — CLIENT WELCOME LETTER
Professional letter explaining:
- Why {name} is required to conduct CDD (CSP Act 2024 obligations)
- What information is needed and why
- Timeline for completion
- Confidentiality assurance (information used only for regulatory compliance)
- DPO contact for data protection queries: [CUSTOMISE]

PART B — INDIVIDUAL CLIENT QUESTIONNAIRE
(For individual clients and individual directors/shareholders)
Complete with all fields a CSP needs for CDD compliance:

Section 1: Personal Information
- Full legal name (as in passport/NRIC)
- Date of birth
- Nationality
- Country of residence
- Current residential address
- Contact phone and email

Section 2: Identity Documents
- Document type (NRIC / Singapore Passport / Foreign Passport)
- Document number
- Expiry date
- Consent to retain copy for regulatory purposes (CSP Act s.27)

Section 3: Business Purpose
- Purpose of engaging {name}'s services
- Nature of business/activities
- Source of funds for company/service fees

Section 4: PEP Declaration (mandatory)
- Are you, or is any family member or close associate, a current or former:
  Government official, senior political figure, judicial official, military officer, senior executive of state-owned enterprise?
- If YES: provide details and supporting documentation

Section 5: Sanctions and Adverse Information
- Have you ever been convicted of any criminal offence (including overseas)?
- Are you subject to any sanctions, asset freeze, or travel ban?
- Are you currently subject to any regulatory investigation?

Section 6: Data Protection Consent
- PDPA consent for collection and processing of personal data
- NRIC collection notice (purpose, retention, withdrawal rights)

PART C — CORPORATE CLIENT QUESTIONNAIRE
(For corporate entities)
Section 1: Company Information
- Legal name, UEN/registration number, country of incorporation
- Registered address, business address
- Date of incorporation, company type
- Nature of business / industry

Section 2: Directors (list all)
For each director: full name, nationality, NRIC/passport, address, role
PEP declaration for each director

Section 3: Shareholders (list all with ≥25% shareholding)
For each: full name, shareholding %, nationality, address

Section 4: Ultimate Beneficial Owners
- Identify every natural person with ≥25% ownership or effective control
- For each UBO: full name, DOB, nationality, address, ownership mechanism

Section 5: Business Activity and Source of Funds
- Detailed business description
- Primary markets and customers
- Expected transaction volume
- Source of funds for service fees

Section 6: Nominee Arrangements
- Will any nominee directors or shareholders be appointed? (Y/N)
- If yes: provide nominator details

PART D — DOCUMENT CHECKLIST
Itemised list of documents required from each client type (individual / SG company / foreign company / LLP).
Clear format with submission instructions.

PART E — VIDEO CALL INSTRUCTIONS (for remote clients)
Step-by-step guide for the mandatory live video call:
- What to prepare: original identity documents
- Who must attend (at least one proposed director or ≥50% shareholder)
- What will happen on the call (identity verification, document check)
- Technical requirements (stable internet, good lighting, camera)
- What happens if the call cannot be completed
- Booking link: [CUSTOMISE]"""
    return _call(SYSTEM, prompt)


# ── 7. COMPLIANCE CALENDAR ────────────────────────────────────────────────────

def gen_compliance_calendar(profile: Dict) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    prompt = f"""{_ctx(profile, [])}

Generate a complete Regulatory Compliance Calendar for {name} for the next 12 months.
Include EVERY regulatory obligation with exact deadline, legal basis, and penalty for non-compliance.

Format each item as:
DEADLINE | OBLIGATION | LEGAL BASIS | PENALTY IF MISSED | OWNER | STATUS

Cover ALL of the following obligation categories:

1. ACRA CSP REGISTRATION
   - Annual licence renewal (check {profile.get('acra_renewal_date','[renewal date]')})
   - Notification of material changes to ACRA (within 14 days of change)
   - Annual return filing

2. AML/CFT/PF PROGRAMME
   - Annual programme review
   - Update upon regulatory change
   - Senior management approval of annual review

3. CDD PERIODIC REVIEWS
   - HIGH risk clients: quarterly review schedule
   - MEDIUM risk clients: semi-annual review schedule
   - LOW risk clients: annual review schedule
   - Note: automated by Booppa client monitoring

4. NOMINEE REGISTERS
   - Annual review of all active nominee directors (fit and proper)
   - Annual review of all nominee shareholders
   - ACRA filing within [X] days of any new nominee appointment
   - ACRA filing within [X] days of nominee cessation
   - RORC (Register of Registrable Controllers) annual update — Companies Act s.389

5. STAFF TRAINING
   - RQI annual AML/CFT training
   - All AML/CFT-handling staff: annual training
   - New staff onboarding training (within 30 days of joining)

6. PDPA COMPLIANCE
   - DPMP annual review
   - Privacy Notice update (if material changes)
   - NRIC remediation deadline: 31 December 2026

7. BENEFICIAL OWNER REGISTER
   - Annual update of UBO information for all active clients
   - Update within 7 days of any change in UBO status

8. RECORD KEEPING REVIEW
   - Annual audit of records approaching 5-year retention limit
   - Secure destruction of expired records with certificate

9. STR MANAGEMENT
   - Monthly review of pending STR assessments
   - Annual training update for STR procedures

10. REGULATORY MONITORING
    - Monthly: check ACRA enforcement announcements
    - Monthly: check PDPC enforcement decisions
    - Quarterly: FATF guidance updates
    - Ongoing: Singapore Statutes Online for legislative amendments

Add a section on:
AUTOMATED MONITORING BY BOOPPA:
List all obligations that Booppa tracks automatically with alert triggers (30 days, 14 days, 7 days, overdue)."""
    return _call(SYSTEM, prompt)


# ── 8. RECORD KEEPING POLICY ─────────────────────────────────────────────────

def gen_record_keeping(profile: Dict) -> Tuple[str,int,int]:
    name = profile.get("legal_name","[CSP]")
    prompt = f"""{_ctx(profile, [])}

Generate a comprehensive Record Keeping Policy for {name}.
References: CSP Act 2024 s.27, PDPA s.25, Companies Act s.199, Limitation Act s.6.

1. POLICY STATEMENT AND SCOPE
   {name} maintains complete, accurate, and retrievable records for:
   - All CDD and EDD documents and decisions
   - All STR and internal SAR assessments
   - All nominee director/shareholder assessments
   - All beneficial owner identification
   - All AML/CFT training records
   - All client transaction records

2. RETENTION PERIODS
   Table format: Record Type | Retention Period | Legal Basis | Start of Retention | Disposal Method

   AML/CFT RECORDS (CSP Act s.27):
   - CDD documents: 5 years from end of business relationship
   - EDD documents: 5 years from end of business relationship
   - STR filing records: 5 years from date of filing
   - STR non-filing rationale: 5 years from date of decision
   - Transaction records: 5 years from date of transaction
   - Risk assessments: 5 years from end of relationship
   - Beneficial owner records: 5 years from end of relationship

   NOMINEE RECORDS (CSP Act + CLLPMA 2024):
   - Nominee register entries: 5 years from cessation of nominee arrangement
   - Fit and proper assessment: 5 years from cessation
   - ACRA filing records: 5 years from cessation

   CORPORATE RECORDS (Companies Act):
   - Financial records: 7 years (Companies Act s.199)
   - Client contracts: duration + 6 years (Limitation Act s.6)
   - Correspondence: 3-5 years by type

   PDPA RECORDS (PDPA s.25):
   - Consent records: duration of consent + 3 years
   - Data breach records: 5 years
   - Privacy Notice versions: indefinitely (for historical reference)
   - NRIC records: per NRIC retention schedule (30 days for access logs; purpose period + minimum for KYC)

   TRAINING RECORDS (CSP Act s.9):
   - AML/CFT training: 5 years from completion
   - Certificates: 5 years

3. STORAGE AND SECURITY
   Physical documents: [CUSTOMISE — fire-safe, access-controlled storage]
   Electronic records: [CUSTOMISE — encrypted, access-logged, backed up daily]
   Cloud storage: PDPA-compliant provider, Singapore data residency preferred
   Access: need-to-know basis; AML/CFT records restricted to compliance function

4. RETRIEVAL
   ACRA/law enforcement request: must be able to retrieve within [CUSTOMISE — 2 business days]
   Court order or production notice: immediate escalation to legal counsel
   Internal audit: 5 business days

5. INTEGRITY AND AUTHENTICITY
   No alteration of original CDD documents after completion
   Audit trail for all electronic records (who accessed, when, what changed)
   Version control for all policy documents
   Blockchain notarization via Booppa for key compliance records

6. DESTRUCTION
   Physical: cross-cut shredding (DIN 66399 Level P-4 for confidential); certificate of destruction
   Electronic: DoD 5220.22-M standard deletion; provider confirmation for cloud
   NRIC-specific: certificate of destruction required
   Never destroy records subject to legal hold

7. LEGAL HOLD
   All records relevant to: litigation / ACRA investigation / CAD investigation / STRO inquiry
   must be preserved regardless of retention schedule.
   Legal hold activated by: AML Compliance Officer or Legal Counsel
   Approval to lift legal hold: same authorities"""
    return _call(SYSTEM, prompt)


# ── DOCUMENT CATALOG ─────────────────────────────────────────────────────────

CSP_DOCUMENT_CATALOG = [
    ("aml_programme",          "AML/CFT/PF Programme",                       gen_aml_programme,   True),
    ("cdd_manual",             "Customer Due Diligence Procedures Manual",    gen_cdd_manual,      True),
    ("str_policy",             "STR Policy and Decision Framework",           gen_str_policy,      False),
    ("nominee_procedure",      "Nominee Director Assessment Procedure",        gen_nominee_procedure, False),
    ("risk_policy",            "Risk-Based Approach Policy",                  gen_risk_policy,     True),
    ("client_onboarding_kit",  "Client Onboarding Kit",                       gen_onboarding_kit,  False),
    ("compliance_calendar",    "Regulatory Compliance Calendar",               gen_compliance_calendar, False),
    ("record_keeping_policy",  "Record Keeping Policy",                        gen_record_keeping,  False),
]
# True = needs clients list; False = profile only


def generate_all_csp_documents(profile: Dict, clients: List[Dict]) -> List[Dict]:
    results = []
    for doc_type, title, fn, needs_clients in CSP_DOCUMENT_CATALOG:
        logger.info("Generating CSP doc: %s via DeepSeek", doc_type)
        try:
            if needs_clients:
                content, in_tok, out_tok = fn(profile, clients)
            else:
                content, in_tok, out_tok = fn(profile) if len(fn.__code__.co_varnames) == 1 else fn(profile, [])
            cost = (in_tok/1_000_000*_IN) + (out_tok/1_000_000*_OUT)
            results.append({
                "doc_type": doc_type, "title": title,
                "content": content, "input_tokens": in_tok,
                "output_tokens": out_tok, "cost_usd": round(cost,5),
                "word_count": len(content.split()),
                "generated_by_model": DEEPSEEK_MODEL,
            })
            logger.info("✓ %s — %d words | $%.5f", doc_type, len(content.split()), cost)
        except Exception as e:
            logger.error("✗ %s failed: %s", doc_type, e, exc_info=True)
            results.append({"doc_type": doc_type, "title": title,
                            "content": None, "error": str(e)})

    total = sum(r.get("cost_usd",0) for r in results)
    logger.info("CSP docs complete — %d docs | $%.4f total", len(results), total)
    return results


# ── PDF RENDERING ─────────────────────────────────────────────────────────────

LEGAL_DISCLAIMER = (
    "Booppa Smart Care LLC generates AML/CFT documentation and tracks compliance "
    "obligations based on information provided by the CSP. This does not constitute "
    "legal advice. The CSP remains solely responsible for its own regulatory compliance "
    "under the CSP Act 2024 and all applicable Singapore legislation. Booppa's blockchain "
    "evidence trail provides documented proof of compliance efforts but does not guarantee "
    "regulatory approval. All AML/CFT documents generated should be reviewed by a qualified "
    "Singapore AML/CFT compliance professional before being relied upon."
)


def _xml_escape(s: str) -> str:
    """Escape user/AI text so ReportLab's Paragraph mini-XML doesn't misinterpret
    `&`, `<`, `>` (e.g. "Q&A" becomes an entity-start and breaks rendering)."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_csp_document_pdf(
    title: str,
    body: str,
    meta: Optional[Dict] = None,
) -> Tuple[bytes, str]:
    """Render a generated CSP compliance document (markdown-ish text) to a PDF.

    Returns ``(pdf_bytes, sha256_hex)``. The SHA-256 is over the PDF bytes and is
    what gets anchored on-chain by the notarization task.

    Markdown handled: ``#``/``##``/``###`` headings, ``- ``/``* `` bullets, blank-line
    paragraph breaks. All text is ``_xml_escape``d before entering a Paragraph.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether,
    )

    meta = meta or {}
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("CspH1", parent=styles["Heading1"], fontSize=16, spaceAfter=10, textColor=colors.HexColor("#0f172a"))
    h2 = ParagraphStyle("CspH2", parent=styles["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=6, keepWithNext=1)
    h3 = ParagraphStyle("CspH3", parent=styles["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=4, keepWithNext=1)
    body_style = ParagraphStyle("CspBody", parent=styles["BodyText"], fontSize=9.5, leading=14, spaceAfter=6)
    bullet_style = ParagraphStyle("CspBullet", parent=body_style, leftIndent=16, bulletIndent=4)
    small = ParagraphStyle("CspSmall", parent=styles["BodyText"], fontSize=7.5, leading=10, textColor=colors.HexColor("#64748b"))

    flow = [Paragraph(_xml_escape(title), h1)]
    sub = []
    if meta.get("legal_name"):
        sub.append(_xml_escape(str(meta["legal_name"])))
    if meta.get("uen"):
        sub.append("UEN " + _xml_escape(str(meta["uen"])))
    sub.append("Generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    flow.append(Paragraph(" · ".join(sub), small))
    flow.append(Spacer(1, 0.2 * inch))

    for raw in (body or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flow.append(Spacer(1, 0.06 * inch))
            continue
        if line.startswith("### "):
            flow.append(Paragraph(_xml_escape(line[4:]), h3))
        elif line.startswith("## "):
            flow.append(Paragraph(_xml_escape(line[3:]), h2))
        elif line.startswith("# "):
            flow.append(Paragraph(_xml_escape(line[2:]), h2))
        elif line.lstrip().startswith(("- ", "* ")):
            txt = line.lstrip()[2:]
            flow.append(Paragraph(_xml_escape(txt), bullet_style, bulletText="•"))
        else:
            flow.append(Paragraph(_xml_escape(line), body_style))

    flow.append(PageBreak())
    flow.append(KeepTogether([
        Paragraph("Legal Disclaimer", h3),
        Paragraph(_xml_escape(LEGAL_DISCLAIMER), small),
    ]))

    buf = BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=title, author="Booppa Smart Care LLC",
    ).build(flow)
    pdf_bytes = buf.getvalue()
    return pdf_bytes, hashlib.sha256(pdf_bytes).hexdigest()
