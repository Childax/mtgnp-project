"""
server/engine/turn_manager.py

Implements TURN-01 through TURN-05 per RFC 0001 Section 7.

- TURN-01: full ordered phase/step state machine
- TURN-02: Untap step (untap permanents, clear sickness, reset land flag)
- TURN-03: first-player no-draw rule + empty-library loss
- TURN-04: PLAY_LAND legality (main phase, active player, empty stack, 1 per turn)
- TURN-05: Cleanup discard loop + end-of-turn cleanup
"""

from __future__ import annotations
from typing import Callable, Optional

from shared.pdu import (
    Phase, PDUType, ErrorCode, build_phase_transition, build_game_state_update,
    build_error,
)
from server.core.game_state import GameState, Permanent, build_permanent_from_catalog
from server.engine.priority import PriorityManager, PriorityError


class TurnManager:
    def __init__(self, state: GameState,
                 send_fn: Callable[[str, dict], None],
                 broadcast_fn: Callable[[dict], None],
                 on_game_over: Callable[[str, str, str], None],
                 first_player_id: str,
                 card_catalog: Optional[dict] = None):
        self.state = state
        self.send_fn = send_fn
        self.broadcast_fn = broadcast_fn
        self.on_game_over = on_game_over
        if card_catalog is None:
            from shared.pdu import CARD_CATALOG
            card_catalog = CARD_CATALOG
        self.card_catalog = card_catalog

        self.first_player_id = first_player_id
        self.state.active_player = first_player_id
        self.state.turn = 0  # Incremented to 1 when the first Untap runs

        # Combat-only substep flags, read by combat.py
        self.any_attackers_declared = False
        self.any_multi_blocked = False
        self.any_first_or_double_strike = False

        # Registry for temporary "until end of turn" cleanup callbacks (e.g., Giant Growth)
        self.cleanup_hooks: list[Callable[[], None]] = []

        self.priority = PriorityManager(
            state=state,
            send_fn=send_fn,
            broadcast_fn=broadcast_fn,
            on_step_advance=self._advance_step,
            on_stack_resolve_needed=self._resolve_top_of_stack_placeholder,
        )

        self._stack_resolver: Optional[Callable[[], None]] = None
        self._combat_hooks: dict[str, Callable] = {}
        self._awaiting_discard: bool = False

    def wire_dependencies(self, stack_resolver: Callable[[], None],
                           combat_hooks: dict) -> None:
        """
        stack_resolver: StackManager.resolve_top() -- called when both
            players pass with a non-empty stack.
        combat_hooks: dict of callables from CombatManager, keyed by
            step name, e.g. {"DECLARE_ATTACKERS": combat.begin_declare_attackers, ...}
        """
        self._stack_resolver = stack_resolver
        self._combat_hooks = combat_hooks

    def register_cleanup_hook(self, hook: Callable[[], None]) -> None:
        """Allows external modules (e.g. card effects) to clear until-end-of-turn state."""
        self.cleanup_hooks.append(hook)

    def _resolve_top_of_stack_placeholder(self) -> None:
        if self._stack_resolver is None:
            raise RuntimeError(
                "StackManager not wired into TurnManager -- call wire_dependencies() first"
            )
        self._stack_resolver()

    # -- Public Entry Point --------------------------------------------

    def start_turn(self) -> None:
        """Begin a new turn at the Untap Step (RFC 7.2)."""
        self.state.turn += 1
        prev_phase = self.state.phase
        self.state.phase = Phase.UNTAP
        self._broadcast_phase_transition(prev_phase, Phase.UNTAP)
        self._run_untap_step()

    # -- Step Implementations --------------------------------------------

    def _run_untap_step(self) -> None:
        """TURN-02: Untap Step. No priority window (RFC 7.2)."""
        ap = self.state.players[self.state.active_player]
        for permanent in ap.battlefield:
            permanent.tapped = False
            if permanent.is_creature:
                permanent.summoning_sick = False
        ap.land_played_this_turn = False

        self.broadcast_fn(build_game_state_update(
            self.state.next_seq(),
            self.state.to_personalized_dict(self.state.active_player),
        ))

        self._transition_to(Phase.UPKEEP)

    def _run_draw_step_entry(self) -> None:
        """
        TURN-03: Draw Step entry. First player's first turn does NOT
        draw (RFC 7.4). Empty-library draw is a loss (RFC 6.5, 8.4).
        """
        is_first_turn_for_ap = (
            self.state.turn == 1
            and self.state.active_player == self.first_player_id
        )
        if is_first_turn_for_ap:
            # Skip draw, still open priority window
            self._open_priority_for_current_step()
            return

        ap = self.state.players[self.state.active_player]
        if not ap.library:
            # Drawing from empty library is loss
            opponent = self.state.opponent_of(self.state.active_player)
            self._end_game(winner_id=opponent, loser_id=self.state.active_player,
                            reason="DECK_EMPTY")
            return

        drawn = ap.library.pop(0)
        ap.hand.append(drawn)

        self.send_fn(self.state.active_player, build_game_state_update(
            self.state.next_seq(),
            self.state.to_personalized_dict(self.state.active_player),
        ))
        self._open_priority_for_current_step()

    def run_cleanup_step(self) -> None:
        """
        TURN-05: Cleanup Step (RFC 7.8).
        1. If active player's hand > 7, request DISCARD until <= 7.
        2. Clear all damage marked on creatures and run registered EOT hooks.
        3. Switch active player and start next turn.
        """
        ap_id = self.state.active_player
        ap = self.state.players[ap_id]

        if len(ap.hand) > 7:
            self.send_fn(ap_id, build_game_state_update(
                self.state.next_seq(), self.state.to_personalized_dict(ap_id),
            ))
            self._awaiting_discard = True
            return

        self._finish_cleanup()

    def handle_discard(self, player_id: str, pdu: dict) -> None:
        """Client response to cleanup discard request (RFC 7.8)."""
        ap = self.state.players[player_id]
        card_ids = pdu.get("card_ids", [])

        for cid in card_ids:
            if cid not in ap.hand:
                self.reject_illegal_action(
                    player_id, pdu,
                    f"Card {cid} is not in {player_id}'s hand.",
                )
                return

        for cid in card_ids:
            ap.hand.remove(cid)
            ap.graveyard.append(cid)

        self.broadcast_fn(build_game_state_update(
            self.state.next_seq(), self.state.to_personalized_dict(player_id),
        ))

        if len(ap.hand) > 7:
            self.send_fn(player_id, build_game_state_update(
                self.state.next_seq(), self.state.to_personalized_dict(player_id),
            ))
            return

        self._awaiting_discard = False
        self._finish_cleanup()

    def _finish_cleanup(self) -> None:
        # Clear marked creature damage
        for ps in self.state.players.values():
            for permanent in ps.battlefield:
                permanent.damage = 0

        # Run external end-of-turn cleanup hooks (e.g., Giant Growth)
        for hook in self.cleanup_hooks:
            hook()

        self.broadcast_fn(build_game_state_update(
            self.state.next_seq(),
            self.state.to_personalized_dict(self.state.active_player),
        ))

        next_active = self.state.opponent_of(self.state.active_player)
        self.state.active_player = next_active
        self.start_turn()

    def reject_illegal_action(self, player_id: str, pdu: dict, message: str,
                               code: str = ErrorCode.ILLEGAL_ACTION) -> None:
        self.send_fn(player_id, build_error(
            self.state.next_seq(), code, message,
            rejected_action={"type": pdu.get("type"), "seq_num": pdu.get("seq_num")},
        ))

    def _end_game(self, winner_id: str, loser_id: str, reason: str) -> None:
        self.state.game_over = True
        self.on_game_over(winner_id, loser_id, reason)

    # -- Phase Sequencing (TURN-01) ---------------------------------------

    def _open_priority_for_current_step(self) -> None:
        self.priority.open_window()

    def _advance_step(self) -> None:
        """Called by PriorityManager when both players pass on an empty stack."""
        current = self.state.phase
        idx = Phase.ORDER.index(current)

        if current == Phase.DECLARE_ATTACKERS and not self.any_attackers_declared:
            self._transition_to(Phase.END_OF_COMBAT)
            return

        if current == Phase.DECLARE_BLOCKERS and not self.any_multi_blocked:
            if self.any_first_or_double_strike:
                self._transition_to(Phase.FIRST_STRIKE_DAMAGE)
            else:
                self._transition_to(Phase.COMBAT_DAMAGE)
            return

        if current == Phase.ASSIGN_DAMAGE_ORDER and not self.any_first_or_double_strike:
            self._transition_to(Phase.COMBAT_DAMAGE)
            return

        if current == Phase.FIRST_STRIKE_DAMAGE and not self.any_first_or_double_strike:
            self._transition_to(Phase.COMBAT_DAMAGE)
            return

        if idx + 1 >= len(Phase.ORDER):
            self.run_cleanup_step()
            return

        self._transition_to(Phase.ORDER[idx + 1])

    def _transition_to(self, to_phase: str) -> None:
        from_phase = self.state.phase
        self.state.phase = to_phase
        self._broadcast_phase_transition(from_phase, to_phase)

        if to_phase == Phase.UPKEEP:
            self._open_priority_for_current_step()
        elif to_phase == Phase.DRAW:
            self._run_draw_step_entry()
        elif to_phase in (Phase.PRECOMBAT_MAIN, Phase.POSTCOMBAT_MAIN):
            self._open_priority_for_current_step()
        elif to_phase == Phase.BEGIN_COMBAT:
            self.any_attackers_declared = False
            self.any_multi_blocked = False
            self.any_first_or_double_strike = False
            self._open_priority_for_current_step()
        elif to_phase in self._combat_hooks:
            self._combat_hooks[to_phase]()
        elif to_phase in (Phase.END_OF_COMBAT, Phase.END_STEP):
            self._open_priority_for_current_step()
        elif to_phase == Phase.CLEANUP:
            self.run_cleanup_step()

    def _broadcast_phase_transition(self, from_phase: Optional[str], to_phase: str) -> None:
        pdu = build_phase_transition(
            self.state.next_seq(), from_phase or "", to_phase,
            self.state.active_player, self.state.turn,
        )
        self.broadcast_fn(pdu)

    # -- PLAY_LAND (TURN-04) ------------------------------------------------

    def handle_play_land(self, player_id: str, pdu: dict) -> None:
        """Enforces main phase, active player, empty stack, and 1-land limit."""
        if player_id != self.state.active_player:
            self.reject_illegal_action(
                player_id, pdu, "Only the Active Player may play a land.",
                code=ErrorCode.ILLEGAL_ACTION,
            )
            return

        if self.state.phase not in Phase.MAIN_PHASES:
            self.reject_illegal_action(
                player_id, pdu, "Lands may only be played during a Main Phase.",
                code=ErrorCode.WRONG_PHASE,
            )
            return

        if not self.state.is_stack_empty():
            self.reject_illegal_action(
                player_id, pdu, "Lands can only be played when the stack is empty.",
                code=ErrorCode.ILLEGAL_ACTION,
            )
            return

        ap = self.state.players[player_id]
        if ap.land_played_this_turn:
            self.reject_illegal_action(
                player_id, pdu, "Only one land may be played per turn.",
                code=ErrorCode.ILLEGAL_ACTION,
            )
            return

        card_id = pdu.get("card_id")
        if card_id not in ap.hand:
            self.reject_illegal_action(
                player_id, pdu, f"Card {card_id} is not in hand.",
                code=ErrorCode.ILLEGAL_ACTION,
            )
            return

        ap.hand.remove(card_id)
        # Uses the catalog-aware builder so this land actually carries a
        # mana_produced value (RFC lands imply mana; previously this
        # created a bare Permanent that could never pay for anything).
        land = build_permanent_from_catalog(card_id, self.card_catalog, summoning_sick=False)
        ap.battlefield.append(land)
        ap.land_played_this_turn = True

        self.broadcast_fn(build_game_state_update(
            self.state.next_seq(), self.state.to_personalized_dict(player_id),
        ))
        
        # Active Player retains priority after playing a land (RFC 7.5)
        self.priority.reopen_after_stack_action(player_id)