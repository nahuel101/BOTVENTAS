"""SMTP/IMAP on a mailbox the team uses every day.

Reading never marks mail read, a reply someone opened first is still seen,
sent mail is filed in Sent, and only bounces of Harvey's own emails count
toward the kill switch. Fictional data only.
"""

from datetime import datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

import harvey.integrations.smtp_mail as smtp_mod
from harvey.integrations.mail_provider import InboundMessage, looks_like_bounce
from harvey.integrations.smtp_mail import SmtpImapProvider
from tests.test_outbox_native import (  # noqa: F401
    FakeProvider, _now_iso, make_handler, seed_prospect, state,
)


def _raw(sender, subject, body, message_id):
    msg = EmailMessage()
    msg["From"], msg["Subject"], msg["Message-ID"] = sender, subject, message_id
    msg.set_content(body)
    return msg.as_bytes()


class FakeImap:
    """Just enough IMAP: one inbox, a folder list, and a log of every command."""
    instances = []

    def __init__(self, host, port):
        self.commands = []
        self.inbox = {}          # uid → raw message
        self.appended = []
        self.folders = [b'(\\HasNoChildren) "." "INBOX"',
                        b'(\\HasNoChildren \\Sent) "." "INBOX.Enviados"']
        self.untagged_responses = {"UIDVALIDITY": [b"7"]}
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.commands.append(("LOGIN",))

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(sorted(self.inbox))]
        uid = args[0]
        return "OK", [(b"1 (BODY[] {n}", self.inbox[uid]), b")"]

    def list(self):
        return "OK", self.folders

    def append(self, mailbox, flags, when, message):
        self.appended.append((mailbox, flags, message))

    def logout(self):
        pass


@pytest.fixture
def imap(monkeypatch):
    server = FakeImap("", 0)
    monkeypatch.setattr(smtp_mod.imaplib, "IMAP4_SSL", lambda host, port: server)
    return server


def _provider(tmp_path=None):
    env = SimpleNamespace(smtp_host="mail.acme.example", smtp_port=587, smtp_username="ana@acme.example",
                          smtp_password="x", imap_host="", imap_port=993, imap_username="",
                          imap_password="")
    config = SimpleNamespace(persona=SimpleNamespace(name="Ana", email="ana@acme.example"))
    provider = SmtpImapProvider(config, env)
    if tmp_path is not None:
        provider.last_read_file = tmp_path / "last_read.txt"
    return provider


def _since(imap):
    search = next(c for c in imap.commands if c[0] == "SEARCH")
    return datetime.strptime(search[search.index("SINCE") + 1], "%d-%b-%Y")


def test_reads_back_to_the_last_handled_read_after_a_week_off(imap, tmp_path):
    provider = _provider(tmp_path)
    provider._fetch_unseen(50)
    # First run: the default lookback.
    assert (datetime.now() - _since(imap)).days in (3, 4)
    provider.confirm_read()

    # Off for nine days: the next read reaches back to the last one.
    week_ago = datetime.now() - timedelta(days=9)
    provider.last_read_file.write_text(week_ago.isoformat(), encoding="utf-8")
    imap.commands.clear()
    _provider(tmp_path)._fetch_unseen(50)
    assert _since(imap).date() == (week_ago - timedelta(days=1)).date()


def test_the_marker_only_moves_when_everything_was_read(imap, tmp_path):
    imap.inbox = {str(i).encode(): _raw(f"p{i}@x.example", "Re", "hola", f"<{i}@x>")
                  for i in range(10, 13)}
    provider = _provider(tmp_path)
    provider._fetch_unseen(2)          # three new, two read: the rest next time
    provider.confirm_read()
    assert not provider.last_read_file.exists()
    provider._fetch_unseen(2)
    provider.confirm_read()
    assert provider.last_read_file.exists()


