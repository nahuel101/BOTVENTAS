"""SalesQL: the buyer's LinkedIn profile → their work email at the company.

All people and companies here are fictional (example.com-style data)."""

import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import harvey.agents.scout as scout_mod
from harvey.agents.scout import Scout
from harvey.collectors.discover import serper_locale
from harvey.integrations import salesql
from harvey.state import StateManager


def person(**kw):
    base = {
        "first_name": "Lucía",
        "last_name": "Ferreyra",
        "title": "Jefa de Compras",
        "emails": [
            {"email": "lucia.f@hotmail.example", "type": "Direct", "status": "Valid"},
            {"email": "lferreyra@oldjob.example", "type": "Work", "status": "Valid"},
            {"email": "lferreyra@cosmeticosdelsur.example", "type": "Work", "status": "Valid"},
        ],
        "phones": [{"phone": "+54 11 5555-0000", "type": "Work", "is_valid": True}],
        "organization": {"name": "Cosméticos del Sur S.A.",
                         "website_domain": "cosmeticosdelsur.example"},
        "work_experience": [
            {"title": "Jefa de Compras", "is_current": True,
             "organization": {"name": "Cosméticos del Sur S.A.",
                              "website_domain": "cosmeticosdelsur.example"}},
            {"title": "Compradora", "is_current": False,
             "organization": {"name": "Old Job", "website_domain": "oldjob.example"}},
        ],
    }
    return {**base, **kw}


# ── picking the address ──

def test_only_a_work_address_at_the_company_is_used():
    assert salesql.company_email(person(), "cosmeticosdelsur.example") == (
        "lferreyra@cosmeticosdelsur.example", "verified")


def test_personal_and_former_employer_addresses_are_never_used():
    p = person(emails=[
        {"email": "lucia.f@hotmail.example", "type": "Direct", "status": "Valid"},
        {"email": "lferreyra@oldjob.example", "type": "Work", "status": "Valid"},
    ])
    assert salesql.company_email(p, "cosmeticosdelsur.example") == ("", "")


def test_unconfirmed_address_is_risky_and_invalid_is_dropped():
    p = person(emails=[
        {"email": "bad@cosmeticosdelsur.example", "type": "Work", "status": "Invalid"},
        {"email": "lferreyra@cosmeticosdelsur.example", "type": "Work", "status": "Unverifiable"},
    ])
    assert salesql.company_email(p, "cosmeticosdelsur.example") == (
        "lferreyra@cosmeticosdelsur.example", "risky")


def test_subdomain_address_counts_as_the_company():
    p = person(emails=[{"email": "lf@ar.cosmeticosdelsur.example", "type": "Work", "status": "Valid"}])
    assert salesql.company_email(p, "www.cosmeticosdelsur.example")[0] == "lf@ar.cosmeticosdelsur.example"


# ── who works there now ──

def test_works_at_by_domain_and_not_at_a_former_employer():
    assert salesql.works_at(person(), "cosmeticosdelsur.example", "Cosméticos del Sur")
    assert not salesql.works_at(person(), "oldjob.example", "Old Job")


def test_works_at_by_name_when_salesql_has_no_domain():
    p = person(organization={"name": "Cosméticos del Sur"}, work_experience=[])
    assert salesql.works_at(p, "cosmeticosdelsur.example", "Cosmeticos del Sur S.A.")
    assert not salesql.works_at(p, "otra.example", "Alimentos Pampa")


def test_current_title_comes_from_the_current_job():
    assert salesql.current_title(person(title="")) == "Jefa de Compras"


def test_search_result_must_name_the_company():
    assert salesql.mentions_company(
        "Lucía Ferreyra - Jefa de Compras - Cosméticos del Sur | LinkedIn",
        "Cosméticos del Sur S.A.")
    assert not salesql.mentions_company(
        "Pedro Gómez - Jefe de Compras - Alimentos Pampa | LinkedIn",
        "Cosméticos del Sur S.A.")


# ── the API ──

def _client(status, body=None):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer fixture-key"
        assert request.url.params["linkedin_url"] == "https://linkedin.com/in/fixture"
        return httpx.Response(status, json=body if body is not None else {})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_enrich_returns_the_person():
    async with _client(200, person()) as c:
        got = await salesql.enrich("https://linkedin.com/in/fixture", "fixture-key", c)
    assert got["last_name"] == "Ferreyra"


@pytest.mark.asyncio
async def test_enrich_not_found_is_none():
    async with _client(404) as c:
        assert await salesql.enrich("https://linkedin.com/in/fixture", "fixture-key", c) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 429])
async def test_enrich_stops_when_salesql_refuses(status):
    async with _client(status) as c:
        with pytest.raises(salesql.SalesQLError):
            await salesql.enrich("https://linkedin.com/in/fixture", "fixture-key", c)


