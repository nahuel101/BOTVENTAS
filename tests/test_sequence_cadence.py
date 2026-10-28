"""writer.follow_ups / follow_up_days / signature: the sequence the operator chose.

A team with its own tested cadence (five follow-ups, a week apart) keeps it;
Harvey's default (3 emails: day 0, +3, +4) is unchanged without the settings.
"""

from types import SimpleNamespace

import pytest

from harvey.agents.sender import Sender
from harvey.agents.writer import Writer
from harvey.config import ComplianceConfig, WriterConfig
from tests.test_writer_feedback import RecordingBrain, config, prospect, state  # noqa: F401


def _writer(brain, state, **writer_settings):
    cfg = config("gmail")
    cfg.writer = WriterConfig(**writer_settings)
    w = Writer(brain, state, cfg)
    w.skills = ""
    return w


def _steps(n, delay=2):
    return [{"step": i, "subject": f"s{i}", "body": f"Hola {{{{first_name}}}}, paso {i}.",
             "delay_days": 0 if i == 1 else delay} for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_default_is_harveys_three_emails(state):
    brain = RecordingBrain(_steps(3))
    w = _writer(brain, state)
    steps = await w._write_sequence([await state.get_prospect(await prospect(state, "Ana"))])
    assert "Write a 3-email" in brain.prompts[0]
    assert '"delay_days": 3' in brain.prompts[0] and '"delay_days": 4' in brain.prompts[0]
    assert [s.delay_days for s in steps] == [0, 2, 2]     # model's gaps kept without a setting


@pytest.mark.asyncio
async def test_five_follow_ups_a_week_apart(state):
    brain = RecordingBrain(_steps(6))
    w = _writer(brain, state, follow_ups=5, follow_up_days=7)
    steps = await w._write_sequence([await state.get_prospect(await prospect(state, "Bea"))])
    prompt = brain.prompts[0]
    assert "Write a 6-email" in prompt
    assert "- Email 6: Break-up email 7 days after" in prompt
    assert '{"step": 6, "subject": "...", "body": "...", "delay_days": 7}' in prompt
    assert "{{" not in prompt.split("Return ONLY")[1].replace("{{first_name}}", "")
    # The configured cadence wins over the gaps the model wrote.
    assert [s.delay_days for s in steps] == [0, 7, 7, 7, 7, 7]


@pytest.mark.asyncio
async def test_a_longer_sequence_than_asked_is_cut(state):
    brain = RecordingBrain(_steps(6))
    w = _writer(brain, state, follow_ups=2, follow_up_days=5)
    steps = await w._write_sequence([await state.get_prospect(await prospect(state, "Cid"))])
    assert [s.delay_days for s in steps] == [0, 5, 5]


@pytest.mark.asyncio
async def test_signature_tells_the_writer_not_to_sign(state):
    brain = RecordingBrain(_steps(3))
    w = _writer(brain, state, signature="Saludos,\nAna\nAcme")
    await w._write_sequence([await state.get_prospect(await prospect(state, "Dan"))])
    assert "No sign-off, name or signature" in brain.prompts[0]


def test_follow_ups_are_capped():
    with pytest.raises(ValueError):
        WriterConfig(follow_ups=6)


def _sender(signature):
    sender = Sender.__new__(Sender)
    sender.config = SimpleNamespace(
        writer=WriterConfig(signature=signature),
        compliance=ComplianceConfig(opt_out_line='Respondé "no, gracias".'),
    )
    return sender


def test_signature_goes_above_the_footer_and_on_replies():
    sender = _sender("Saludos,\nAna\nAcme\nacme.example")
    cold = sender._outgoing_body({"kind": "sequence", "body": "Hola Eva, ¿quién ve compras?"})
    assert cold == ("Hola Eva, ¿quién ve compras?\n\nSaludos,\nAna\nAcme\nacme.example"
                    '\n\nRespondé "no, gracias".')
    reply = sender._outgoing_body({"kind": "reply", "body": "Gracias Eva."})
    assert reply == "Gracias Eva.\n\nSaludos,\nAna\nAcme\nacme.example"
    # Not added twice.
    assert sender._with_signature(reply) == reply


def test_no_signature_changes_nothing():
    assert _sender("")._outgoing_body({"kind": "reply", "body": "Gracias."}) == "Gracias."
