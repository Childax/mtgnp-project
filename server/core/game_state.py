"""
server/core/game_state.py

Interim GameState object, shaped directly off RFC 0001 Section 10.2.2's
in-game GAME_STATE_UPDATE example.

NOTE: This was supposed to be SET-03 standin but we can use this instead if 
it looks ok to you

Also includes SET-01 (initial setup) and SET-02 (mulligan)
helper methods, since the engine (TurnManager/StackManager) needs a
concrete object to call into regardless of which ticket officially
owns which method -- these are clearly labelled by RFC section so
ownership is easy to split back out later if needed.
 
Sections:
  1. Permanent / StackItem / PlayerState / GameState  (core objects)
  2. Mana sources & payment              (closes the STK-01 mana gap)
  3. SET-01: deck validation + initial setup
  4. SET-02: London Mulligan
  5. SET-03: personalized state dicts (in-game + lobby variants)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import random

from shared.pdu import build_game_state_update
 
 
# ---------------------------------------------------------------------------
# 1. Core objects
# ---------------------------------------------------------------------------
 
@dataclass
class Permanent:
    """A card instance on the battlefield. RFC 10.2.2 battlefield entries."""
    id: str
    tapped: bool = False
    is_creature: bool = False
    damage: int = 0
    power: int = 0
    toughness: int = 0
    summoning_sick: bool = False
    has_first_strike: bool = False
    has_double_strike: bool = False
    has_haste: bool = False
 
    # Mana-source fields (closes the STK-01 "any player can pay any
    # cost" gap). Empty dict = this permanent does not produce mana.
    # Example: a Mountain has mana_produced = {"R": 1}.
    mana_produced: dict = field(default_factory=dict)
 
    def is_dead(self) -> bool:
        """RFC 8.4 state-based action: lethal damage or toughness <= 0."""
        if not self.is_creature:
            return False
        return self.toughness <= 0 or self.damage >= self.toughness
 
    def can_tap_for_mana(self) -> bool:
        return bool(self.mana_produced) and not self.tapped and not (
            self.is_creature and self.summoning_sick and not self.has_haste
        )
 
 
@dataclass
class StackItem:
    """RFC 8.3 -- a single Stack entry."""
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
 
    # -- 2. Mana sources & payment (closes STK-01's real gap) ------------
 
    def available_mana(self) -> dict:
        """
        Sums mana_produced across all untapped, usable mana sources.
        RFC 7.5: "mana production is handled implicitly" -- this is the
        server-side truth used to validate a client's declared
        mana_payment, rather than trusting it blindly.
        """
        totals: dict[str, int] = {}
        for perm in self.battlefield:
            if not perm.can_tap_for_mana():
                continue
            for color, amount in perm.mana_produced.items():
                totals[color] = totals.get(color, 0) + amount
        return totals
 
    def can_afford(self, mana_payment: dict) -> bool:
        """
        True if this player's untapped mana sources can actually cover
        the declared payment (per RFC 11's INSUFFICIENT_MANA check).
        Does NOT check whether the payment covers a spell's cost --
        that's a separate comparison against the card's mana_cost.
        """
        available = self.available_mana()
        for color, amount in mana_payment.items():
            if color == "X":
                continue  # generic cost is covered by leftover, checked separately
            if available.get(color, 0) < amount:
                return False
        total_declared = sum(mana_payment.values())
        total_available = sum(available.values())
        return total_declared <= total_available
 
    def pay_mana(self, mana_payment: dict) -> bool:
        """
        Actually taps the permanents needed to cover mana_payment.
        Returns False (and taps nothing) if payment can't be covered --
        caller should check can_afford() first, but this re-validates
        defensively so partial-tap states can never occur.
        """
        if not self.can_afford(mana_payment):
            return False
 
        remaining = dict(mana_payment)
        # Pay colored costs first with matching-color sources.
        for color in list(remaining.keys()):
            if color == "X":
                continue
            need = remaining[color]
            for perm in self.battlefield:
                if need <= 0:
                    break
                if not perm.can_tap_for_mana():
                    continue
                produced = perm.mana_produced.get(color, 0)
                if produced <= 0:
                    continue
                perm.tapped = True
                need -= produced
            remaining[color] = max(need, 0)
 
        # Pay any remaining generic ("X") cost with whatever's left untapped.
        generic_needed = remaining.get("X", 0) + sum(
            v for k, v in remaining.items() if k != "X"
        )
        if generic_needed > 0:
            for perm in self.battlefield:
                if generic_needed <= 0:
                    break
                if not perm.can_tap_for_mana():
                    continue
                produced = sum(perm.mana_produced.values())
                if produced <= 0:
                    continue
                perm.tapped = True
                generic_needed -= produced
 
        return True
 
 
class GameState:
    """
    The single authoritative Game State (RFC Section 3, "Game State").
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
        self.first_player_id: Optional[str] = None
 
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
 
    # -- 3. SET-01: deck validation + initial setup ------------------------
 
    @staticmethod
    def validate_deck(deck_list: list, valid_card_ids: Optional[set] = None) -> Optional[str]:
        """
        RFC 6.3 step 1 / Section 11 ILLEGAL_DECK. Returns None if legal,
        otherwise an error message. Card-catalog membership check is
        optional here (pass valid_card_ids from shared.pdu.VALID_CARD_IDS)
        since callers may want to validate size only in isolated tests.
        """
        if not deck_list or len(deck_list) > 50:
            return f"Deck contains {len(deck_list)} cards; must be 1-50."
        if valid_card_ids is not None:
            unknown = [c for c in deck_list if c not in valid_card_ids]
            if unknown:
                return f"Deck contains unknown card(s): {unknown}"
        return None
 
    @classmethod
    def initialize_from_decks(cls, player_decks: dict[str, list],
                               rng: Optional[random.Random] = None) -> "GameState":
        """
        RFC 6.3 GAME_SETUP: life totals to 20, shuffle, draw 7,
        coin-flip first player. Deck legality should already have been
        checked by validate_deck() / PlayerReadyPDU before this is
        called -- this method assumes valid decks.
        """
        rng = rng or random.Random()
        player_ids = list(player_decks.keys())
        state = cls(player_ids)
 
        for pid, decklist in player_decks.items():
            ps = state.players[pid]
            ps.library = list(decklist)
            rng.shuffle(ps.library)
            ps.life_total = 20
            ps.hand = [ps.library.pop(0) for _ in range(min(7, len(ps.library)))]
 
        state.first_player_id = rng.choice(player_ids)
        state.active_player = state.first_player_id
        state.phase = "MULLIGAN"
        return state
 
    # -- 4. SET-02: London Mulligan -----------------------------------------
 
    def mulligan_redraw(self, player_id: str, rng: Optional[random.Random] = None) -> None:
        """
        RFC 6.4: player takes a mulligan. Shuffles hand back into
        library, draws a fresh 7. Bottoming happens separately in
        mulligan_keep() once the player decides to keep.
        """
        rng = rng or random.Random()
        ps = self.players[player_id]
        ps.library.extend(ps.hand)
        ps.hand = []
        rng.shuffle(ps.library)
        ps.mulligan_count += 1
        ps.hand = [ps.library.pop(0) for _ in range(min(7, len(ps.library)))]
 
    def mulligan_keep(self, player_id: str, cards_to_bottom: list) -> Optional[str]:
        """
        RFC 6.4: validates cards_to_bottom has exactly mulligan_count
        cards, all present in hand, then bottoms them. Returns an error
        message on failure (caller sends ILLEGAL_ACTION), else None.
        """
        ps = self.players[player_id]
        if len(cards_to_bottom) != ps.mulligan_count:
            return (f"cards_to_bottom must contain exactly {ps.mulligan_count} "
                     f"card(s), got {len(cards_to_bottom)}.")
        for cid in cards_to_bottom:
            if cid not in ps.hand:
                return f"Card {cid} is not in hand."
 
        for cid in cards_to_bottom:
            ps.hand.remove(cid)
            ps.library.append(cid)  # bottom of library
 
        ps.has_kept_hand = True
        return None
 
    # -- 5. SET-03: personalized state dicts ---------------------------------
 
    def to_personalized_dict(self, viewer_id: str) -> dict:
        """
        Builds the state object for GAME_STATE_UPDATE (in-game variant),
        hiding the opponent's hand per RFC 4.2 / Section 8.
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
            "mulligan_count": viewer.mulligan_count,
        }
 
 
def broadcast_personalized_state(state: "GameState",
                                  send_fn: Callable[[str, dict], None]) -> None:
    """
    Sends each player their OWN personalized GAME_STATE_UPDATE.

    IMPORTANT: this must be used instead of a single
    broadcast_fn(build_game_state_update(seq, state.to_personalized_dict(X)))
    call. to_personalized_dict(X) embeds player X's hand in the "hand"
    key -- broadcasting that one dict to both clients leaks X's hidden
    hand to the opponent, which RFC 0001 Section 4.2 / 12 explicitly
    forbids ("the server MUST withhold hidden information ... from
    GAME_STATE_UPDATE messages"). Each player must get a separately
    built dict via send_fn, never via broadcast_fn.
    """
    for pid in state.players:
        send_fn(pid, build_game_state_update(state.next_seq(), state.to_personalized_dict(pid)))


def build_lobby_state_dict(players_ready: int, waiting_for: list) -> dict:
    """
    RFC 10.2.2 LOBBY-phase variant. Standalone function (not a
    GameState method) since a GameState instance doesn't exist yet
    during LOBBY -- this is what Andi's LOB-01/LOB-02 lobby-session
    tracking should call to build the `state` object passed into
    build_game_state_update() from shared/pdu.py.
    """
    return {
        "phase": "LOBBY",
        "players_ready": players_ready,
        "waiting_for": waiting_for,
    }
 
 
# ---------------------------------------------------------------------------
# Catalog-driven helpers used by the engine when creating Permanents
# ---------------------------------------------------------------------------
 
_BASIC_LAND_MANA = {
    "mountain": {"R": 1},
    "swamp": {"B": 1},
    "island": {"U": 1},
    "forest": {"G": 1},
    "plains": {"W": 1},
}
 
 
def infer_mana_produced(card_id: str, catalog: Optional[dict] = None) -> dict:
    """
    Determines what mana a land (or mana-producing permanent) taps for.
    Catalog entry takes priority (expects a "mana_produced" key, e.g.
    {"R": 1}); falls back to basic-land name matching (RFC's example
    card IDs: mountain_001, swamp_001, island_001, forest_003, plains_x)
    so games work even before every catalog entry has this field set.
    NOTE: this fallback is a pragmatic default -- confirm against the
    real catalog schema once it's finalized and drop the name-matching
    branch if the catalog covers every mana source explicitly.
    """
    catalog = catalog or {}
    card_info = catalog.get(card_id, {})
    if "mana_produced" in card_info:
        return dict(card_info["mana_produced"])
 
    lowered = card_id.lower()
    for land_name, mana in _BASIC_LAND_MANA.items():
        if lowered.startswith(land_name):
            return dict(mana)
    return {}
 
 
def build_permanent_from_catalog(card_id: str, catalog: Optional[dict] = None,
                                  summoning_sick: bool = True) -> Permanent:
    """
    Constructs a Permanent for a card entering the battlefield, pulling
    stats from the catalog when available. Used by STK-02 when a
    creature/artifact spell resolves, and by TURN-04 when a land is
    played.
    """
    catalog = catalog or {}
    info = catalog.get(card_id, {})
    card_type = str(info.get("type", "")).upper()
    is_creature = card_type == "CREATURE"
 
    # NOTE: previously this only ran mana inference when card_type was
    # exactly "LAND" or empty, which silently produced {} (no mana) for
    # any land whose catalog "type" value used different casing (e.g.
    # "Land") or wasn't present in the catalog at all. infer_mana_produced()
    # is already safe to call unconditionally -- it checks the catalog's
    # explicit mana_produced field first, then falls back to basic-land
    # name matching, and returns {} for anything that isn't a mana
    # source. So just always call it instead of gating on card_type.
    return Permanent(
        id=card_id,
        tapped=False,
        is_creature=is_creature,
        power=info.get("power", 0),
        toughness=info.get("toughness", 0),
        summoning_sick=summoning_sick and is_creature,
        has_first_strike=info.get("has_first_strike", False),
        has_double_strike=info.get("has_double_strike", False),
        has_haste=info.get("has_haste", False),
        mana_produced=infer_mana_produced(card_id, catalog),
    )
 