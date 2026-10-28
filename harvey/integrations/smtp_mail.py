"""Generic SMTP + IMAP mailbox provider.

Works with any real mailbox: AgentMail (their IMAP/SMTP relay), Fastmail,
a Google Workspace account with an app password, or self-hosted mail.

.env:
    SMTP_HOST=            IMAP_HOST=
    SMTP_PORT=587         IMAP_PORT=993
    SMTP_USERNAME=        IMAP_USERNAME=   (defaults to SMTP_USERNAME)
    SMTP_PASSWORD=        IMAP_PASSWORD=   (defaults to SMTP_PASSWORD)

Sends as the configured persona (name + email), and files a copy of each
sent email in the mailbox's Sent folder (IMAP_SENT_FOLDER, or the folder
the server marks \\Sent), so the team sees it like any other email.

Reading never changes the mailbox: it looks at the inbox since the last
read Harvey finished (a day of overlap), or the last IMAP_LOOKBACK_DAYS (3)
the first time, without marking anything read, so it works on a mailbox
people use every day. A computer that was off for a week still catches up
on that week. A reply someone already opened is still seen; the Handler
skips what it has handled before.
"""

import asyncio
import email
import email.policy
import imaplib
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

import aiosmtplib

from harvey.paths import PROJECT_ROOT
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)

logger = logging.getLogger("harvey.smtp")

# When the last fully handled read started, so the next one reaches back to
# it however long Harvey was off.
LAST_READ_FILE = PROJECT_ROOT / "data" / "imap_last_read.txt"

DEFAULT_LOOKBACK_DAYS = 3
# Folder names servers use for sent mail when they don't flag one \Sent.
SENT_FOLDER_NAMES = ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages",
                     "Enviados", "INBOX.Enviados", "Elementos enviados")
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) "?(?P<sep>[^" ]*)"? (?P<name>.+)$')


