"""
server/core/game_state.py

Interim GameState object, shaped directly off RFC 0001 Section 10.2.2's
in-game GAME_STATE_UPDATE example.

NOTE: This was supposed to be SET-03 standin but we can use this instead if 
it looks ok to you
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Permanent:
    """A card instance on the battlefield. RFC 10.2.2 battlefield entries."""
    id: str
    tapped: bool = False
    # Creature-only fields (RFC: "Creatures add: damage, power, toughness,
    # summoning_sick"). Non-creature permanents (lands, etc.) leave these
    # at their defaults and callers should treat power/toughness as N/A.
    is_creature: bool = False
    damage: int = 0
    power: int = 0
    toughness: int = 0
    summoning_sick: bool = False
    has_first_strike: bool = False
    has_double_strike: bool = False
    has_haste: bool = False

    def is_dead(self) -> bool:
        """RFC 8.4 state-based action: lethal damage or toughness <= 0."""
        if not self.is_creature:
            return False
        return self.toughness <= 0 or self.damage >= self.toughness


@dataclass
class StackItem:
    """RFC 8.3 — a single Stack entry."""
    stack_item_id: str
    item_type: str          # SPELL | ABILITY | TRIGGER_ABILITY
    source_id: str
    controller_id: str
    targets: list = field(default_factory=list)
    mana_paid: dict = field(default_factory=dict)


@dataclass
class PlayerState:
    player_id: str
    life_total: int = 20
    hand: list = field(default_factory=list)          # list of card_id strings
    library: list = field(default_factory=list)       # ordered, index 0 = top
    graveyard: list = field(default_factory=list)      # index 0 = first placed
    battlefield: list = field(default_factory=list)    # list[Permanent]
    land_played_this_turn: bool = False
    mulligan_count: int = 0
    has_kept_hand: bool = False

    def find_permanent(self, permanent_id: str) -> Optional[Permanent]:
        for p in self.battlefield:
            if p.id == permanent_id:
                return p
        return None

    def has_card_in_hand(self, card_id: str) -> bool:
        return card_id in self.hand


class GameState:
    """
    The single authoritative Game State (RFC Section 3, "Game State").

    Engine modules should treat this as the source of truth for
    everything except lobby-only metadata (which lives before IN_GAME
    and is Andi's LOB-01/SET-01 territory).
    """

    def __init__(self, player_ids: list[str]):
        if len(player_ids) != 2:
            raise ValueError("MTGNP 1.0 requires exactly two players (RFC 1)")
        self.players: dict[str, PlayerState] = {
            pid: PlayerState(player_id=pid) for pid in player_ids
        }
        self.turn: int = 0
        self.phase: Optional[str] = None
        self.active_player: Optional[str] = None
        self.priority_holder: Optional[str] = None
        self.stack: list[StackItem] = []
        self.game_over: bool = False

        # server-issued monotonic counter for outgoing PDUs (RFC 5.4).
        self._seq_counter: int = 0

    def next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def opponent_of(self, player_id: str) -> str:
        for pid in self.players:
            if pid != player_id:
                return pid
        raise ValueError(f"No opponent found for {player_id}")

    def non_active_player(self) -> str:
        return self.opponent_of(self.active_player)

    def is_stack_empty(self) -> bool:
        return len(self.stack) == 0

    def to_personalized_dict(self, viewer_id: str) -> dict:
        """
        Builds the state object for GAME_STATE_UPDATE, hiding the
        opponent's hand per RFC 4.2 / Section 8 (Visible State) —
        only a hand_counts entry is included for the opponent.
        """
        opponent_id = self.opponent_of(viewer_id)
        viewer = self.players[viewer_id]
        opponent = self.players[opponent_id]

        def battlefield_dict(perm: Permanent) -> dict:
            d: dict[str, Any] = {"id": perm.id, "tapped": perm.tapped}
            if perm.is_creature:
                d.update({
                    "damage": perm.damage,
                    "power": perm.power,
                    "toughness": perm.toughness,
                    "summoning_sick": perm.summoning_sick,
                })
            return d

        return {
            "turn": self.turn,
            "active_player": self.active_player,
            "phase": self.phase,
            "priority_holder": self.priority_holder,
            "life_totals": {
                pid: p.life_total for pid, p in self.players.items()
            },
            "stack": [
                {
                    "stack_item_id": item.stack_item_id,
                    "item_type": item.item_type,
                    "source": item.source_id,
                    "targets": item.targets,
                    "controller": item.controller_id,
                }
                for item in self.stack
            ],
            "battlefield": {
                pid: [battlefield_dict(p) for p in ps.battlefield]
                for pid, ps in self.players.items()
            },
            "graveyard": {
                pid: list(ps.graveyard) for pid, ps in self.players.items()
            },
            "hand": {viewer_id: list(viewer.hand)},
            "hand_counts": {opponent_id: len(opponent.hand)},
            "library_counts": {
                pid: len(p.library) for pid, p in self.players.items()
            },
            "land_played_this_turn": viewer.land_played_this_turn,
        }