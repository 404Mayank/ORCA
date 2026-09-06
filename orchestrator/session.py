"""Per-session turn history, so a follow-up can inherit spatial context.

CLAUDE.md requires the schema to survive *"a follow-up turn inheriting spatial
context from the previous turn"*. ``apply_inheritance()`` in the planner has
always known how to do that; there was nowhere to keep the previous turn. This
is that place.

**In-memory and per-process, on purpose.** A hackathon demo runs one process
for an afternoon, and a Postgres table for something that must not outlive the
demo is work spent in the wrong place. The interface is small enough that
swapping in Supabase later is one class, and
``core/supabase_client.py`` is where that would go.

Two rules that are not conveniences:

* **Inheritance is recorded, never silent.** ``apply_inheritance`` writes every
  carried-over slot into ``inherited_slots``, and synthesis surfaces those as
  assumptions. A fisherman who asked about Nagapattinam an hour ago and now
  asks "what about tomorrow?" must be told which boat and which place the
  answer assumed. Silent reuse is how a safety answer ends up describing the
  wrong vessel.
* **Only the intent is inherited, never the answer.** Carrying a previous
  verdict forward would let stale conditions leak into a fresh question.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.schemas.intent import Intent

__all__ = ["Turn", "SessionStore", "SESSIONS"]

#: How long a turn stays eligible to be inherited from. Beyond this the
#: context is dropped and the user is asked again.
#:
#: Marine conditions move. Inheriting a two-hour-old "Nagapattinam, FRP boat"
#: is helpful; inheriting yesterday's is a different trip, and asking again
#: costs one question where guessing costs a wrong answer.
CONTEXT_TTL_MINUTES = 90

#: Sessions kept before the oldest is evicted. Bounded so a long-running
#: process cannot grow without limit.
MAX_SESSIONS = 512

#: Turns kept per session. Only the most recent is ever inherited from; the
#: rest are for the trace and for debugging a demo afterwards.
MAX_TURNS_PER_SESSION = 20

#: How long a clarification stays open. Short: a reply five minutes later is
#: answering the question; one an hour later is a new conversation, and reading
#: it as a slot value would attach "Rameswaram" to a question nobody remembers
#: being asked.
PENDING_TTL_MINUTES = 15


@dataclass
class Turn:
    """One question and what it produced."""

    turn_id: str
    query: str
    intent: Intent | None
    state: str
    """answer | clarification | refusal | error, as TurnResult reports it.

    Not the planner's own state vocabulary. These are different alphabets and
    conflating them silently disabled inheritance for every session -- see
    :attr:`inheritable`.
    """
    answer: str = ""
    verdict: str | None = None
    #: On a clarification, which slots were still needed. The user's next turn
    #: is the answer to exactly this.
    missing_slots: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds() / 60.0

    @property
    def inheritable(self) -> bool:
        """Whether this turn may still supply context to a follow-up.

        Only a turn that produced a real answer qualifies. A clarification
        carries no usable slots by definition -- it is the turn where we asked
        *because* slots were missing -- so inheriting from one would propagate
        the gap rather than fill it. A refusal and an error have nothing to
        carry either.

        **This compared against "plan" until 2026-09-06**, which is the
        planner's vocabulary, not the turn's. ``run_turn`` records "answer", so
        the condition was never true and multi-turn context silently never
        worked -- the store filled up, ``context_for`` always returned None, and
        every follow-up was asked from scratch. It failed as a missing feature
        rather than as an error, which is why it survived until a live
        two-question session was actually tried.
        """
        # "error" is included deliberately. A turn that resolved a place and a
        # vessel and then failed in synthesis still established those slots --
        # the user said them, and making them say it again because our
        # assembly step raised is our failure charged to them.
        return (
            self.intent is not None
            and self.state in ("answer", "error")
            and self.age_minutes <= CONTEXT_TTL_MINUTES
        )


class SessionStore:
    """Bounded, thread-safe, in-memory session history."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, list[Turn]] = OrderedDict()
        self._max = max_sessions
        self._lock = threading.Lock()

    def context_for(self, session_id: str | None) -> Intent | None:
        """The intent a follow-up should inherit from, or None.

        None is returned freely -- for an unknown session, an expired turn, or
        a previous turn that was itself a clarification. The planner treats a
        missing context as "ask", which is the correct behaviour whenever we
        are unsure what the user meant.
        """
        if not session_id:
            return None
        with self._lock:
            turns = self._sessions.get(session_id)
            if not turns:
                return None
            for turn in reversed(turns):
                if turn.inheritable:
                    return turn.intent
            return None

    def recent(self, session_id: str | None, limit: int = 3) -> list[tuple[str, str]]:
        """Recent (question, classified type) pairs, oldest first.

        Given to the planner so it can resolve "there" and "the day after".
        Deliberately not the answers: the model needs to know what was *asked*,
        and handing it previous verdicts invites it to repeat one.
        """
        if not session_id:
            return []
        with self._lock:
            turns = self._sessions.get(session_id) or []
            return [
                (t.query, t.intent.query_type.value)
                for t in turns[-limit:]
                if t.intent is not None
            ]

    def conversation(self, session_id: str | None, limit: int = 4) -> list[dict[str, str]]:
        """Recent turns as a dialogue, for conversational replies.

        Distinct from :meth:`recent`, which is for *planning* and deliberately
        withholds the answers -- handing a planner a previous verdict invites it
        to repeat one.

        A chat reply has the opposite need: it cannot refer to what was just
        discussed without being told. So this includes what was said back, but
        **only for turns that produced no verdict** (chat and clarification).
        An answered safety turn still contributes its question alone, for the
        same reason ``recent`` gives only questions.
        """
        if not session_id:
            return []
        with self._lock:
            turns = self._sessions.get(session_id) or []
            out: list[dict[str, str]] = []
            for t in turns[-limit:]:
                entry = {"user": t.query}
                if t.state in ("chat", "clarification") and t.answer:
                    entry["you_replied"] = t.answer[:220]
                elif t.state == "answer":
                    entry["you_replied"] = f"(answered their {t.intent.query_type.value} question)" if t.intent else "(answered)"
                out.append(entry)
            return out

    def pending_question(self, session_id: str | None) -> Turn | None:
        """The clarification this session is still waiting on an answer to.

        Distinct from :meth:`context_for`, and the distinction is the bug this
        fixes. ``context_for`` offers a *completed* turn to inherit from, and it
        deliberately refuses clarifications: the question we asked carries no
        slots.

        But the user's **reply** to that question does. Without somewhere to put
        it, answering "mechanised_trawler" produced a turn with no place, no
        vessel and no query type -- so the same question was asked again, and
        again, forever. Found in the UI on 2026-09-06.
        """
        if not session_id:
            return None
        with self._lock:
            turns = self._sessions.get(session_id)
            if not turns:
                return None
            # Look past chat turns, not just at the last one.
            #
            # "Where can I go fishing?" -> "From which landing centre?" ->
            # "tell me". The reply named no port, so it was planned fresh and
            # came back as small talk -- and the question we had asked
            # evaporated, because only the immediately preceding turn was
            # examined and that turn was now the chat. The user was left in a
            # conversation where we had asked something and then forgotten it.
            # Seen 2026-09-07.
            #
            # A chat turn answers nothing and fills no slot, so it cannot
            # cancel an outstanding question. Anything else -- an answer, a
            # refusal, a fresh clarification -- does.
            last = None
            for turn in reversed(turns):
                if turn.state == "chat":
                    continue
                last = turn
                break
            if last is None or last.state != "clarification" or last.intent is None:
                return None
            # A question from an hour ago is not one the user is still
            # answering; treat a late reply as a fresh request.
            return last if last.age_minutes <= PENDING_TTL_MINUTES else None

    def record(self, session_id: str | None, turn: Turn) -> None:
        if not session_id:
            return
        with self._lock:
            turns = self._sessions.setdefault(session_id, [])
            turns.append(turn)
            del turns[:-MAX_TURNS_PER_SESSION]
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)

    def history(self, session_id: str) -> list[Turn]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def forget(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


#: Process-wide store used by the API.
SESSIONS = SessionStore()
