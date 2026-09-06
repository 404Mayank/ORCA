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
        return (
            self.intent is not None
            and self.state == "answer"
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
            last = turns[-1]
            if last.state != "clarification" or last.intent is None:
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
