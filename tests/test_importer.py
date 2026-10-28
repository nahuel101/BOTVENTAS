"""Importing an existing pipeline from a sales sheet, and the name exclusions.

All companies and people here are fictional (example.com-style data)."""

import csv
import os
import tempfile

import pytest
import pytest_asyncio

from harvey import exclusions
from harvey.importer import (
    contact_status, email_status, import_workbook, outreach_status, read_workbook,
)
from harvey.state import StateManager
from tests.test_outbox_native import (  # noqa: F401
    FakeProvider, make_sender, seed_campaign,
)
from harvey.models.prospect import Prospect


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        yield sm


TABS = {
    "Cuentas": [
        ["Cuenta_ID", "Empresa", "Website", "Scope", "Provincia", "Pais", "Score", "Estado_Cuenta"],
        ["CTA-1", "Alfajores Delta S.A.", "https://www.alfajoresdelta.example/", "Alimentos",
         "Buenos Aires", "Argentina", "92", "CONTACTADA"],
        ["CTA-2", "Cosmética Norte", "https://cosmeticanorte.example", "Cosmética",
         "Córdoba", "Argentina", "80", "APROBADO"],
        ["CTA-3", "Latas del Sur", "https://latasdelsur.example", "Alimentos",
         "", "Argentina", "60", "ENCAJE_NO_VALIDADO"],
        ["CTA-4", "Viejo Cliente SRL", "https://viejocliente.example", "Alimentos",
         "", "Argentina", "90", "EXCLUIDA_POR_NAHUEL"],
    ],
    "Contactos": [
        ["Contacto_ID", "Cuenta_ID", "Empresa", "Nombre", "Apellido", "Cargo", "LinkedIn_URL",
         "Email", "Telefono", "SalesQL_Status", "Estado_Contacto"],
        ["CON-1", "CTA-1", "Alfajores Delta S.A.", "Ana", "Paz", "Jefa de Compras",
         "https://linkedin.com/in/ana-fixture", "apaz@alfajoresdelta.example", "#ERROR!",
         "SALESQL_WORK_VALID", "ENVIADO"],
        ["CON-2", "CTA-1", "Alfajores Delta S.A.", "Bruno", "Gil", "Comprador",
         "https://linkedin.com/in/bruno-fixture", "bgil@alfajoresdelta.example", "",
         "SALESQL_WORK_UNVERIFIABLE_MX_ACTIVO", "ENVIADO"],
        ["CON-3", "CTA-1", "Alfajores Delta S.A.", "Carla", "Ruiz", "Supply Chain",
         "https://linkedin.com/in/carla-fixture", "carla.ruiz@gmail.com", "",
         "PERSONAL_EMAIL_ONLY", "REVISAR_EMAIL_PERSONAL"],
        ["CON-4", "CTA-1", "Alfajores Delta S.A.", "Dario", "Sosa", "Gerente de Compras",
         "https://linkedin.com/in/dario-fixture", "dsosa@alfajoresdelta.example", "",
         "SALESQL_WORK_VALID", "APROBADO"],
    ],
    "Outreach": [
        ["Outreach_ID", "Contacto_ID", "Cuenta_ID", "Empresa", "Nombre", "Cargo", "Email",
         "Estado_Secuencia", "Respondio", "Resultado"],
        ["OUT-1", "CON-1", "CTA-1", "Alfajores Delta S.A.", "Ana", "Jefa de Compras",
         "apaz@alfajoresdelta.example", "EN_CURSO", "No", "APROBADO"],
        ["OUT-2", "CON-2", "CTA-1", "Alfajores Delta S.A.", "Bruno", "Comprador",
         "bgil@alfajoresdelta.example", "REBOTADO", "Si", "REBOTADO"],
    ],
    "Respuestas": [
        ["Respuesta_ID", "Contacto_ID", "Empresa", "Email", "Clasificacion_IA", "Derivado_A"],
        ["RESP-1", "CON-1", "Alfajores Delta S.A.", "apaz@alfajoresdelta.example",
         "DERIVACION", "compras@alfajoresdelta.example"],
    ],
    "Exclusiones": [
        ["Exclusion_ID", "Nombre_Original", "Nombre_Normalizado", "Motivo"],
        ["EXC-1", "PANADERÍA EJEMPLO S.A.C.I.F.", "panaderia ejemplo", "Cliente"],
    ],
}


