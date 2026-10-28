"""SalesQL — a LinkedIn profile in, the person's work email out.

The flow sales teams run by hand: find the company, find its buyer on
LinkedIn, look the profile up in SalesQL, email them. Scout finds the
profile with a web search (Google's index of public profiles, never
LinkedIn itself) and this module does the lookup.

SalesQL answers with every address it knows for the person: personal ones
("Direct", a hotmail.com address) and ones at former employers too. Only a
work address at the company being prospected is used. Cold email to a
personal inbox, or to a job they left, is the wrong email to send.

Each lookup spends a SalesQL credit, so lookups are counted against a daily
budget (SALESQL_DAILY_LOOKUPS, 25 by default) shared by every caller.
"""

import logging
import os
import re
import unicodedata
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("harvey.salesql")

API_BASE = "https://api-public.salesql.com/v1"
TIMEOUT = 30.0

SALESQL_DEFAULT_DAILY_LOOKUPS = 25

# SalesQL's verdict on an address → Harvey's email_status. "Valid" is a
# mailbox SalesQL confirmed; anything it couldn't confirm is "risky", which
# is only sent with channels.email.send_to_risky. "Invalid" is never used.
_STATUS = {"valid": "verified", "invalid": "invalid"}

# Words in a company name that say nothing about which company it is.
_NAME_NOISE = frozenset("""
s.a. sa s.a srl s.r.l. sas sacif saic saci sca inc llc ltd co cia compania
company group grupo argentina arg laboratorio laboratorios industria industrias
de del la las los el y e the and
""".split())


class SalesQLError(Exception):
    """SalesQL can't take more lookups right now (bad key, no credits,
    rate limit). Scout stops calling it for the rest of the cycle."""


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def _same_company_domain(domain: str, company_domain: str) -> bool:
    """mail.acme.example and acme.example are the same company; notacme.example is not."""
    domain = (domain or "").lower().strip(".")
    company_domain = (company_domain or "").lower().strip(".")
    if company_domain.startswith("www."):
        company_domain = company_domain[4:]
    if not domain or not company_domain:
        return False
    return domain == company_domain or domain.endswith("." + company_domain)


def name_tokens(company_name: str) -> list[str]:
    """The words that identify a company by name ("Laboratorio Acme S.A." → ["acme"])."""
    words = re.findall(r"[a-z0-9][a-z0-9.&'-]*", _plain(company_name))
    return [w.strip(".") for w in words if w not in _NAME_NOISE and len(w.strip(".")) >= 3]


def mentions_company(text: str, company_name: str, domain: str = "") -> bool:
    """Does a search result talk about this company (by name or domain)?"""
    plain = _plain(text)
    tokens = name_tokens(company_name)
    if tokens and all(t in plain for t in tokens[:2]):
        return True
    label = (domain or "").lower().split(".")[0]
    return len(label) >= 4 and label in plain


def _current_orgs(person: dict) -> list[dict]:
    orgs = []
    org = person.get("organization")
    if isinstance(org, dict):
        orgs.append(org)
    for job in person.get("work_experience") or []:
        if isinstance(job, dict) and job.get("is_current") and isinstance(job.get("organization"), dict):
            orgs.append(job["organization"])
    return orgs


def works_at(person: dict, domain: str, company_name: str) -> bool:
    """Is the person's current job at this company?

    By the organisation's website domain when SalesQL has it, else by name.
    A profile found by searching a company's name is often someone who
    used to work there, or works at a company with a similar name."""
    for org in _current_orgs(person):
        org_domain = (org.get("website_domain") or "").lower()
        if org_domain:
            if _same_company_domain(org_domain, domain) or _same_company_domain(domain, org_domain):
                return True
            continue
        if mentions_company(org.get("name") or "", company_name):
            return True
    return False


def current_title(person: dict) -> str:
    for job in person.get("work_experience") or []:
        if isinstance(job, dict) and job.get("is_current") and job.get("title"):
            return str(job["title"]).strip()
    return str(person.get("title") or "").strip()


def company_email(person: dict, domain: str) -> tuple[str, str]:
    """The person's work address at ``domain`` and its Harvey status, or ("", "").

    Prefers a SalesQL-verified address over an unconfirmed one."""
    best = ("", "")
    for entry in person.get("emails") or []:
        if not isinstance(entry, dict):
            continue
        email = (entry.get("email") or "").strip().lower()
        if not _same_company_domain(_domain_of(email), domain):
            continue
        status = _STATUS.get(str(entry.get("status") or "").strip().lower(), "risky")
        if status == "invalid":
            continue
        if status == "verified":
            return email, status
        if not best[0]:
            best = (email, status)
    return best


def work_phone(person: dict) -> str:
    for entry in person.get("phones") or []:
        if isinstance(entry, dict) and entry.get("type") == "Work" and entry.get("is_valid"):
            return str(entry.get("phone") or "").strip()
    return ""


async def enrich(linkedin_url: str, api_key: str, client: httpx.AsyncClient | None = None) -> dict | None:
    """SalesQL's record for a LinkedIn profile, or None when it has none.

    Raises SalesQLError when SalesQL won't take more lookups (bad key, out
    of credits, rate limited), so the caller stops for this cycle."""
    params = {"linkedin_url": linkedin_url}
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await client.get(f"{API_BASE}/persons/enrich", params=params, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"SalesQL: lookup failed: {e}")
        return None
    finally:
        if own:
            await client.aclose()
    if r.status_code == 404:
        return None
    if r.status_code in (401, 402, 403, 429):
        raise SalesQLError(f"SalesQL answered HTTP {r.status_code}")
    if r.status_code != 200:
        logger.warning(f"SalesQL: HTTP {r.status_code} for a lookup")
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) and data else None


# ── daily budget ─────────────────────────────────────────────────────────

def _daily_cap() -> int:
    raw = os.getenv("SALESQL_DAILY_LOOKUPS", "").strip()
    if not raw:
        return SALESQL_DEFAULT_DAILY_LOOKUPS
    try:
        return max(int(raw), 0)
    except ValueError:
        return SALESQL_DEFAULT_DAILY_LOOKUPS


def _usage_key() -> str:
    return f"salesql_lookup_count:{datetime.now(timezone.utc):%Y-%m-%d}"


async def lookups_remaining(state) -> int:
    """Lookups left today (UTC)."""
    cap = _daily_cap()
    if state is None:
        return cap
    try:
        used = int(await state.get_setting(_usage_key(), "0") or "0")
    except (ValueError, TypeError):
        used = 0
    except Exception as e:
        logger.debug(f"SalesQL usage read failed: {e}")
        return 0
    return max(cap - used, 0)


async def record_lookup(state) -> None:
    """Count one lookup. Called before the request, so a failed one still counts."""
    if state is None:
        return
    try:
        await state.increment_setting(_usage_key())
    except Exception as e:
        logger.debug(f"SalesQL usage write failed: {e}")