class SmtpImapProvider(MailProvider):
    name = "smtp"

    def __init__(self, config, env):
        self.config = config
        self.smtp_host = getattr(env, "smtp_host", "")
        self.smtp_port = int(getattr(env, "smtp_port", 587) or 587)
        self.smtp_user = getattr(env, "smtp_username", "")
        self.smtp_pass = getattr(env, "smtp_password", "")
        self.imap_host = getattr(env, "imap_host", "") or self.smtp_host
        self.imap_port = int(getattr(env, "imap_port", 993) or 993)
        self.imap_user = getattr(env, "imap_username", "") or self.smtp_user
        self.imap_pass = getattr(env, "imap_password", "") or self.smtp_pass
        self.sent_folder = os.getenv("IMAP_SENT_FOLDER", "").strip()
        try:
            self.lookback_days = max(1, int(os.getenv("IMAP_LOOKBACK_DAYS", "") or DEFAULT_LOOKBACK_DAYS))
        except ValueError:
            self.lookback_days = DEFAULT_LOOKBACK_DAYS
        # UIDs already returned by this process: each poll fetches only new mail.
        self._seen_uids: set[bytes] = set()
        self._uidvalidity = b""
        self.last_read_file = LAST_READ_FILE
        self._pending_read: datetime | None = None

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        persona = self.config.persona
        msg = EmailMessage()
        msg["To"] = to_email
        msg["From"] = f"{persona.name} <{persona.email or self.smtp_user}>"
        msg["Subject"] = subject
        message_id = make_msgid(domain=(persona.email or self.smtp_user).split("@")[-1])
        msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_pass,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465),
                timeout=30,
            )
        except Exception as e:
            logger.error(f"SMTP send failed to {to_email}: {e}")
            return SendResult(ok=False, error=str(e)[:300])
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._save_to_sent, msg)
        except Exception as e:   # sent all the same: a missing copy isn't a failure
            logger.warning(f"Sent, but couldn't file a copy in the Sent folder: {e}")
        return SendResult(ok=True, message_id=message_id, thread_ref=message_id)

    def _imap(self):
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        conn.login(self.imap_user, self.imap_pass)
        return conn

    def _find_sent_folder(self, conn) -> str:
        if self.sent_folder:
            return self.sent_folder
        status, rows = conn.list()
        names = []
        for raw in rows or [] if status == "OK" else []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            m = _LIST_RE.match(line)
            if not m:
                continue
            name = m.group("name").strip().strip('"')
            if "\\sent" in m.group("flags").lower():
                return name
            names.append(name)
        for candidate in SENT_FOLDER_NAMES:
            for name in names:
                if name.lower() == candidate.lower():
                    return name
        return ""

    def _save_to_sent(self, msg: EmailMessage) -> None:
        conn = self._imap()
        try:
            folder = self._find_sent_folder(conn)
            if not folder:
                logger.warning("No Sent folder found; set IMAP_SENT_FOLDER in .env to keep copies.")
                return
            mailbox = f'"{folder}"' if " " in folder else folder
            conn.append(mailbox, "\\Seen", imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes())
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._fetch_unseen, limit)
        except Exception as e:
            logger.error(f"IMAP fetch failed: {e}")
            return []

    def _fetch_unseen(self, limit: int) -> list[InboundMessage]:
        """Recent inbox mail not yet returned by this process, oldest first.

        Read with BODY.PEEK, so nothing is marked read, and by date rather
        than by the unread flag, so a reply someone already opened isn't
        missed. More than ``limit`` new messages: the rest come next poll."""
        out: list[InboundMessage] = []
        conn = self._imap()
        try:
            status, data = conn.select("INBOX", readonly=True)
            validity = b""
            try:
                validity = (conn.untagged_responses.get("UIDVALIDITY") or [b""])[0]
            except Exception:
                pass
            if validity != self._uidvalidity:
                self._seen_uids, self._uidvalidity = set(), validity
            started = datetime.now(timezone.utc).replace(tzinfo=None)
            since = started - timedelta(days=self.lookback_days)
            last = self._last_read()
            if last is not None:
                since = min(since, last - timedelta(days=1))
            status, data = conn.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
            if status != "OK":
                return []
            uids = data[0].split() if data and data[0] else []
            new = [u for u in uids if u not in self._seen_uids]
            # Only a read that got everything may move the marker forward.
            self._pending_read = started if len(new) <= limit else None
            new = new[:limit]
            for uid in new:
                self._seen_uids.add(uid)
                status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if status != "OK" or raw is None:
                    continue
                parsed = self._parse_rfc822(raw)
                if parsed:
                    out.append(parsed)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _last_read(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.last_read_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def confirm_read(self) -> None:
        """Everything the last read returned was handled: the next read can
        start from it (with a day's overlap)."""
        if self._pending_read is None:
            return
        try:
            self.last_read_file.parent.mkdir(parents=True, exist_ok=True)
            self.last_read_file.write_text(self._pending_read.isoformat(), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Couldn't save the last-read time: {e}")
        self._pending_read = None

    @staticmethod
    def _parse_rfc822(raw: bytes) -> InboundMessage | None:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
        from_email = parseaddr(str(msg.get("From", "")))[1].lower()
        if not from_email:
            return None
        subject = str(msg.get("Subject", ""))

        body = ""
        try:
            part = msg.get_body(preferencelist=("plain", "html"))
            if part is not None:
                body = part.get_content()
                if part.get_content_type() == "text/html":
                    body = re.sub(r"<[^>]+>", " ", body)
        except Exception:
            pass

        message_id = str(msg.get("Message-ID", "")).strip()
        is_bounce = looks_like_bounce(from_email, subject)
        in_reply_to = str(msg.get("In-Reply-To", "")).strip()
        if is_bounce:
            # A delivery report names the failed address and carries the
            # original's headers: enough to tie it to the email Harvey sent.
            text = raw.decode("utf-8", errors="replace")
            recipient = re.search(r"(?im)^(?:Final|Original)-Recipient:\s*rfc822;\s*<?([^>\s]+@[^>\s]+)", text)
            if recipient:
                body = f"{body}\nFinal-Recipient: {recipient.group(1)}"
            if not in_reply_to:
                original = re.findall(r"(?im)^Message-ID:\s*(<[^>\s]+>)", text)
                original = [m for m in original if m != message_id]
                if original:
                    in_reply_to = original[-1]
        return InboundMessage(
            provider_id=message_id or f"{from_email}:{subject}",
            from_email=from_email,
            subject=subject,
            body=(body or "")[:5000],
            message_id=message_id,
            in_reply_to=in_reply_to,
            thread_ref=str(msg.get("In-Reply-To", "")).strip(),
            date=str(msg.get("Date", "")),
            is_bounce=is_bounce,
        )

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD missing from .env"
        # SMTP handshake
        try:
            smtp = aiosmtplib.SMTP(
                hostname=self.smtp_host, port=self.smtp_port,
                start_tls=(self.smtp_port == 587),
                use_tls=(self.smtp_port == 465), timeout=15,
            )
            await smtp.connect()
            await smtp.login(self.smtp_user, self.smtp_pass)
            await smtp.quit()
        except Exception as e:
            return False, f"SMTP login failed: {str(e)[:200]}"
        # IMAP handshake
        loop = asyncio.get_event_loop()

        def _imap_check():
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            try:
                conn.login(self.imap_user, self.imap_pass)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _imap_check)
        except Exception as e:
            return False, f"SMTP OK but IMAP login failed: {str(e)[:200]}"
        return True, f"SMTP + IMAP connected as {self.smtp_user}"
