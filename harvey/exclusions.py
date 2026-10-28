"""Companies never to contact, by name.

compliance.do_not_contact bars domains and addresses. A customer list
exported from an ERP or a sales sheet has neither, only names ("ACME
S.A.C.I.F."), so this list holds normalised company names and is checked
wherever a company or an email to one goes past: discovery, Scout, and the
Sender's last check before each email.

Matching is by whole words: the excluded "acme foods" blocks "Acme Foods
Argentina S.A." but not "Acmes Foodservice". Erring toward blocking is the
safe side; a customer who gets a cold email is the costly mistake.
"""

import json
import logging
import re
import unicodedata

logger = logging.getLogger("harvey.exclusions")

SETTING_KEY = "excluded_company_names"

# Legal forms and filler that don't tell companies apart.
_LEGAL = frozenset("""
s a sa saic saci sacif sacifia sacifi saca sau srl sas sca sh se
s r l s a s inc llc ltd corp co cia y e the de del la las los el
""".split())


def normalize_company_name(name: str) -> str:
    """'MOLINO EJEMPLO S.A.C.I.F.' → 'molino ejemplo'."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    # Dotted initials are one word: "s.a.c.i.f." → "sacif", "s. r. l." → "srl".
    text = re.sub(r"\b(?:[a-z]\.\s?){2,}[a-z]?\b\.?",
                  lambda m: re.sub(r"[.\s]", "", m.group(0)) + " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w not in _LEGAL]
    return " ".join(words)


def match(company_name: str, excluded: list[str] | set[str]) -> str:
    """The excluded name ``company_name`` matches, or ""."""
    name = normalize_company_name(company_name)
    if not name or not excluded:
        return ""
    padded = f" {name} "
    for entry in excluded:
        if entry and f" {entry} " in padded:
            return entry
    return ""


async def load(state) -> list[str]:
    try:
        raw = await state.get_setting(SETTING_KEY, "[]")
        names = json.loads(raw) if isinstance(raw, str) and raw else []
    except Exception as e:
        logger.warning(f"Exclusions: could not read the list: {e}")
        return []
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n]


async def add(state, names) -> int:
    """Add names to the list. Returns how many were new."""
    current = await load(state)
    seen = set(current)
    added = 0
    for raw in names:
        name = normalize_company_name(raw)
        if name and name not in seen:
            seen.add(name)
            current.append(name)
            added += 1
    await state.set_setting(SETTING_KEY, json.dumps(sorted(current), ensure_ascii=False))
    return added