def test_reading_never_marks_mail_read_and_finds_opened_replies(imap, tmp_path):
    imap.inbox = {b"10": _raw("eva@cliente.example", "Re: estuches", "Me interesa.", "<r1@x>")}
    provider = _provider(tmp_path)

    first = provider._fetch_unseen(50)

    assert [m.from_email for m in first] == ["eva@cliente.example"]
    assert ("SELECT", "INBOX", True) in imap.commands            # read-only
    search = next(c for c in imap.commands if c[0] == "SEARCH")
    assert "SINCE" in search and "UNSEEN" not in search          # opened replies too
    assert ("FETCH", b"10", "(BODY.PEEK[])") in imap.commands     # never sets \Seen

    # The next poll fetches only what's new.
    imap.inbox[b"11"] = _raw("leo@otro.example", "Re: cajas", "Hablemos.", "<r2@x>")
    imap.commands.clear()
    assert [m.from_email for m in provider._fetch_unseen(50)] == ["leo@otro.example"]
    assert [c for c in imap.commands if c[0] == "FETCH"] == [("FETCH", b"11", "(BODY.PEEK[])")]


def test_sent_mail_is_filed_in_the_sent_folder(imap):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "ana@acme.example", "eva@cliente.example", "hola"
    msg.set_content("Hola Eva.")
    _provider()._save_to_sent(msg)
    [(mailbox, flags, raw)] = imap.appended
    assert mailbox == "INBOX.Enviados" and flags == "\\Seen" and b"Hola Eva." in raw


def test_sent_folder_by_name_when_none_is_flagged(imap):
    imap.folders = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent Items"']
    msg = EmailMessage()
    msg.set_content("x")
    _provider()._save_to_sent(msg)
    assert imap.appended[0][0] == '"Sent Items"'


def test_delivery_report_names_the_original_and_the_address():
    report = (
        b"From: MAILER-DAEMON@mail.acme.example\r\nSubject: Mensaje no entregado\r\n"
        b"Message-ID: <dsn1@mail.acme.example>\r\nContent-Type: text/plain\r\n\r\n"
        b"No se pudo entregar.\r\n\r\nFinal-Recipient: rfc822; eva@cliente.example\r\n"
        b"Action: failed\r\n\r\nMessage-ID: <harvey-1@acme.example>\r\nSubject: hola\r\n"
    )
    parsed = SmtpImapProvider._parse_rfc822(report)
    assert parsed.is_bounce
    assert parsed.in_reply_to == "<harvey-1@acme.example>"
    assert "eva@cliente.example" in parsed.body


@pytest.mark.parametrize("subject", ["Mensaje no entregado", "Undelivered Mail Returned to Sender",
                                     "Error de entrega del mensaje"])
def test_spanish_and_postfix_bounce_subjects(subject):
    assert looks_like_bounce("someone@acme.example", subject)


async def _sent(state, pid, email, n=1, message_id=""):
    for i in range(n):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s", body="b", send_at=_now_iso(),
            status="approved", campaign_id=f"c{email}{i}", step=1)
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso(),
                                       message_id=message_id if i == 0 else "")


def _bounce(body, in_reply_to=""):
    return InboundMessage(provider_id=f"b-{body[:10]}", from_email="mailer-daemon@mail.acme.example",
                          subject="Mensaje no entregado", body=body, in_reply_to=in_reply_to,
                          is_bounce=True)


@pytest.mark.asyncio
async def test_a_bounce_of_the_teams_own_mail_is_not_counted(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid, "jane@acme.com", n=11)
    provider = FakeProvider()
    provider.inbound = [_bounce("Final-Recipient: proveedor@otro.example")]
    await make_handler(state, provider)._run_native()
    assert await state.get_setting("bounce_count") == ""
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_a_bounce_matched_by_address_is_counted(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid, "jane@acme.com", n=11)
    provider = FakeProvider()
    provider.inbound = [_bounce("Final-Recipient: jane@acme.com")]
    await make_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert await state.get_setting("bounce_count") == "1"


@pytest.mark.asyncio
async def test_a_bounce_of_mail_sent_before_harvey_is_not_counted(state):
    pid = await seed_prospect(state, status="contacted")
    oid = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s", body="b", send_at=_now_iso(),
        status="sent", campaign_id="old", step=1, provider="import")
    await state.update_outbox_item(oid, sent_at=_now_iso())
    provider = FakeProvider()
    provider.inbound = [_bounce("Final-Recipient: jane@acme.com")]
    await make_handler(state, provider)._run_native()
    assert await state.get_setting("bounce_count") == ""
