"""Import an existing outbound pipeline from a spreadsheet.

For a team moving to Harvey from a sales sheet: the companies already
researched, the people already found, who was emailed and what came of it,
and the customers never to cold-email. After the import Harvey knows all of
it, so it never sends a first email to someone already in a conversation,
never pays SalesQL again for a person already looked up, and carries on
finding buyers at researched companies that have none yet.

The workbook is one .xlsx (File → Download → Microsoft Excel), or a folder
with one CSV per tab. Tabs and columns it reads (others are ignored):

  Cuentas       Cuenta_ID, Empresa, Website, Scope, Subscope, Provincia, Pais,
                Score, Tier, Packaging_Detectado, Motivo_Score, Estado_Cuenta
  Contactos     Contacto_ID, Cuenta_ID, Empresa, Nombre, Apellido, Cargo,
                Seniority, LinkedIn_URL, Email, Telefono, SalesQL_Status,
                Estado_Contacto
  Outreach      Contacto_ID, Cuenta_ID, Empresa, Nombre, Cargo, Email,
                Estado_Secuencia, Respondio, Resultado
  Respuestas    Contacto_ID, Email, Clasificacion_IA, Derivado_A
  Exclusiones   Nombre_Normalizado (or Nombre_Original)

Everything is matched by ID first, then by email, so a re-import updates
rather than duplicates. The sheet is the truth: a re-import overwrites the
statuses it sets.
"""

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harvey import exclusions
from harvey.agents.writer import CONTINUATION_KEY, CONTINUED_KEY
from harvey.collectors.discover import normalize_domain
from harvey.models.company import Company
from harvey.models.prospect import Prospect

logger = logging.getLogger("harvey.importer")

SOURCE = "import:sheet"

# Free-mail domains: a personal inbox is never cold-emailed.
FREE_MAIL = frozenset("""
gmail.com googlemail.com hotmail.com hotmail.com.ar outlook.com outlook.com.ar
live.com live.com.ar yahoo.com yahoo.com.ar icloud.com me.com aol.com
protonmail.com proton.me fibertel.com.ar arnet.com.ar speedy.com.ar
""".split())


@dataclass
class ImportReport:
    companies: int = 0
    prospects: int = 0
    statuses: dict = field(default_factory=dict)
    excluded_names: int = 0
    not_a_fit: int = 0
    personal_emails_dropped: int = 0
    companies_left_for_prospecting: int = 0
    referrals: list = field(default_factory=list)
    # Running sequences whose remaining follow-ups Harvey will write.
    continuations: int = 0
    warnings: list = field(default_factory=list)

    def count(self, status: str):
        self.statuses[status] = self.statuses.get(status, 0) + 1


# ── reading the workbook ─────────────────────────────────────────────────

def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.startswith("#") and text.endswith("!") else text   # "#ERROR!"


def read_workbook(path: str | Path) -> dict[str, list[dict]]:
    """{tab name: [row as {column: value}]}."""
    path = Path(path)
    tabs: dict[str, list[dict]] = {}
    if path.is_dir():
        for csv_path in sorted(path.glob("*.csv")):
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                tabs[csv_path.stem] = [
                    {k.strip(): _clean(v) for k, v in row.items() if k}
                    for row in csv.DictReader(f)
                ]
        return tabs
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as e:
            raise RuntimeError(
                "Reading .xlsx needs openpyxl: pip install openpyxl "
                "(or export each tab as CSV into one folder).") from e
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue
            names = [_clean(h) for h in header]
            tabs[ws.title] = [
                {n: _clean(v) for n, v in zip(names, row) if n}
                for row in rows if any(v not in (None, "") for v in row)
            ]
        return tabs
    raise ValueError(f"{path} is not an .xlsx file or a folder of CSV files.")


def _tab(tabs: dict, name: str) -> list[dict]:
    for key, rows in tabs.items():
        if key.strip().lower() == name.lower():
            return rows
    return []


# ── status mapping ───────────────────────────────────────────────────────

def _yes(value: str) -> bool:
    return value.strip().lower() in ("si", "sí", "yes", "true", "1")


def outreach_status(row: dict) -> tuple[str, bool]:
    """(Harvey prospect status, bounced) for an Outreach row.

    Anything not recognised counts as already contacted: unknown means
    "don't send a first email", never "start over"."""
    state = row.get("Estado_Secuencia", "").upper()
    result = row.get("Resultado", "").upper()
    if state.startswith("REBOT") or result.startswith("REBOT"):
        return "lost", True
    if state.startswith(("CERRADO", "EXCLUIDO", "DESCART")):
        return "lost", False
    if state.startswith(("RESPOND", "PAUSADO", "CLIENTE", "CONTACTO_GENERICO")) \
            or _yes(row.get("Respondio", "")):
        return "replied", False
    if state.startswith("PENDIENTE"):
        return "new", False
    return "contacted", False


