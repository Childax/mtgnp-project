"""
server/engine/stack_manager.py

Implements STK-01, STK-01b, STK-02, STK-03 per RFC 0001 Section 8.3, 8.4, 8.1.

- STK-01: CAST_SPELL timing (sorcery speed vs instant), mana payment, & target validation
- STK-01b: ACTIVATE_ABILITY validation (tap-cost check, targets, stack push)
- STK-02: server-side LIFO stack, STACK_PUSH / STACK_RESOLVE
- STK-03: state-based actions (SBAs) after every game event
"""

from __future__ import annotations
from typing import Callable, Optional
import itertools

from shared.pdu import (
    ErrorCode, ItemType, Phase, ResolveResult, build_stack_push, build_stack_resolve,
    build_game_state_update, build_error,
)
from server.core.game_state import GameState, StackItem, Permanent, build_permanent_from_catalog


_id_counter = itertools.count(1)


def _new_stack_item_id() -> str:
    return f"stk_{next(_id_counter):02d}"


class StackManager:
    def __init__(self, state: GameState,
                 send_fn: Callable[[str, dict], None],
                 broadcast_fn: Callable[[dict], None],
                 on_game_over: Callable[[str, str, str], None],
                 reopen_priority_for_actor: Callable[[str], None],
                 reopen_priority_after_resolution: Callable[[], None],
                 card_catalog: Optional[dict] = None):
        self.state = state
        self.send_fn = send_fn
        self.broadcast_fn = broadcast_fn
        self.on_game_over = on_game_over
        self.reopen_priority_for_actor = reopen_priority_for_actor
        self.reopen_priority_after_resolution = reopen_priority_after_resolution
        # card_catalog: out-of-band shared catalog per RFC intro note.
        # Expected shape per card_id: {"mana_cost": {...}, "type": "INSTANT"|"SORCERY"|"CREATURE"|"LAND"|"ARTIFACT"|"ENCHANTMENT", ...}
        if card_catalog is None:
            from shared.pdu import CARD_CATALOG
            card_catalog = CARD_CATALOG
        self.card_catalog = card_catalog

    # -- STK-01 / STK-01b: casting & activating ---------------------------

    def handle_cast_spell(self, player_id: str, pdu: dict) -> None:
        """STK-01: CAST_SPELL validation (hand, timing, mana, stack push)."""
        card_id = pdu.get("card_id")
        targets = pdu.get("targets", [])
        mana_payment = pdu.get("mana_payment", {})

        caster = self.state.players[player_id]

        # 1. Hand check
        if card_id not in caster.hand:
            self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                          f"Card {card_id} is not in hand.")
            return

        card_info = self.card_catalog.get(card_id, {})
        is_instant = card_info.get("type") == "INSTANT"

        # 2. Timing check (RFC 7.5): Non-instants require Main Phase, empty stack, Active Player
        if not is_instant:
            if self.state.phase not in Phase.MAIN_PHASES:
                self._reject(player_id, pdu, ErrorCode.WRONG_PHASE,
                              "Sorcery-speed spells can only be cast during a Main Phase.")
                return
            if not self.state.is_stack_empty():
                self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                              "Sorcery-speed spells require an empty stack.")
                return
            if player_id != self.state.active_player:
                self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                              "Sorcery-speed spells can only be cast by the Active Player.")
                return

        # 3. Mana validation against catalog cost
        required_mana = card_info.get("mana_cost", {})
        if not self._can_pay(caster, mana_payment, required_mana):
            self._reject(player_id, pdu, ErrorCode.INSUFFICIENT_MANA,
                          "Declared mana payment cannot be satisfied.")
            return

        self._deduct_mana(caster, mana_payment)
        caster.hand.remove(card_id)

        item = StackItem(
            stack_item_id=_new_stack_item_id(),
            item_type=ItemType.SPELL,
            source_id=card_id,
            controller_id=player_id,
            targets=targets,
            mana_paid=mana_payment,
        )
        self._push(item)
        # RFC 8.1 rule 3: caster retains priority.
        self.reopen_priority_for_actor(player_id)

    def handle_activate_ability(self, player_id: str, pdu: dict) -> None:
        """STK-01b. RFC 8.1, 10.2.8."""
        source_id = pdu.get("source_id")
        ability_index = pdu.get("ability_index")
        targets = pdu.get("targets", [])
        cost_payment = pdu.get("cost_payment", {})

        controller = self.state.players[player_id]
        permanent = controller.find_permanent(source_id)

        if permanent is None:
            self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                          f"Permanent {source_id} not found under your control.")
            return

        requires_tap = cost_payment.get("tap", False)
        if requires_tap and permanent.tapped:
            self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                          f"Permanent {source_id} is already tapped.")
            return

        if requires_tap and permanent.is_creature and permanent.summoning_sick \
                and not permanent.has_haste:
            self._reject(player_id, pdu, ErrorCode.ILLEGAL_ACTION,
                          "Permanent has summoning sickness and cannot use tap abilities.")
            return

        mana_cost = cost_payment.get("mana", {})
        if not self._can_pay(controller, mana_cost):
            self._reject(player_id, pdu, ErrorCode.INSUFFICIENT_MANA,
                          "Declared mana payment cannot be satisfied.")
            return

        if requires_tap:
            permanent.tapped = True
        self._deduct_mana(controller, mana_cost)

        item = StackItem(
            stack_item_id=_new_stack_item_id(),
            item_type=ItemType.ABILITY,
            source_id=source_id,
            controller_id=player_id,
            targets=targets,
            mana_paid=mana_cost,
        )
        self._push(item)
        self.reopen_priority_for_actor(player_id)

    def _can_pay(self, player, declared_payment: dict, required_cost: Optional[dict] = None) -> bool:
        """
        Two-part check (RFC 7.5 / Section 11 INSUFFICIENT_MANA):
          1. Does the declared_payment actually cover the spell's
             required_cost from the catalog? (existing check, kept)
          2. Does the player actually HAVE untapped mana sources able
             to produce what they declared? This was previously a
             no-op stub that trusted the client blindly -- now backed
             by PlayerState.can_afford(), which reads real battlefield
             state (see game_state.py).
        """
        if required_cost:
            for color, amount in required_cost.items():
                if color == "X":
                    continue
                if declared_payment.get(color, 0) < amount:
                    return False

        return player.can_afford(declared_payment)

    def _deduct_mana(self, player, mana_payment: dict) -> None:
        """Actually taps the mana sources used to pay (RFC 7.5)."""
        player.pay_mana(mana_payment)

    # -- STK-02: LIFO stack -------------------------------------------------

    def _push(self, item: StackItem) -> None:
        self.state.stack.append(item)
        pdu = build_stack_push(
            self.state.next_seq(), item.stack_item_id, item.item_type,
            item.source_id, item.targets, item.controller_id,
        )
        self.broadcast_fn(pdu)

    def push_trigger(self, item: StackItem) -> None:
        """Entry point for TRG-01 to push a triggered ability."""
        self._push(item)

    def resolve_top(self) -> None:
        """
        RFC 8.4: pop the top item, check target legality, apply effect
        or fizzle, broadcast STACK_RESOLVE, run SBAs, then re-grant
        priority to the Active Player.
        """
        if not self.state.stack:
            return

        item = self.state.stack.pop()

        if not self._targets_still_legal(item):
            pdu = build_stack_resolve(
                self.state.next_seq(), item.stack_item_id, ResolveResult.FIZZLE, [],
            )
            self.broadcast_fn(pdu)
        else:
            state_changes = self._apply_effect(item)
            pdu = build_stack_resolve(
                self.state.next_seq(), item.stack_item_id, ResolveResult.RESOLVED,
                state_changes,
            )
            self.broadcast_fn(pdu)
            self.broadcast_fn(build_game_state_update(
                self.state.next_seq(),
                self.state.to_personalized_dict(item.controller_id),
            ))

        self.run_state_based_actions()
        if not self.state.game_over:
            self.reopen_priority_after_resolution()

    def _targets_still_legal(self, item: StackItem) -> bool:
        for target in item.targets:
            if target in self.state.players:
                continue  # player targets are always "legal" unless dead
            found = any(
                ps.find_permanent(target) is not None
                for ps in self.state.players.values()
            )
            if not found:
                return False
        return True

    def _apply_effect(self, item: StackItem) -> list[dict]:
        """
        Effect dispatcher. Two categories, handled differently:

        1. Permanent spells (CREATURE / ARTIFACT / ENCHANTMENT): resolving
           just means "enter the battlefield" -- this is core stack
           mechanics (RFC 8.6.1 lists "a permanent enters the battlefield"
           as a trigger-detection event), not a card-specific effect, so
           it's implemented here directly rather than deferred.

        2. One-off effects (INSTANT / SORCERY / ACTIVATE_ABILITY effects
           like direct damage, life gain, bounce, destroy, counter):
           these ARE card-specific (CARD-01..05) and remain a TODO stub
           here, to be filled in by whoever owns that ticket.
        """
        card_info = self.card_catalog.get(item.source_id, {})
        card_type = str(card_info.get("type", "")).upper()
        state_changes: list[dict] = []

        if item.item_type == ItemType.SPELL and card_type in ("CREATURE", "ARTIFACT", "ENCHANTMENT"):
            controller = self.state.players[item.controller_id]
            permanent = build_permanent_from_catalog(
                item.source_id, self.card_catalog, summoning_sick=True,
            )
            controller.battlefield.append(permanent)
            state_changes.append({
                "change_type": "PERMANENT_ENTERS",
                "card_id": item.source_id,
                "controller": item.controller_id,
                "tapped": permanent.tapped,
            })
            return state_changes

        # TODO (CARD-01..05): Dispatch INSTANT/SORCERY/ABILITY one-off
        # effects here once that module exists, e.g.:
        #   effect_type = card_info.get("effect")
        #   if effect_type == "DAMAGE":
        #       ... apply card_info["amount"] to item.targets ...
        return state_changes

    # -- STK-03: state-based actions -----------------------------------------

    def run_state_based_actions(self) -> None:
        """
        RFC 8.4: repeat until no SBAs remain.
          - life <= 0 -> that player loses (simultaneous zero -> AP loses)
          - lethal damage / toughness <= 0 creature -> graveyard
        """
        changed = True
        while changed and not self.state.game_over:
            changed = False

            p1_id, p2_id = list(self.state.players.keys())
            p1, p2 = self.state.players[p1_id], self.state.players[p2_id]

            if p1.life_total <= 0 and p2.life_total <= 0:
                # RFC 8.4: simultaneous zero -> Active Player loses.
                loser = self.state.active_player
                winner = self.state.opponent_of(loser)
                self.state.game_over = True
                self.on_game_over(winner, loser, "LIFE_ZERO")
                return

            if p1.life_total <= 0:
                self.state.game_over = True
                self.on_game_over(p2_id, p1_id, "LIFE_ZERO")
                return
            if p2.life_total <= 0:
                self.state.game_over = True
                self.on_game_over(p1_id, p2_id, "LIFE_ZERO")
                return

            for ps in self.state.players.values():
                dead = [p for p in ps.battlefield if p.is_dead()]
                for perm in dead:
                    ps.battlefield.remove(perm)
                    ps.graveyard.append(perm.id)
                    changed = True

    def _reject(self, player_id: str, pdu: dict, code: str, message: str) -> None:
        self.send_fn(player_id, build_error(
            self.state.next_seq(), code, message,
            rejected_action={"type": pdu.get("type"), "seq_num": pdu.get("seq_num")},
        ))