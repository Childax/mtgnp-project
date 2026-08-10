"""
server/engine/combat.py

Implements CMB-01 through CMB-05 per RFC 0001 Section 9.

- CMB-01: DECLARE_ATTACKERS validation and tapping
- CMB-02: DECLARE_BLOCKERS validation
- CMB-03: ASSIGN_DAMAGE_ORDER for multiple blockers
- CMB-04: First strike / double strike damage step
- CMB-05: Simultaneous combat damage computation and broadcast

Wired into TurnManager via wire_dependencies()'s combat_hooks dict, so
TurnManager calls into here at the right steps without a hard import
cycle.
"""

from __future__ import annotations
from typing import Callable
from collections import defaultdict

from shared.pdu import ErrorCode, build_game_state_update, build_combat_damage_result
from server.core.game_state import GameState, broadcast_personalized_state


class CombatManager:
    def __init__(self, state: GameState,
                 send_fn: Callable[[str, dict], None],
                 broadcast_fn: Callable[[dict], None],
                 reject_fn: Callable[[str, dict, str, str], None],
                 open_priority_fn: Callable[[], None],
                 advance_to_fn: Callable[[str], None],
                 run_sba_fn: Callable[[], None]):
        self.state = state
        self.send_fn = send_fn
        self.broadcast_fn = broadcast_fn
        self.reject_fn = reject_fn          # (player_id, pdu, message, code)
        self.open_priority_fn = open_priority_fn
        self.advance_to_fn = advance_to_fn
        self.run_sba_fn = run_sba_fn

        # Combat-local state, reset each combat phase by TurnManager
        # setting BEGIN_COMBAT flags; mirrored here for convenience.
        self.attackers: dict[str, str] = {}      # creature_id -> target player_id
        self.blockers: dict[str, list[str]] = defaultdict(list)  # attacker_id -> [blocker_ids]
        self.damage_order: dict[str, list[str]] = {}  # attacker_id -> ordered blocker_ids
        self._awaiting_player: str | None = None

        # Flags TurnManager._advance_step() reads (via TurnManager's
        # wired combat_manager reference) to know whether to skip an
        # empty Declare Attackers / Declare Blockers / damage-order
        # step. Initialized here so they're never missing on first
        # access even before reset() has run once.
        self.any_attackers_declared: bool = False
        self.any_multi_blocked: bool = False
        self.any_first_or_double_strike: bool = False
        self._fs_ds_seen: bool = False

    def reset(self) -> None:
        """Called by TurnManager at the start of each Begin Combat Step."""
        self.attackers.clear()
        self.blockers.clear()
        self.damage_order.clear()
        self.any_attackers_declared = False
        self.any_multi_blocked = False
        self.any_first_or_double_strike = False
        self._fs_ds_seen = False

    # -- CMB-01: Declare Attackers -----------------------------------------

    def begin_declare_attackers(self) -> None:
        """
        Called by TurnManager when entering DECLARE_ATTACKERS. Per RFC
        9.3, this implicitly signals the Active Player to send
        DECLARE_ATTACKERS -- there's no separate request PDU, so we just
        wait for the client's next message (routed to handle_declare_attackers
        by the network layer).
        """
        self._awaiting_player = self.state.active_player

    def handle_declare_attackers(self, player_id: str, pdu: dict) -> None:
        if player_id != self.state.active_player:
            self.reject_fn(player_id, pdu, "Only the Active Player may declare attackers.",
                            ErrorCode.ILLEGAL_ACTION)
            return

        ap = self.state.players[player_id]
        declared = pdu.get("attackers", [])

        for entry in declared:
            creature_id = entry.get("creature_id")
            target = entry.get("target")
            permanent = ap.find_permanent(creature_id)

            if permanent is None or not permanent.is_creature:
                self.reject_fn(player_id, pdu, f"{creature_id} is not a creature you control.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            if permanent.tapped:
                self.reject_fn(player_id, pdu, f"{creature_id} is already tapped.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            if permanent.summoning_sick and not permanent.has_haste:
                self.reject_fn(player_id, pdu, f"{creature_id} has summoning sickness.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            if target != self.state.opponent_of(player_id):
                self.reject_fn(player_id, pdu, f"Invalid attack target {target}.",
                                ErrorCode.ILLEGAL_TARGET)
                return

        # All legal -- apply.
        self.attackers = {}
        for entry in declared:
            creature_id = entry["creature_id"]
            self.attackers[creature_id] = entry["target"]
            perm = ap.find_permanent(creature_id)
            perm.tapped = True
            if perm.has_first_strike or perm.has_double_strike:
                self._mark_fs_ds_present()

        broadcast_personalized_state(self.state, self.send_fn)

        # Signal TurnManager whether combat continues past this step
        # (RFC 9.3: empty attackers -> skip straight to End of Combat).
        self._notify_turn_manager_attacker_flag(len(self.attackers) > 0)
        self.open_priority_fn()

    def _mark_fs_ds_present(self) -> None:
        self._fs_ds_seen = True

    def _notify_turn_manager_attacker_flag(self, has_attackers: bool) -> None:
        # Simple attribute bridge; TurnManager reads
        # combat.any_attackers_declared directly (see wiring below).
        self.any_attackers_declared = has_attackers

    # -- CMB-02: Declare Blockers -------------------------------------------

    def begin_declare_blockers(self) -> None:
        self._awaiting_player = self.state.non_active_player()

    def handle_declare_blockers(self, player_id: str, pdu: dict) -> None:
        nap = self.state.players[player_id]
        if player_id != self.state.non_active_player():
            self.reject_fn(player_id, pdu, "Only the Non-Active Player may declare blockers.",
                            ErrorCode.ILLEGAL_ACTION)
            return

        declared = pdu.get("blockers", [])
        seen_blockers = set()

        for entry in declared:
            creature_id = entry.get("creature_id")
            blocking_id = entry.get("blocking_id")
            permanent = nap.find_permanent(creature_id)

            if permanent is None or not permanent.is_creature:
                self.reject_fn(player_id, pdu, f"{creature_id} is not a creature you control.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            if permanent.tapped:
                self.reject_fn(player_id, pdu, f"{creature_id} is tapped and cannot block.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            if blocking_id not in self.attackers:
                self.reject_fn(player_id, pdu, f"{blocking_id} is not an attacking creature.",
                                ErrorCode.ILLEGAL_TARGET)
                return
            if creature_id in seen_blockers:
                self.reject_fn(player_id, pdu, f"{creature_id} cannot block more than one attacker.",
                                ErrorCode.ILLEGAL_ACTION)
                return
            seen_blockers.add(creature_id)

        self.blockers.clear()
        for entry in declared:
            self.blockers[entry["blocking_id"]].append(entry["creature_id"])
            # Blocking does not tap the blocker (RFC 9.4).

        for pid in self.state.players:
            visible_state = self.state.to_personalized_dict(pid)

            visible_state["combat_blockers"] = {
                attacker_id: list(blocker_ids)
                for attacker_id, blocker_ids in self.blockers.items()
            }

            blocker_update = build_game_state_update(
                self.state.next_seq(),
                visible_state
            )

            self.send_fn(
                pid,
                blocker_update
            )

        self.any_multi_blocked = any(len(bs) >= 2 for bs in self.blockers.values())
        self.any_first_or_double_strike = getattr(self, "_fs_ds_seen", False) or \
            self._any_blocker_has_fs_ds()

        self.open_priority_fn()

    def _any_blocker_has_fs_ds(self) -> bool:
        for attacker_id, blocker_ids in self.blockers.items():
            for bid in blocker_ids:
                for ps in self.state.players.values():
                    perm = ps.find_permanent(bid)
                    if perm and (perm.has_first_strike or perm.has_double_strike):
                        return True
        return False

    # -- CMB-03: Assign Damage Order -----------------------------------------

    def begin_assign_damage_order(self) -> None:
        self._pending_order_attackers = [
            a for a, bs in self.blockers.items() if len(bs) >= 2
        ]
        self._awaiting_player = self.state.active_player
        # Network layer should prompt for one ASSIGN_DAMAGE_ORDER per
        # multiply-blocked attacker; here we just wait for each.

    def handle_assign_damage_order(self, player_id: str, pdu: dict) -> None:
        if player_id != self.state.active_player:
            self.reject_fn(player_id, pdu, "Only the Active Player assigns damage order.",
                            ErrorCode.ILLEGAL_ACTION)
            return

        attacker_id = pdu.get("attacker_id")
        order = pdu.get("blocker_order", [])
        actual_blockers = set(self.blockers.get(attacker_id, []))

        if attacker_id not in getattr(self, "_pending_order_attackers", []):
            self.reject_fn(player_id, pdu, f"{attacker_id} does not require damage ordering.",
                            ErrorCode.ILLEGAL_ACTION)
            return
        if set(order) != actual_blockers:
            self.reject_fn(player_id, pdu, "Order must contain exactly this attacker's blockers.",
                            ErrorCode.ILLEGAL_ACTION)
            return

        self.damage_order[attacker_id] = order
        self._pending_order_attackers.remove(attacker_id)

        if self._pending_order_attackers:
            return  # wait for more ASSIGN_DAMAGE_ORDER PDUs

        self.open_priority_fn()

    # -- CMB-04 / CMB-05: Damage steps ---------------------------------------

    def run_first_strike_damage(self) -> None:
        """RFC 9.6. Only creatures with first/double strike deal damage here."""
        events = self._compute_damage(first_strike_pass=True)
        self._apply_damage_and_broadcast(events, is_final_combat_step=False)
        self.open_priority_fn()

    def run_combat_damage(self) -> None:
        """RFC 9.7. All remaining creatures (excludes first-strike-only,
        since they already dealt damage in the first strike step)."""
        events = self._compute_damage(first_strike_pass=False)
        self._apply_damage_and_broadcast(events, is_final_combat_step=True)
        self.advance_to_fn("END_OF_COMBAT")

    def _compute_damage(self, first_strike_pass: bool) -> list[dict]:
        """
        Returns a list of {"source", "target", "amount"} events.
        MTGNP 1.0 has no trample (RFC 9.7): blocked attackers deal all
        damage to blockers only; unblocked attackers hit the player.
        """
        events: list[dict] = []
        ap_id = self.state.active_player
        ap = self.state.players[ap_id]

        def wants_to_deal_now(perm) -> bool:
            if perm is None:
                return False
            if perm.has_double_strike:
                return True
            if perm.has_first_strike:
                return first_strike_pass
            return not first_strike_pass

        for attacker_id, target in self.attackers.items():
            attacker = ap.find_permanent(attacker_id)
            if attacker is None or attacker.is_dead():
                continue
            if not wants_to_deal_now(attacker):
                continue

            blocker_ids = self.blockers.get(attacker_id, [])
            if not blocker_ids:
                events.append({"source": attacker_id, "target": target,
                                "amount": attacker.power})
                continue

            order = self.damage_order.get(attacker_id, blocker_ids)
            remaining = attacker.power
            defender = self.state.players[target]

            live_blockers = []

            for bid in order:
                blocker = defender.find_permanent(bid)

                if blocker is not None and not blocker.is_dead():
                    live_blockers.append((bid, blocker))

            for index, (bid, blocker) in enumerate(live_blockers):
                if remaining <= 0:
                    break

                is_last_blocker = index == len(live_blockers) - 1

                if is_last_blocker:
                    assign = remaining
                else:
                    lethal_needed = max(
                        blocker.toughness - blocker.damage,
                        0
                    )
                    assign = min(remaining, lethal_needed)

                events.append({
                    "source": attacker_id,
                    "target": bid,
                    "amount": assign
                })

                remaining -= assign

            # Blockers deal damage back to the attacker simultaneously.
            for bid in blocker_ids:
                blocker = defender.find_permanent(bid)
                if blocker is None or blocker.is_dead():
                    continue
                if not wants_to_deal_now(blocker):
                    continue
                events.append({"source": bid, "target": attacker_id, "amount": blocker.power})

        return events

    def _apply_damage_and_broadcast(self, events: list[dict], is_final_combat_step: bool) -> None:
        creatures_died_before = self._dead_creature_ids()

        for ev in events:
            target = ev["target"]
            amount = ev["amount"]
            if target in self.state.players:
                self.state.players[target].life_total -= amount
            else:
                for ps in self.state.players.values():
                    perm = ps.find_permanent(target)
                    if perm:
                        perm.damage += amount
                        break

        creatures_died_after = self._dead_creature_ids()
        newly_died = list(creatures_died_after - creatures_died_before)

        # Apply state-based actions before broadcasting the new state.
        # This moves creatures with lethal damage to the graveyard.
        self.run_sba_fn()

        if self.state.game_over:
            return

        if is_final_combat_step or events:
            pdu = build_combat_damage_result(
                self.state.next_seq(), events,
                {pid: p.life_total for pid, p in self.state.players.items()},
                newly_died,
            )
            self.broadcast_fn(pdu)

        for pid in self.state.players:
            self.send_fn(pid, build_game_state_update(
                self.state.next_seq(), self.state.to_personalized_dict(pid),
            ))

    def _dead_creature_ids(self) -> set[str]:
        dead = set()
        for ps in self.state.players.values():
            for perm in ps.battlefield:
                if perm.is_dead():
                    dead.add(perm.id)
        return dead
