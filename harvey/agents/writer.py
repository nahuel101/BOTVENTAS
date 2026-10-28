"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from harvey import exclusions
from harvey.brain import Brain
from harvey.config import MAX_FOLLOW_UPS, HarveyConfig
from harvey.humanize import humanize, humanizer_enabled
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import WRITABLE_EMAIL_STATUSES, StateManager
from harvey.sending_mode import sync_sending_mode
from harvey.startups import startup_facts
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.writer")

# How many times the model may fail to write a usable sequence for a
# prospect before Harvey stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
# Imported sequences (harvey import): the history of what another tool
# already sent, waiting for the Writer to write the follow-ups still due.
# "continued:" remembers a sequence already picked up, so a re-import
# never starts it twice.
CONTINUATION_KEY = "continuation:"
CONTINUED_KEY = "continued:"
CONTINUATIONS_PER_CYCLE = 5
IMPORT_PROVIDER = "import"
HISTORY_PLACEHOLDER = "(Seguimiento enviado antes de Harvey; su texto no se importó.)"

NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        if self.is_native:
            await self._write_continuations()

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            self._no_reply = False
            sequence = await self._write_sequence(prospects)
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    # ── Imported sequences: write the follow-ups still due ──

    async def _write_continuations(self):
        """Continue sequences another tool started (harvey import).

        The emails already sent go into the outbox as sent, on their real
        dates, so the Sender's own follow-up spacing counts from the last
        one. Only the follow-ups still due are written, as a continuation of
        that conversation, never as a first contact."""
        pending = await self.state.settings_with_prefix(
            CONTINUATION_KEY, limit=CONTINUATIONS_PER_CYCLE)
        if not pending:
            return
        excluded = await exclusions.load(self.state)
        delays = self._delays()
        for key, raw in pending:
            pid = key[len(CONTINUATION_KEY):]
            try:
                info = json.loads(raw)
                dates = [datetime.fromisoformat(d) for d in info.get("dates") or []]
            except (ValueError, TypeError):
                await self.state.delete_setting(key)
                continue
            prospect = await self.state.get_prospect(pid)
            sent = len(dates)
            done_reason = (
                "gone" if prospect is None
                else f"status {prospect.status}" if prospect.status != "contacted"
                else "no deliverable address" if not prospect.email
                or (prospect.email_status or "guess") not in WRITABLE_EMAIL_STATUSES
                else "sequence complete" if not sent or sent >= len(delays)
                else "excluded company" if exclusions.match(prospect.company, excluded)
                else "colleague replied" if await self.state.colleague_who_replied(pid)
                else "")
            if done_reason:
                logger.info(f"Writer: no follow-ups to continue for {pid}: {done_reason}.")
                await self.state.delete_setting(key)
                continue

            remaining = len(delays) - sent
            self._no_reply = False
            follow_ups = await self._write_follow_ups(prospect, info, dates, remaining)
            if not follow_ups:
                if getattr(self.brain, "paused_reason", ""):
                    return       # the provider is down: every one would fail
                await self._count_failed_continuation(key, prospect)
                continue
            await self._stage_continuation(prospect, info, dates, follow_ups, delays)
            await self.state.delete_setting(key)
            await self.state.delete_setting(WRITE_ATTEMPTS_KEY + pid)

    async def _count_failed_continuation(self, key: str, prospect):
        attempts_key = WRITE_ATTEMPTS_KEY + prospect.id
        try:
            attempts = int(await self.state.get_setting(attempts_key) or 0) + 1
        except ValueError:
            attempts = 1
        if attempts >= MAX_WRITE_ATTEMPTS:
            await self.state.delete_setting(key)
            await self.state.delete_setting(attempts_key)
            logger.warning(f"Writer: gave up continuing {prospect.email} after "
                           f"{MAX_WRITE_ATTEMPTS} unusable drafts.")
        else:
            await self.state.set_setting(attempts_key, str(attempts))

    async def _write_follow_ups(self, prospect, info: dict, dates: list, remaining: int):
        sent = len(dates)
        total = sent + remaining
        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += await self._feedback_block([prospect.id])
        first = f"Subject: {info.get('subject', '')}\n\n{info.get('body', '')}".strip()
        prompt += f"""

{prospect.full_name()} ({prospect.title} at {prospect.company}) already received
{sent} email(s) from {self.config.persona.name}: the first one, shown below, and
{sent - 1} follow-up(s). The last went out on {max(dates):%Y-%m-%d}. No reply yet.
{fence(first, "first email already sent")}

{UNTRUSTED_RULE}

Write the next {remaining} email(s): emails {sent + 1} to {total} of this sequence.
- They continue that conversation. Never introduce the company as if for the
  first time, and never repeat the first email's points or its question.
- Each takes a different angle, with one concrete proof point or useful detail
  from the product info. Under 75 words, one question.
- Email {total} is the last: a break-up, under 40 words, gracious.
- Write the actual text (no merge variables: you know their name and company).
- Subject: lowercase, 2-5 words. Never "Re:" or "Fwd:".
- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.{self._signature_rule()}
- Follow every STRICT EMAIL RULE above.

Return ONLY a JSON array of {remaining} objects: [{{"subject": "...", "body": "..."}}]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer", agent="writer", task="continue_sequence")
        if isinstance(result, dict):
            for k in ("emails", "sequence", "steps"):
                if isinstance(result.get(k), list):
                    result = result[k]
                    break
        if not isinstance(result, list):
            return []
        drafts = []
        for raw in result[:remaining]:
            if not isinstance(raw, dict):
                return []
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body or "{{" in subject + body:
                return []
            drafts.append({"subject": subject[:120], "body": body[:2000]})
        return drafts if len(drafts) == remaining else []

    async def _stage_continuation(self, prospect, info: dict, dates: list,
                                  follow_ups: list, delays: list):
        sent = len(dates)
        history = [
            EmailStep(step=i, delay_days=delays[i - 1],
                      subject=info.get("subject") or "(primer correo)" if i == 1 else "(seguimiento)",
                      body=(info.get("body") or HISTORY_PLACEHOLDER) if i == 1 else HISTORY_PLACEHOLDER)
            for i in range(1, sent + 1)
        ]
        new_steps = [EmailStep(step=sent + n, delay_days=delays[sent + n - 1], **draft)
                     for n, draft in enumerate(follow_ups, 1)]
        campaign = Campaign(
            id="", name=f"continuación: {prospect.company or prospect.email}",
            channel="email", sequence=history + new_steps,
            prospect_ids=[prospect.id], status="active",
        )
        campaign_id = await self.state.add_campaign(campaign)

        for step, when in zip(history, sorted(dates)):
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id, to_email=prospect.email, campaign_id=campaign_id,
                step=step.step, subject=step.subject, body=step.body,
                send_at=when.isoformat(), status="sent", provider=IMPORT_PROVIDER)
            if item_id:
                await self.state.update_outbox_item(item_id, sent_at=when.isoformat())

        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(self.config.channels.email, "require_approval", True)
        when = max(dates)
        for step in new_steps:
            when = when + timedelta(days=step.delay_days)
            await self.state.add_outbox_item(
                prospect_id=prospect.id, to_email=prospect.email, campaign_id=campaign_id,
                step=step.step, subject=step.subject, body=step.body,
                send_at=when.isoformat(),
                status="pending_review" if require_approval else "approved",
                approved_by="" if require_approval else "autopilot")
        await self.state.set_setting(CONTINUED_KEY + prospect.id, campaign_id)
        await self.state.log_action(
            action_type="continue_sequence", agent="writer",
            details={"prospect": prospect.email, "campaign_id": campaign_id,
                     "already_sent": sent, "written": len(new_steps)})
        logger.info(f"Writer: continued {prospect.email}: {sent} sent before Harvey, "
                    f"{len(new_steps)} follow-up(s) written.")

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if draft:
                pending.append((prospect, draft))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        drafted = 0
        for (prospect, _), draft in zip(pending, polished):
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see harvey.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK — notes the person running Harvey left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(prospect.company_id)
        if startup is not None:
            facts.extend(startup)

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += await self._feedback_block([prospect.id])

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it — do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
- Under 75 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words.
- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.{self._signature_rule()}
- Follow every STRICT EMAIL RULE above.{inbox_rule}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write the sequence: email 1 plus writer.follow_ups."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables — every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])

        delays = self._delays()
        total = len(delays)
        lines = ["- Email 1: Personalized cold observation + one question. Under 75 words. No pitch."]
        for n in range(2, total + 1):
            if n == total:
                lines.append(f"- Email {n}: Break-up email {delays[n - 1]} days after the one before. "
                             "Under 40 words. Gracious, not guilt-tripping.")
            else:
                lines.append(f"- Email {n}: Follow-up {delays[n - 1]} days after the one before. "
                             "A different angle from every earlier email, one concrete proof "
                             "point or useful detail. Under 75 words.")
        example = ",\n".join(
            f'  {{"step": {n}, "subject": "...", "body": "...", "delay_days": {d}}}'
            for n, d in enumerate(delays, 1))

        prompt += f"""