def contact_status(row: dict) -> tuple[str, bool]:
    """(Harvey prospect status, bounced) for a Contactos row with no Outreach row."""
    state = row.get("Estado_Contacto", "").upper()
    if state.startswith("REBOT"):
        return "lost", True
    if state.startswith(("DESCART", "EXCLUID", "REEMPLAZAR")):
        return "lost", False
    # A referral is a conversation someone opened for you, not a cold lead.
    if state.startswith(("RESPOND", "PAUSADO", "REFERIDO", "CONTACTO_REFERIDO")):
        return "replied", False
    if state.startswith(("ENVIADO", "CONTACTADO")):
        return "contacted", False
    return "new", False


def email_status(email: str, salesql_status: str) -> tuple[str, str]:
    """(email to keep, Harvey email_status). Personal inboxes are dropped."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return "", ""
    if email.rsplit("@", 1)[1] in FREE_MAIL:
        return "", ""
    status = (salesql_status or "").upper()
    if "REBOT" in status or "BOUNCED" in status:
        return email, "invalid"
    if "UNVERIFIABLE" in status:
        return email, "risky"
    return email, "verified"


# When each email of a sequence went out: the first, then follow-ups 1-5.
SENT_DATE_COLUMNS = ("Fecha_Inicial", "FU1", "FU2", "FU3", "FU4", "FU5")


def _as_utc(value: str):
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def sent_dates(row: dict) -> list:
    """The send dates an Outreach row records, oldest first (naive UTC)."""
    dates = [_as_utc(row.get(col, "")) for col in SENT_DATE_COLUMNS if row.get(col)]
    return sorted(d for d in dates if d is not None)


def account_excluded(state: str) -> bool:
    """A customer or a company you said never to contact."""
    state = (state or "").upper()
    return state.startswith(("EXCLUID", "CLIENTE"))


def account_not_a_fit(state: str) -> bool:
    """Researched and found not to buy what you make. Excluded too: Scout
    revisits companies with nobody on file, and each visit spends credits."""
    return (state or "").upper().startswith("ENCAJE_NO_VALIDADO")


# Companies the sheet already finished with: Harvey doesn't look for more
# people there. The rest (candidates, researched, approved) with nobody on
# file are left for Scout to find buyers at.
_ACCOUNT_DONE = ("ENCAJE_NO_VALIDADO", "DESCART", "DUPLICADO", "REEMPLAZADA")


# ── import ───────────────────────────────────────────────────────────────

def _company_from_row(row: dict) -> Company:
    website = row.get("Website", "")
    location = ", ".join(v for v in (row.get("Provincia", ""), row.get("Pais", "")) if v)
    notes = " | ".join(f"{k}: {row[k]}" for k in
                       ("Score", "Tier", "Estado_Cuenta", "Motivo_Score") if row.get(k))
    description = " ".join(v for v in (row.get("Subscope", ""),
                                       row.get("Packaging_Detectado", "")) if v)
    return Company(
        name=row.get("Empresa", ""),
        domain=normalize_domain(website) if website else "",
        website=website,
        industry=row.get("Scope", ""),
        description=description[:1000],
        location=location,
        source=SOURCE,
        source_url=row.get("Fuente", "")[:500],
        external_id=f"sheet:{row['Cuenta_ID']}" if row.get("Cuenta_ID") else "",
        notes=notes[:1000],
    )


async def import_workbook(state, path: str | Path) -> ImportReport:
    tabs = read_workbook(path)
    report = ImportReport()

    # Exclusions first, so nothing below can reach an excluded company.
    names = [r.get("Nombre_Normalizado") or r.get("Nombre_Original", "")
             for r in _tab(tabs, "Exclusiones")]
    names += [r.get("Empresa", "") for r in _tab(tabs, "Cuentas")
              if account_excluded(r.get("Estado_Cuenta", ""))]
    names += [r.get("Empresa", "") for r in _tab(tabs, "Outreach")
              if r.get("Estado_Secuencia", "").upper().startswith("CLIENTE")]
    report.excluded_names = await exclusions.add(state, [n for n in names if n])
    report.not_a_fit = await exclusions.add(state, [
        r.get("Empresa", "") for r in _tab(tabs, "Cuentas")
        if r.get("Empresa") and account_not_a_fit(r.get("Estado_Cuenta", ""))])

    # Companies.
    company_ids: dict[str, str] = {}       # Cuenta_ID → Harvey id
    by_name: dict[str, str] = {}           # normalised name → Harvey id
    done: set[str] = set()
    for row in _tab(tabs, "Cuentas"):
        if not row.get("Empresa"):
            continue
        company = _company_from_row(row)
        cid = await state.add_company(company)
        report.companies += 1
        if row.get("Cuenta_ID"):
            company_ids[row["Cuenta_ID"]] = cid
        by_name[exclusions.normalize_company_name(company.name)] = cid
        account = row.get("Estado_Cuenta", "").upper()
        if account_excluded(account) or account.startswith(_ACCOUNT_DONE):
            done.add(cid)

    def company_of(row: dict) -> str:
        return (company_ids.get(row.get("Cuenta_ID", ""))
                or by_name.get(exclusions.normalize_company_name(row.get("Empresa", "")), ""))

    # People, keyed by Contacto_ID; Outreach and Respuestas refine them.
    people: dict[str, dict] = {}
    for row in _tab(tabs, "Contactos"):
        key = row.get("Contacto_ID") or row.get("Email") or row.get("LinkedIn_URL")
        if not key:
            continue
        status, bounced = contact_status(row)
        people[key] = {"row": row, "status": status, "bounced": bounced}

    for row in _tab(tabs, "Outreach"):
        key = row.get("Contacto_ID") or row.get("Email")
        if not key:
            continue
        status, bounced = outreach_status(row)
        entry = people.setdefault(key, {"row": {
            "Cuenta_ID": row.get("Cuenta_ID", ""), "Empresa": row.get("Empresa", ""),
            "Nombre": row.get("Nombre", ""), "Cargo": row.get("Cargo", ""),
            "Email": row.get("Email", ""),
        }})
        entry["status"], entry["bounced"] = status, bounced
        # A sequence still running: what was sent and when, so Harvey can
        # write the follow-ups still due (see Writer._write_continuations).
        if row.get("Estado_Secuencia", "").upper().startswith("EN_CURSO"):
            dates = sent_dates(row)
            if dates:
                entry["continuation"] = {
                    "dates": [d.isoformat() for d in dates],
                    "subject": row.get("Asunto", ""), "body": row.get("Mensaje", ""),
                }
        if row.get("Email") and not entry["row"].get("Email"):
            entry["row"]["Email"] = row["Email"]

    by_email = {e["row"].get("Email", "").lower(): k for k, e in people.items()
                if e["row"].get("Email")}
    for row in _tab(tabs, "Respuestas"):
        key = row.get("Contacto_ID") or by_email.get(row.get("Email", "").lower(), "")
        entry = people.get(key)
        if entry is None:
            continue
        if row.get("Clasificacion_IA", "").upper().startswith("REBOT"):
            entry["status"], entry["bounced"] = "lost", True
        elif entry["status"] not in ("lost",):
            entry["status"] = "replied"
        if row.get("Derivado_A"):
            report.referrals.append(f"{row.get('Empresa', '')}: {row['Derivado_A']}")

    for entry in people.values():
        row = entry["row"]
        email, e_status = email_status(row.get("Email", ""), row.get("SalesQL_Status", ""))
        if row.get("Email") and not email:
            report.personal_emails_dropped += 1
        if entry["bounced"] and email:
            e_status = "invalid"
        prospect = Prospect(
            first_name=row.get("Nombre", ""),
            last_name=row.get("Apellido", ""),
            title=row.get("Cargo", ""),
            seniority=row.get("Seniority", "").lower(),
            linkedin_url=row.get("LinkedIn_URL", ""),
            email=email,
            email_status=e_status,
            email_verified=(e_status == "verified"),
            phone=row.get("Telefono", ""),
            company=row.get("Empresa", ""),
            company_id=company_of(row),
            source=SOURCE,
            status=entry["status"],
        )
        if not (prospect.email or prospect.linkedin_url):
            if not (prospect.first_name and prospect.company) or await state.prospect_exists(
                    first_name=prospect.first_name, last_name=prospect.last_name,
                    company=prospect.company):
                continue
        pid = await state.add_prospect(prospect)
        # An existing record: the sheet's status and address verdict win.
        await state.update_prospect_status(pid, prospect.status)
        if prospect.email:
            await state.update_prospect_email(pid, prospect.email, prospect.email_status)
        report.prospects += 1
        report.count(prospect.status)
        if (entry.get("continuation") and prospect.status == "contacted"
                and prospect.email_status in ("verified", "risky")
                and not await state.get_setting(CONTINUED_KEY + pid)):
            await state.set_setting(CONTINUATION_KEY + pid,
                                    json.dumps(entry["continuation"], ensure_ascii=False))
            report.continuations += 1
        if prospect.company_id:
            done.add(prospect.company_id)

    for cid in done:
        await state.mark_prospects_checked(cid)
    report.companies_left_for_prospecting = len(set(company_ids.values()) - done)
    return report
