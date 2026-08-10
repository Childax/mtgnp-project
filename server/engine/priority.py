"""
server/engine/priority.py

Implements PRI-01, PRI-02, PRI-03 per RFC 0001 Section 8.1-8.2, 8.5.

- PRI-01: PRIORITY_GRANT tokens + response deadline
- PRI-02: Consecutive-pass tracking -> resolve stack or advance step
- PRI-03: Reject stale seq_num / wrong-priority actions

This module owns the priority sub-state-machine (RFC Figure 5) but does
NOT own stack resolution itself -- it calls back into StackManager
(STK-02) when both players pass with a non-empty stack, and signals the
TurnManager (TURN-01) when both players pass with an empty stack so the
step can advance.
"""

from __future__ import annotations
from typing import Callable, Optional

from shared.pdu import (
    PDUType, ErrorCode, build_priority_grant, build_error,
)
from server.core.game_state import GameState


class PriorityError(Exception):
    """Raised when a client action fails priority/seq_num validation."""
    def __init__(self, code: str, message: str, rejected_action: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rejected_action = rejected_action


class PriorityManager:
    """
    Manages a single priority window's lifecycle:
      grant -> (cast/activate, retains priority) -> pass -> other player
      grant -> pass (both consecutive, stack empty) -> step advances
      grant -> pass (both consecutive, stack non-empty) -> resolve top item,
               active player gets priority again

    send_fn(player_id, pdu) and broadcast_fn(pdu) are injected so this
    module has no direct network dependency (Richmond's tcp_server.py
    plugs in underneath).
    """

    def __init__(self, state: GameState,
                 send_fn: Callable[[str, dict], None],
                 broadcast_fn: Callable[[dict], None],
                 on_step_advance: Callable[[], None],
                 on_stack_resolve_needed: Callable[[], None],
                 time_limit_ms: int = 60000):
        self.state = state
        self.send_fn = send_fn
        self.broadcast_fn = broadcast_fn
        self.on_step_advance = on_step_advance
        self.on_stack_resolve_needed = on_stack_resolve_needed
        self.time_limit_ms = time_limit_ms

        # The seq_num of the most recently issued PRIORITY_GRANT (or
        # corresponding server request PDU) -- this is the token clients
        # must echo back (RFC 5.4).
        self._current_priority_seq: Optional[int] = None
        # Tracks whether the last action in this window was a pass, and
        # by whom, to detect "both players passed consecutively" (RFC 8.1).
        self._last_pass_by: Optional[str] = None

    # -- RFC 8.2: STEP_BEGIN -> grant priority to Active Player first ----

    def open_window(self) -> None:
        """
        Opens a new priority window for the current step. Per RFC 8.1
        rule 1, the Active Player receives priority first.
        """
        self._last_pass_by = None
        self.state.priority_holder = self.state.active_player
        self._grant(self.state.active_player)

    def _grant(self, player_id: str) -> None:
        seq = self.state.next_seq()
        self._current_priority_seq = seq
        self.state.priority_holder = player_id
        pdu = build_priority_grant(seq, player_id, self.time_limit_ms)
        self.send_fn(player_id, pdu)

    def reissue_current_priority(self, player_id: str) -> None:
        """
        Re-send the current PRIORITY_GRANT without creating
        a new priority token.
        """
        if (
            player_id != self.state.priority_holder
            or self._current_priority_seq is None
        ):
            return

        pdu = build_priority_grant(
            self._current_priority_seq,
            player_id,
            self.time_limit_ms
        )

        self.send_fn(player_id, pdu)

    def reopen_after_stack_action(self, actor_id: str) -> None:
        """
        RFC 8.1 rule 3: when a player casts a spell or activates an
        ability, that player retains priority. Call this after a
        successful CAST_SPELL / ACTIVATE_ABILITY to re-grant.
        """
        self._last_pass_by = None
        self._grant(actor_id)

    def reopen_after_resolution(self) -> None:
        """
        RFC 8.4 step 4: after a stack item resolves, the Active Player
        receives priority again.
        """
        self._last_pass_by = None
        self._grant(self.state.active_player)

    # -- Validation (PRI-03) ----------------------------------------------

    def validate_action(self, player_id: str, pdu: dict,
                         is_priority_bearing: bool = True) -> None:
        """
        Raises PriorityError if the action is not legal to accept right
        now. Priority-bearing PDUs (RFC 5.4 list) must:
          1. come from the player who currently holds priority
             (NOT_YOUR_PRIORITY), and
          2. echo the current priority token's seq_num (STALE_ACTION).

        CONCEDE and PING are exempt per RFC 5.4 and should not be routed
        through this check.
        """
        if not is_priority_bearing:
            return

        if player_id != self.state.priority_holder:
            raise PriorityError(
                ErrorCode.NOT_YOUR_PRIORITY,
                f"Player {player_id} does not hold priority.",
                rejected_action={"type": pdu.get("type"), "seq_num": pdu.get("seq_num")},
            )

        seq = pdu.get("seq_num")
        if seq != self._current_priority_seq:
            raise PriorityError(
                ErrorCode.STALE_ACTION,
                f"Priority token mismatch. Expected seq_num "
                f"{self._current_priority_seq}, got {seq}.",
                rejected_action={"type": pdu.get("type"), "seq_num": seq},
            )

    def reject(self, player_id: str, err: PriorityError) -> None:
        """
        RFC Section 11: send ERROR, discard the action, and if the
        player still holds priority, re-issue PRIORITY_GRANT with the
        SAME seq_num so they can retry.
        """
        error_pdu = build_error(
            self.state.next_seq(), err.code, err.message, err.rejected_action,
        )
        self.send_fn(player_id, error_pdu)

        if player_id == self.state.priority_holder:
            self.reissue_current_priority(player_id)

    # -- Passing (PRI-02) --------------------------------------------------

    def handle_pass(self, player_id: str) -> None:
        """
        RFC 8.1 rules 4-6. Called after validate_action() has already
        confirmed this PRIORITY_PASS is legal.
        """
        if self._last_pass_by is None:
            # First pass in this window -- give priority to the other
            # player (RFC rule 4).
            self._last_pass_by = player_id
            other = self.state.opponent_of(player_id)
            self._grant(other)
            return

        if self._last_pass_by != player_id:
            # Both players passed consecutively (RFC rules 5-6).
            if self.state.is_stack_empty():
                self._last_pass_by = None
                self._current_priority_seq = None
                self.state.priority_holder = None
                self.on_step_advance()
            else:
                self._last_pass_by = None
                self.on_stack_resolve_needed()
        else:
            # Same player passed twice in a row without the state
            # resetting in between -- shouldn't happen if callers reset
            # _last_pass_by correctly, but guard defensively.
            self._grant(self.state.opponent_of(player_id))
