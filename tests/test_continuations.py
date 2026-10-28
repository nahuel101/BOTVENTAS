"""Sequences another tool started (harvey import) are continued, not restarted.

Someone on follow-up 2 of 5 when the old system is switched off gets the
three still due, a week after the last one went out, as a continuation:
never a second first email. Fictional data only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from harvey.agents.sender import Sender
from harvey.agents.writer import CONTINUATION_KEY, CONTINUED_KEY, Writer
from harvey.config import WriterConfig
from harvey.importer import import_workbook, sent_dates
from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from tests.test_writer_feedback import RecordingBrain, config, state  # noqa: F401
from tests.test_importer import TABS, _write_csv_folder


def _days_ago(n):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=n)).replace(microsecond=0)


def _writer(brain, state):
    cfg = config("gmail")
    cfg.writer = WriterConfig(follow_ups=5, follow_up_days=7)
    w = Writer(brain, state, cfg)
    w.skills = ""
    return w


async def _contacted(state, email="eva@frutasdelvalle.example"):
    return await state.add_prospect(Prospect(
        first_name="Eva", last_name="Luz", title="Jefa de Compras", company="Frutas del Valle",
        email=email, email_status="verified", email_verified=True, status="contacted"))


async def _pending(state, pid, sent_days_ago=(30, 23, 16)):
    info = {"dates": [_days_ago(d).isoformat() for d in sent_days_ago],
            "subject": "estuches para frutas secas", "body": "Hola Eva, ¿quién ve compras?"}
    await state.set_setting(CONTINUATION_KEY + pid, json.dumps(info))


def _drafts(n):
    return [{"subject": f"seguimiento {i}", "body": f"Hola Eva, otro ángulo {i}. ¿Lo vemos?"}
            for i in range(1, n + 1)]


def test_sent_dates_reads_every_format():
    row = {"Fecha_Inicial": "2026-09-14T12:30:14.196Z", "FU1": "2026-09-21",
           "FU2": "2026-09-28T09:00:00-03:00", "FU3": "", "FU4": "no es fecha"}
    assert [d.isoformat() for d in sent_dates(row)] == [
        "2026-09-14T12:30:14.196000", "2026-09-21T00:00:00", "2026-09-28T12:00:00"]


@pytest.mark.asyncio
async def test_writes_only_the_follow_ups_still_due(state):
    pid = await _contacted(state)
    await _pending(state, pid)               # email 1 + 2 follow-ups sent
    brain = RecordingBrain(_drafts(3))

    await _writer(brain, state)._write_continuations()

    prompt = brain.prompts[0]
    assert "already received\n3 email(s)" in prompt
    assert "Write the next 3 email(s): emails 4 to 6" in prompt
    assert "Never introduce the company as if for the\n  first time" in prompt
    assert "Hola Eva, ¿quién ve compras?" in prompt        # the first email, for context

    rows = sorted(await state.get_outbox(), key=lambda r: r["step"])
    assert [(r["step"], r["status"]) for r in rows] == [
        (1, "sent"), (2, "sent"), (3, "sent"),
        (4, "pending_review"), (5, "pending_review"), (6, "pending_review")]
    assert all(r["provider"] == "import" for r in rows[:3])
    assert rows[2]["sent_at"] == _days_ago(16).isoformat()
    # Follow-up 4 is planned a week after the last one sent.
    assert rows[3]["send_at"] == (_days_ago(16) + timedelta(days=7)).isoformat()
    assert rows[3]["subject"] == "seguimiento 1"
    # History never dilutes the bounce rate.
    assert await state.count_outbox_sent() == 0
    assert await state.get_setting(CONTINUATION_KEY + pid) == ""
    assert await state.get_setting(CONTINUED_KEY + pid)


@pytest.mark.asyncio
async def test_the_next_follow_up_waits_for_its_week(state):
    pid = await _contacted(state)
    await _pending(state, pid, sent_days_ago=(10, 3))      # last one 3 days ago
    w = _writer(RecordingBrain(_drafts(4)), state)
    await w._write_continuations()
    sender = Sender.__new__(Sender)
    sender.state, sender.config = state, w.config
    sender.config.send_window = None
    item = next(r for r in await state.get_outbox() if r["step"] == 3)
    assert await sender._follow_up_order(item) == "wait"   # due in 4 more days


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["replied", "lost", "opted_out"])
async def test_nothing_is_written_once_they_are_out_of_the_sequence(state, status):
    pid = await _contacted(state)
    await state.update_prospect_status(pid, status)
    await _pending(state, pid)
    brain = RecordingBrain(_drafts(3))
    await _writer(brain, state)._write_continuations()
    assert brain.prompts == [] and await state.get_outbox() == []
    assert await state.get_setting(CONTINUATION_KEY + pid) == ""


@pytest.mark.asyncio
async def test_a_finished_sequence_gets_nothing(state):
    pid = await _contacted(state)
    await _pending(state, pid, sent_days_ago=(42, 35, 28, 21, 14, 7))   # all 6 sent
    brain = RecordingBrain(_drafts(1))
    await _writer(brain, state)._write_continuations()
    assert brain.prompts == []


@pytest.mark.asyncio
async def test_a_short_or_templated_draft_is_not_used(state):
    pid = await _contacted(state)
    await _pending(state, pid)
    for reply in (_drafts(2), [{"subject": "x", "body": "Hola {{first_name}}"}] * 3):
        await _writer(RecordingBrain(reply), state)._write_continuations()
    assert await state.get_outbox() == []
    assert await state.get_setting(CONTINUATION_KEY + pid)    # tried again next cycle


@pytest.mark.asyncio
async def test_heartbeat_writes_when_only_continuations_are_pending(state):
    pid = await _contacted(state)
    await _pending(state, pid)
    assert await decide_next_action(None, state, config("gmail")) == "write_campaign"


@pytest.mark.asyncio
async def test_import_queues_running_sequences_once(state, tmp_path):
    tabs = {k: [list(r) for r in v] for k, v in TABS.items()}
    tabs["Outreach"][0] += ["Asunto", "Mensaje", "Fecha_Inicial", "FU1"]
    for row in tabs["Outreach"][1:]:
        row += ["", "", "", ""]
    # Dario: emailed, one follow-up, no reply.
    tabs["Outreach"].append(["OUT-3", "CON-4", "CTA-1", "Alfajores Delta S.A.", "Dario",
                             "Gerente de Compras", "dsosa@alfajoresdelta.example", "EN_CURSO",
                             "No", "APROBADO", "estuches", "Hola Dario.", "2026-10-01", "2026-10-08"])
    import tests.test_importer as ti
    original = ti.TABS
    ti.TABS = tabs
    try:
        _write_csv_folder(tmp_path)
    finally:
        ti.TABS = original

    report = await import_workbook(state, tmp_path)
    assert report.continuations == 1
    dario = await state.get_prospect_by_email("dsosa@alfajoresdelta.example")
    info = json.loads(await state.get_setting(CONTINUATION_KEY + dario.id))
    assert info["dates"] == ["2026-10-01T00:00:00", "2026-10-08T00:00:00"]
    assert info["body"] == "Hola Dario."

    # Once Harvey has picked it up, a re-import doesn't queue it again.
    await state.delete_setting(CONTINUATION_KEY + dario.id)
    await state.set_setting(CONTINUED_KEY + dario.id, "c1")
    assert (await import_workbook(state, tmp_path)).continuations == 0
