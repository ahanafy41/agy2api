"""Map the stateless OpenAI chat protocol onto stateful agy conversations.

SillyTavern (and any OpenAI client) resends the FULL message history every
request. Sending all of it to agy each turn makes every prompt huge and slow,
which is exactly what trips the upstream ~connection-timeout on long chats.

This store recognises a continuing chat by matching the incoming message list
against histories we have already forwarded: if the stored history is a prefix
of the incoming one, the chat is a continuation and only the *new* turn needs to
be sent to the existing agy conversation (via `--conversation <id>`). agy keeps
the rest in its own memory.

Experimental, opt-in (AGY2API_STATEFUL). Pure in-memory; not persisted.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field


def fingerprint(role: str, text: str) -> str:
    # blake2s (not SHA-1): stronger, collision-resistant identity for matching
    # chats. 16-byte digest is plenty for this in-memory keyspace.
    return hashlib.blake2s(
        f"{role}\n{text}".encode("utf-8", "replace"), digest_size=16
    ).hexdigest()


@dataclass
class _Session:
    conversation_id: str
    sigs: list[str]
    last_used: float = field(default_factory=time.time)


@dataclass(frozen=True)
class Plan:
    """How to handle a request: resume an existing conversation and send only
    `new_indices` messages, or (conversation_id=None) start fresh with all."""

    conversation_id: str | None
    prefix_len: int  # how many leading messages agy already has


class SessionStore:
    def __init__(self, max_sessions: int = 200) -> None:
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        # Clamp to >=1 so a misconfigured AGY2API_MAX_SESSIONS (0/negative) can't
        # make _evict delete every session (including the one just added).
        self._max = max(1, max_sessions)

    def lookup(self, sigs: list[str]) -> Plan:
        """Find the session whose stored sigs are the LONGEST prefix of `sigs`.
        Returns a fresh Plan (conversation_id=None) if none qualifies."""
        best: _Session | None = None
        for sess in self._sessions.values():
            n = len(sess.sigs)
            if n < len(sigs) and sigs[:n] == sess.sigs:
                if best is None or n > len(best.sigs):
                    best = sess
        if best is None:
            return Plan(conversation_id=None, prefix_len=0)
        best.last_used = time.time()
        self._sessions.move_to_end(best.conversation_id)
        return Plan(conversation_id=best.conversation_id, prefix_len=len(best.sigs))

    def remember(
        self, conversation_id: str, sigs: list[str], protected: set[str] | None = None
    ) -> list[str]:
        """Record the full message history now covered by `conversation_id`.
        Returns conversation_ids evicted (caller should delete their DBs).
        `protected` ids are never evicted (e.g. conversations currently in
        flight in other requests)."""
        self._sessions[conversation_id] = _Session(conversation_id, list(sigs))
        self._sessions.move_to_end(conversation_id)
        return self._evict(protected or set())

    def forget(self, conversation_id: str) -> None:
        self._sessions.pop(conversation_id, None)

    def all_ids(self) -> set[str]:
        """Return all active conversation IDs tracked in the store."""
        return set(self._sessions.keys())

    def _evict(self, protected: set[str]) -> list[str]:
        evicted: list[str] = []
        if len(self._sessions) <= self._max:
            return evicted

        # OrderedDict maintains insertion/access order; oldest entries are at the front
        for cid in list(self._sessions.keys()):
            if len(self._sessions) <= self._max:
                break
            if cid not in protected:
                self._sessions.pop(cid, None)
                evicted.append(cid)
        return evicted