Write a {total}-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
{chr(10).join(lines)}
{merge_rule}
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words.
- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends.{self._signature_rule()}
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
{example}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result)
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        for step, draft in zip(steps, polished):
            step.subject, step.body = draft["subject"], draft["body"]
        return steps

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:1 + MAX_FOLLOW_UPS]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        # The configured cadence wins over whatever gaps the model wrote, and
        # a longer sequence than asked for is cut, never sent.
        delays = self._delays()
        steps = steps[:len(delays)]
        if self._writer_setting("follow_up_days", 0):
            for step, delay in zip(steps, delays):
                step.delay_days = delay

        if len(steps) < len(delays):
            logger.warning(f"Writer: Expected {len(delays)} emails, got {len(steps)}.")
        return steps

    def _writer_setting(self, name: str, default):
        return getattr(getattr(self.config, "writer", None), name, default)

    def _delays(self) -> list[int]:
        """Days before each email: 0 for email 1, then one gap per follow-up.

        writer.follow_up_days sets every gap; without it Harvey's own
        cadence is kept (3 days, then 4, then 4 more)."""
        follow_ups = max(0, min(int(self._writer_setting("follow_ups", 2)), MAX_FOLLOW_UPS))
        days = int(self._writer_setting("follow_up_days", 0) or 0)
        gaps = [days] * follow_ups if days else [3] + [4] * max(follow_ups - 1, 0)
        return [0] + gaps[:follow_ups]

    def _signature_rule(self) -> str:
        if (self._writer_setting("signature", "") or "").strip():
            return ("\n- No sign-off, name or signature: Harvey adds the sender's "
                    "signature when it sends.")
        return ""

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{industry}-{kind}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