# ── daily budget ──

@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        yield sm


@pytest.mark.asyncio
async def test_daily_budget_counts_down(state, monkeypatch):
    monkeypatch.setenv("SALESQL_DAILY_LOOKUPS", "2")
    assert await salesql.lookups_remaining(state) == 2
    await salesql.record_lookup(state)
    await salesql.record_lookup(state)
    assert await salesql.lookups_remaining(state) == 0


# ── Scout ──

def _scout(state, results, people):
    scout = Scout.__new__(Scout)
    scout.state = state
    scout.env = SimpleNamespace(salesql_api_key="fixture-key", serper_api_key="fixture-serper")
    scout.config = SimpleNamespace(icp=SimpleNamespace(
        titles=["Jefe de Compras", "Gerente de Compras"], geography=["Argentina"]))
    scout._seen_prospect_keys = set()
    scout._salesql_searches = 0
    scout._salesql_off = False
    scout.searches = []

    async def fetch(url, **kw):
        scout.searches.append(kw["json_body"])
        return httpx.Response(200, json={"organic": results})
    scout._fetch = fetch

    async def enrich(url, key, client=None):
        return people.get(url)
    return scout, enrich


@pytest.mark.asyncio
async def test_scout_finds_the_buyer_and_keeps_their_work_email(state, monkeypatch):
    results = [
        {"link": "https://ar.linkedin.com/in/lucia-fixture",
         "title": "Lucía Ferreyra - Jefa de Compras - Cosméticos del Sur | LinkedIn", "snippet": ""},
        {"link": "https://ar.linkedin.com/in/someone-else",
         "title": "Pedro Gómez - Jefe de Compras - Alimentos Pampa | LinkedIn", "snippet": ""},
    ]
    scout, enrich = _scout(state, results, {"https://ar.linkedin.com/in/lucia-fixture": person()})
    monkeypatch.setattr(scout_mod.salesql, "enrich", enrich)

    found = await scout._contacts_via_salesql(
        "c1", "cosmeticosdelsur.example", "Cosméticos del Sur S.A.", "Cosmética")

    assert [(p.email, p.email_status, p.title, p.source) for p in found] == [
        ("lferreyra@cosmeticosdelsur.example", "verified", "Jefa de Compras", "salesql")]
    assert found[0].phone == "+54 11 5555-0000"
    # Searched Google Argentina, in Spanish, for the configured roles.
    body = scout.searches[0]
    assert body["gl"] == "ar" and body["hl"] == "es"
    assert body["q"].startswith("site:linkedin.com/in cosmeticos sur")
    assert '"Jefe de Compras" OR "Gerente de Compras"' in body["q"]
    # Only the result naming the company cost a lookup.
    assert await salesql.lookups_remaining(state) == salesql.SALESQL_DEFAULT_DAILY_LOOKUPS - 1


@pytest.mark.asyncio
async def test_scout_skips_someone_who_left_the_company(state, monkeypatch):
    results = [{"link": "https://ar.linkedin.com/in/lucia-fixture",
                "title": "Lucía Ferreyra - Cosméticos del Sur | LinkedIn", "snippet": ""}]
    moved = person(organization={"name": "Otra", "website_domain": "otra.example"},
                   work_experience=[])
    scout, enrich = _scout(state, results, {"https://ar.linkedin.com/in/lucia-fixture": moved})
    monkeypatch.setattr(scout_mod.salesql, "enrich", enrich)
    assert await scout._contacts_via_salesql(
        "c1", "cosmeticosdelsur.example", "Cosméticos del Sur S.A.", "Cosmética") == []


@pytest.mark.asyncio
async def test_scout_stops_calling_salesql_when_it_refuses(state, monkeypatch):
    results = [{"link": "https://ar.linkedin.com/in/lucia-fixture",
                "title": "Lucía Ferreyra - Cosméticos del Sur | LinkedIn", "snippet": ""}]
    scout, _ = _scout(state, results, {})

    async def refuse(url, key, client=None):
        raise salesql.SalesQLError("HTTP 402")
    monkeypatch.setattr(scout_mod.salesql, "enrich", refuse)
    assert await scout._contacts_via_salesql(
        "c1", "cosmeticosdelsur.example", "Cosméticos del Sur S.A.", "Cosmética") == []
    assert not scout._salesql_ready()


def test_salesql_needs_both_keys():
    scout = Scout.__new__(Scout)
    scout.env = SimpleNamespace(salesql_api_key="k", serper_api_key="")
    assert not scout._salesql_ready()
    scout.env = None
    assert not scout._salesql_ready()


def test_serper_locale():
    assert serper_locale("Córdoba, Argentina") == {"gl": "ar", "hl": "es"}
    assert serper_locale("Denver, CO") == {}