def _write_csv_folder(folder):
    for tab, rows in TABS.items():
        with open(os.path.join(folder, f"{tab}.csv"), "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)


def _write_xlsx(path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for tab, rows in TABS.items():
        ws = wb.create_sheet(tab)
        for row in rows:
            ws.append(row)
    wb.save(path)


# ── mapping ──

@pytest.mark.parametrize("row, expected", [
    ({"Estado_Secuencia": "EN_CURSO"}, ("contacted", False)),
    ({"Estado_Secuencia": "REBOTADO"}, ("lost", True)),
    ({"Estado_Secuencia": "EN_CURSO", "Resultado": "REBOTADO"}, ("lost", True)),
    ({"Estado_Secuencia": "CERRADO_SIN_RESPUESTA"}, ("lost", False)),
    ({"Estado_Secuencia": "RESPONDIDO_DERIVACION"}, ("replied", False)),
    ({"Estado_Secuencia": "PAUSADO_VACACIONES"}, ("replied", False)),
    ({"Estado_Secuencia": "EN_CURSO", "Respondio": "Sí"}, ("replied", False)),
    ({"Estado_Secuencia": "PENDIENTE_APROBACION"}, ("new", False)),
    ({"Estado_Secuencia": "ALGO_NUEVO"}, ("contacted", False)),   # unknown: never a fresh start
])
def test_outreach_status(row, expected):
    assert outreach_status(row) == expected


@pytest.mark.parametrize("state, expected", [
    ("ENRIQUECIDO", "new"), ("APROBADO", "new"), ("ENVIADO", "contacted"),
    ("REBOTADO", "lost"), ("DESCARTAR", "lost"), ("PAUSADO_DERIVADO", "replied"),
    ("CONTACTO_REFERIDO_DIRECTO", "replied"), ("CONTACTO_IDENTIFICADO", "new"),
])
def test_contact_status(state, expected):
    assert contact_status({"Estado_Contacto": state})[0] == expected


def test_email_status():
    assert email_status("x@acme.example", "SALESQL_WORK_VALID") == ("x@acme.example", "verified")
    assert email_status("x@acme.example", "SalesQL Unverifiable; entrega no confirmada") == (
        "x@acme.example", "risky")
    assert email_status("x@acme.example", "EMAIL_REBOTADO") == ("x@acme.example", "invalid")
    assert email_status("someone@hotmail.com", "PERSONAL_EMAIL_ONLY") == ("", "")


# ── the import ──

async def _by_email(state, email):
    return await state.get_prospect_by_email(email)


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
async def test_import_workbook(state, fmt, tmp_path):
    if fmt == "csv":
        _write_csv_folder(tmp_path)
        path = tmp_path
    else:
        path = tmp_path / "pipeline.xlsx"
        _write_xlsx(path)
    assert set(read_workbook(path)) == set(TABS)

    report = await import_workbook(state, path)

    assert report.companies == 4
    # Ana: emailed, then replied with a referral → replied, Harvey stays out.
    ana = await _by_email(state, "apaz@alfajoresdelta.example")
    assert ana.status == "replied" and ana.email_status == "verified" and ana.phone == ""
    # Bruno bounced → closed, address invalid.
    bruno = await _by_email(state, "bgil@alfajoresdelta.example")
    assert bruno.status == "lost" and bruno.email_status == "invalid"
    # Dario was approved but never emailed → Harvey may write to him.
    dario = await _by_email(state, "dsosa@alfajoresdelta.example")
    assert dario.status == "new" and dario.email_status == "verified"
    # Carla's personal address is not kept; her profile still is (no paid re-lookup).
    assert await _by_email(state, "carla.ruiz@gmail.com") is None
    assert await state.prospect_exists(linkedin_url="https://linkedin.com/in/carla-fixture")
    assert report.personal_emails_dropped == 1
    assert report.referrals == ["Alfajores Delta S.A.: compras@alfajoresdelta.example"]

    names = await exclusions.load(state)
    assert "panaderia ejemplo" in names           # the customer list
    assert "viejo cliente" in names               # an account you excluded
    assert "latas sur" in names                   # not a fit: no more lookups
    assert report.not_a_fit == 1

    # Only the approved account with nobody on file is left for Scout.
    waiting = await state.companies_needing_prospects(limit=10)
    assert [c["domain"] for c in waiting] == ["cosmeticanorte.example"]
    assert report.companies_left_for_prospecting == 1


@pytest.mark.asyncio
async def test_reimport_updates_instead_of_duplicating(state, tmp_path):
    _write_csv_folder(tmp_path)
    await import_workbook(state, tmp_path)
    await import_workbook(state, tmp_path)
    assert len(await state.get_prospects_by_status("lost")) == 1
    assert len(await state.get_prospects_by_status("replied")) == 1
    assert len(await state.get_prospects_by_status("new")) == 2    # Dario, Carla (no email)


# ── exclusions ──

def test_normalize_and_match():
    assert exclusions.normalize_company_name("MOLINO EJEMPLO S.A.C.I.F.") == "molino ejemplo"
    excluded = ["molino ejemplo", "delta"]
    assert exclusions.match("Molino Ejemplo Argentina S.A.", excluded) == "molino ejemplo"
    assert exclusions.match("Alfajores Delta", excluded) == "delta"
    assert exclusions.match("Deltana Foods", excluded) == ""
    assert exclusions.match("", excluded) == ""


@pytest.mark.asyncio
async def test_sender_never_emails_an_excluded_company(state):
    pid = await state.add_prospect(Prospect(
        first_name="Eva", last_name="Luz", title="Compras", company="Panadería Ejemplo S.A.",
        email="eva@panaderiaejemplo.example", email_status="verified", email_verified=True,
    ))
    await seed_campaign(state, [pid])
    await exclusions.add(state, ["PANADERÍA EJEMPLO S.A.C.I.F."])
    sender = make_sender(state, FakeProvider(), require_approval=False)

    await sender._run_native()
    await sender._run_native()

    assert sender.provider.sent == []
