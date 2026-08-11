from typing import List, Dict, Any, Optional, Union, Literal
from pydantic import BaseModel, Field, ValidationError, field_validator
import json
import os

# Load the valid card IDs once when the module is imported.
# NOTE: made path relative to this file instead of the process's cwd,
# so it works regardless of where the server/client entrypoint is run
# from (e.g. smoke_test.py, pytest, or Richmond's tcp_server.py).
_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "card_catalog.json")
try:
    with open(_CATALOG_PATH, "r") as f:
        CARD_CATALOG = json.load(f)
except FileNotFoundError:
    # Catalog not present yet in this environment (e.g. engine-only
    # testing before the catalog file is added to the repo). Falling
    # back to an empty dict means deck validation / mana lookups are
    # skipped rather than crashing on import -- fine for engine dev,
    # NOT fine for the real LOBBY flow, where the catalog must exist.
    CARD_CATALOG = {}

# Kept for backwards compatibility with code that only needs the ID set
# (originally this was the only thing extracted from the catalog file --
# CARD_CATALOG above now also exposes full card data: mana_cost, type,
# power/toughness, mana_produced, etc.)
VALID_CARD_IDS = set(CARD_CATALOG.keys())

# ==========================================
# HELPER / SUB-MODELS
# ==========================================

class AttackerTarget(BaseModel):
    creature_id: str
    target: str

class BlockerAssignment(BaseModel):
    creature_id: str
    blocking_id: str

class DamageEvent(BaseModel):
    source: str
    target: str
    amount: int

class StateChange(BaseModel):
    change_type: str
    target: Optional[str] = None
    amount: Optional[int] = None
    card_id: Optional[str] = None
    controller: Optional[str] = None
    tapped: Optional[bool] = None

# ==========================================
# BASE PDU MODEL
# ==========================================

class BasePDU(BaseModel):
    type: str
    seq_num: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dict for send_pdu compatibility."""
        return self.model_dump(exclude_none=True)

# ==========================================
# 1. LOBBY & SETUP PDUs
# ==========================================

class PlayerReadyPDU(BasePDU):
    type: Literal["PLAYER_READY"] = "PLAYER_READY"
    player_id: str
    deck_list: List[str]

    @field_validator('player_id')
    @classmethod
    def validate_player_id(cls, player_id: str) -> str:
        if not player_id.strip():
            raise ValueError(
                "player_id must be a non-empty string."
            )

        return player_id

    @field_validator('deck_list')
    @classmethod
    def validate_deck(cls, deck: List[str]) -> List[str]:
        if not (1 <= len(deck) <= 50):
            raise ValueError("ILLEGAL_DECK: Deck must contain between 1 and 50 cards.")
        
        for card_id in deck:
            if card_id not in VALID_CARD_IDS:
                raise ValueError(f"ILLEGAL_DECK: Card '{card_id}' is not in the legal catalog.")
        
        return deck

class MulliganChoicePDU(BasePDU):
    type: Literal["MULLIGAN_CHOICE"] = "MULLIGAN_CHOICE"
    keep: bool
    cards_to_bottom: List[str] = Field(default_factory=list)

# ==========================================
# 2. STATE & PHASE TRANSITION PDUs
# ==========================================

class GameStateUpdatePDU(BasePDU):
    type: Literal["GAME_STATE_UPDATE"] = "GAME_STATE_UPDATE"
    state: Dict[str, Any]

class PhaseTransitionPDU(BasePDU):
    type: Literal["PHASE_TRANSITION"] = "PHASE_TRANSITION"
    from_phase: str
    to_phase: str
    active_player: str
    turn: Optional[int] = None

# ==========================================
# 3. PRIORITY & PLAYER ACTION PDUs
# ==========================================

class PriorityGrantPDU(BasePDU):
    type: Literal["PRIORITY_GRANT"] = "PRIORITY_GRANT"
    player_id: str
    time_limit_ms: int = 60000

class PriorityPassPDU(BasePDU):
    type: Literal["PRIORITY_PASS"] = "PRIORITY_PASS"

class PlayLandPDU(BasePDU):
    type: Literal["PLAY_LAND"] = "PLAY_LAND"
    card_id: str

class CastSpellPDU(BasePDU):
    type: Literal["CAST_SPELL"] = "CAST_SPELL"
    card_id: str
    targets: List[str] = Field(default_factory=list)
    mana_payment: Dict[str, int]

class ActivateAbilityPDU(BasePDU):
    type: Literal["ACTIVATE_ABILITY"] = "ACTIVATE_ABILITY"
    source_id: str
    ability_index: int
    targets: List[str] = Field(default_factory=list)
    cost_payment: Dict[str, Any]

class DiscardPDU(BasePDU):
    type: Literal["DISCARD"] = "DISCARD"
    card_ids: List[str]

# ==========================================
# 4. STACK & TRIGGER PDUs
# ==========================================

class StackPushPDU(BasePDU):
    type: Literal["STACK_PUSH"] = "STACK_PUSH"
    stack_item_id: str
    item_type: Literal["SPELL", "ABILITY", "TRIGGER_ABILITY"]
    source: str
    targets: List[str] = Field(default_factory=list)
    controller: str

class StackResolvePDU(BasePDU):
    type: Literal["STACK_RESOLVE"] = "STACK_RESOLVE"
    stack_item_id: str
    result: Literal["RESOLVED", "FIZZLE"]
    state_changes: List[Dict[str, Any]] = Field(default_factory=list)

class TriggerOrderPDU(BasePDU):
    type: Literal["TRIGGER_ORDER"] = "TRIGGER_ORDER"
    player_id: str
    trigger_ids: List[str]

class TriggerOrderResponsePDU(BasePDU):
    type: Literal["TRIGGER_ORDER_RESPONSE"] = "TRIGGER_ORDER_RESPONSE"
    ordered_trigger_ids: List[str]

class TriggerChoicePDU(BasePDU):
    type: Literal["TRIGGER_CHOICE"] = "TRIGGER_CHOICE"
    trigger_id: str
    source_id: str
    effect_summary: str
    requires_target: bool = False
    legal_targets: Optional[List[str]] = None

class TriggerChoiceResponsePDU(BasePDU):
    type: Literal["TRIGGER_CHOICE_RESPONSE"] = "TRIGGER_CHOICE_RESPONSE"
    trigger_id: str
    accept: bool
    chosen_target: Optional[str] = None

# ==========================================
# 5. COMBAT PDUs
# ==========================================

class DeclareAttackersPDU(BasePDU):
    type: Literal["DECLARE_ATTACKERS"] = "DECLARE_ATTACKERS"
    attackers: List[AttackerTarget] = Field(default_factory=list)

class DeclareBlockersPDU(BasePDU):
    type: Literal["DECLARE_BLOCKERS"] = "DECLARE_BLOCKERS"
    blockers: List[BlockerAssignment] = Field(default_factory=list)

class AssignDamageOrderPDU(BasePDU):
    type: Literal["ASSIGN_DAMAGE_ORDER"] = "ASSIGN_DAMAGE_ORDER"
    attacker_id: str
    blocker_order: List[str]

class CombatDamageResultPDU(BasePDU):
    type: Literal["COMBAT_DAMAGE_RESULT"] = "COMBAT_DAMAGE_RESULT"
    damage_events: List[DamageEvent] = Field(default_factory=list)
    life_totals: Dict[str, int]
    creatures_died: List[str] = Field(default_factory=list)

# ==========================================
# 6. GENERAL / SYSTEM PDUs
# ==========================================

class ConcedePDU(BasePDU):
    type: Literal["CONCEDE"] = "CONCEDE"
    player_id: str

class GameOverPDU(BasePDU):
    type: Literal["GAME_OVER"] = "GAME_OVER"
    winner_id: str
    loser_id: str
    reason: Literal["LIFE_ZERO", "DECK_EMPTY", "CONCEDE", "DISCONNECT"]

class ErrorPDU(BasePDU):
    type: Literal["ERROR"] = "ERROR"
    code: str
    message: str
    rejected_action: Optional[Dict[str, Any]] = None

class PingPDU(BasePDU):
    type: Literal["PING"] = "PING"
    timestamp: int

class PongPDU(BasePDU):
    type: Literal["PONG"] = "PONG"
    timestamp: int


# ==========================================
# PDU MAPPER & PARSER FACTORY
# ==========================================

PDU_MAP = {
    "PLAYER_READY": PlayerReadyPDU,
    "GAME_STATE_UPDATE": GameStateUpdatePDU,
    "MULLIGAN_CHOICE": MulliganChoicePDU,
    "PHASE_TRANSITION": PhaseTransitionPDU,
    "PRIORITY_GRANT": PriorityGrantPDU,
    "PRIORITY_PASS": PriorityPassPDU,
    "CAST_SPELL": CastSpellPDU,
    "ACTIVATE_ABILITY": ActivateAbilityPDU,
    "STACK_PUSH": StackPushPDU,
    "TRIGGER_ORDER": TriggerOrderPDU,
    "TRIGGER_ORDER_RESPONSE": TriggerOrderResponsePDU,
    "TRIGGER_CHOICE": TriggerChoicePDU,
    "TRIGGER_CHOICE_RESPONSE": TriggerChoiceResponsePDU,
    "STACK_RESOLVE": StackResolvePDU,
    "DECLARE_ATTACKERS": DeclareAttackersPDU,
    "DECLARE_BLOCKERS": DeclareBlockersPDU,
    "ASSIGN_DAMAGE_ORDER": AssignDamageOrderPDU,
    "COMBAT_DAMAGE_RESULT": CombatDamageResultPDU,
    "PLAY_LAND": PlayLandPDU,
    "DISCARD": DiscardPDU,
    "CONCEDE": ConcedePDU,
    "GAME_OVER": GameOverPDU,
    "ERROR": ErrorPDU,
    "PING": PingPDU,
    "PONG": PongPDU,
}

def parse_pdu(raw_dict: Dict[str, Any]) -> BasePDU:
    """
    Parses a raw dictionary into its corresponding Pydantic PDU model.
    Raises ValueError or ValidationError if parsing fails.
    """
    pdu_type = raw_dict.get("type")
    if not pdu_type or pdu_type not in PDU_MAP:
        raise ValueError(f"UNKNOWN_TYPE: {pdu_type}")
    
    model_cls = PDU_MAP[pdu_type]
    return model_cls(**raw_dict)


# ===========================================================================
# ENGINE COMPATIBILITY LAYER  (added by Ren -- server/engine/* modules)
# ===========================================================================
#
# Everything below this line didn't exist in Andi's original shared/pdu.py, but is added 
# here to make the other engine part works, tintamad lng me to change the file names 
# added side by side with no behavior change to either.
# ---------------------------------------------------------------------------


class PDUType:
    """String constants for all 25 PDU types (RFC Section 10.1)."""
    PLAYER_READY = "PLAYER_READY"
    GAME_STATE_UPDATE = "GAME_STATE_UPDATE"
    MULLIGAN_CHOICE = "MULLIGAN_CHOICE"
    PHASE_TRANSITION = "PHASE_TRANSITION"
    PRIORITY_GRANT = "PRIORITY_GRANT"
    PRIORITY_PASS = "PRIORITY_PASS"
    CAST_SPELL = "CAST_SPELL"
    ACTIVATE_ABILITY = "ACTIVATE_ABILITY"
    STACK_PUSH = "STACK_PUSH"
    TRIGGER_ORDER = "TRIGGER_ORDER"
    TRIGGER_ORDER_RESPONSE = "TRIGGER_ORDER_RESPONSE"
    TRIGGER_CHOICE = "TRIGGER_CHOICE"
    TRIGGER_CHOICE_RESPONSE = "TRIGGER_CHOICE_RESPONSE"
    STACK_RESOLVE = "STACK_RESOLVE"
    DECLARE_ATTACKERS = "DECLARE_ATTACKERS"
    DECLARE_BLOCKERS = "DECLARE_BLOCKERS"
    ASSIGN_DAMAGE_ORDER = "ASSIGN_DAMAGE_ORDER"
    COMBAT_DAMAGE_RESULT = "COMBAT_DAMAGE_RESULT"
    PLAY_LAND = "PLAY_LAND"
    DISCARD = "DISCARD"
    CONCEDE = "CONCEDE"
    GAME_OVER = "GAME_OVER"
    ERROR = "ERROR"
    PING = "PING"
    PONG = "PONG"


class ErrorCode:
    """String constants for all ERROR codes (RFC Section 11)."""
    INVALID_JSON = "INVALID_JSON"
    ILLEGAL_DECK = "ILLEGAL_DECK"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    STALE_ACTION = "STALE_ACTION"
    NOT_YOUR_PRIORITY = "NOT_YOUR_PRIORITY"
    ILLEGAL_ACTION = "ILLEGAL_ACTION"
    ILLEGAL_TARGET = "ILLEGAL_TARGET"
    TRIGGER_ORDER_INVALID = "TRIGGER_ORDER_INVALID"
    TRIGGER_CHOICE_INVALID = "TRIGGER_CHOICE_INVALID"
    INSUFFICIENT_MANA = "INSUFFICIENT_MANA"
    WRONG_PHASE = "WRONG_PHASE"
    DUPLICATE_ID = "DUPLICATE_ID"


class ItemType:
    """Stack item types (RFC Section 8.3). Matches StackPushPDU's Literal."""
    SPELL = "SPELL"
    ABILITY = "ABILITY"
    TRIGGER_ABILITY = "TRIGGER_ABILITY"


class ResolveResult:
    """Matches StackResolvePDU's Literal."""
    RESOLVED = "RESOLVED"
    FIZZLE = "FIZZLE"


class Phase:
    """
    Phase/step constants and ordering (RFC Section 10.2.4 / Figure 4).

    NOTE: this ordering/grouping logic doesn't exist anywhere in Andi's
    PhaseTransitionPDU (which just accepts free-form strings) -- it's
    genuinely turn-engine-specific, so it lives here rather than being
    something to reconcile against Andi's code.
    """
    UNTAP = "UNTAP"
    UPKEEP = "UPKEEP"
    DRAW = "DRAW"
    PRECOMBAT_MAIN = "PRECOMBAT_MAIN"
    BEGIN_COMBAT = "BEGIN_COMBAT"
    DECLARE_ATTACKERS = "DECLARE_ATTACKERS"
    DECLARE_BLOCKERS = "DECLARE_BLOCKERS"
    ASSIGN_DAMAGE_ORDER = "ASSIGN_DAMAGE_ORDER"
    FIRST_STRIKE_DAMAGE = "FIRST_STRIKE_DAMAGE"
    COMBAT_DAMAGE = "COMBAT_DAMAGE"
    END_OF_COMBAT = "END_OF_COMBAT"
    POSTCOMBAT_MAIN = "POSTCOMBAT_MAIN"
    END_STEP = "END_STEP"
    CLEANUP = "CLEANUP"

    ORDER = [
        UNTAP, UPKEEP, DRAW, PRECOMBAT_MAIN,
        BEGIN_COMBAT, DECLARE_ATTACKERS, DECLARE_BLOCKERS,
        ASSIGN_DAMAGE_ORDER, FIRST_STRIKE_DAMAGE, COMBAT_DAMAGE,
        END_OF_COMBAT, POSTCOMBAT_MAIN, END_STEP, CLEANUP,
    ]

    PRIORITY_STEPS = {
        UPKEEP, DRAW, PRECOMBAT_MAIN, BEGIN_COMBAT,
        DECLARE_ATTACKERS, DECLARE_BLOCKERS, ASSIGN_DAMAGE_ORDER,
        FIRST_STRIKE_DAMAGE, COMBAT_DAMAGE, END_OF_COMBAT,
        POSTCOMBAT_MAIN, END_STEP,
    }

    MAIN_PHASES = {PRECOMBAT_MAIN, POSTCOMBAT_MAIN}


# ---------------------------------------------------------------------------
# Builder functions -- construct the real Pydantic model, return a plain
# dict via .to_dict(). This means every PDU your engine sends is still
# validated by the models made above
# ---------------------------------------------------------------------------

def build_phase_transition(seq_num: int, from_phase: str, to_phase: str,
                            active_player: str, turn: int) -> dict:
    return PhaseTransitionPDU(
        seq_num=seq_num, from_phase=from_phase, to_phase=to_phase,
        active_player=active_player, turn=turn,
    ).to_dict()


def build_priority_grant(seq_num: int, player_id: str,
                          time_limit_ms: int = 60000) -> dict:
    return PriorityGrantPDU(
        seq_num=seq_num, player_id=player_id, time_limit_ms=time_limit_ms,
    ).to_dict()


def build_stack_push(seq_num: int, stack_item_id: str, item_type: str,
                      source: str, targets: list, controller: str) -> dict:
    return StackPushPDU(
        seq_num=seq_num, stack_item_id=stack_item_id, item_type=item_type,
        source=source, targets=targets, controller=controller,
    ).to_dict()


def build_stack_resolve(seq_num: int, stack_item_id: str, result: str,
                         state_changes: Optional[list] = None) -> dict:
    return StackResolvePDU(
        seq_num=seq_num, stack_item_id=stack_item_id, result=result,
        state_changes=state_changes or [],
    ).to_dict()


def build_combat_damage_result(seq_num: int, damage_events: list,
                                life_totals: dict, creatures_died: list) -> dict:
    return CombatDamageResultPDU(
        seq_num=seq_num, damage_events=damage_events,
        life_totals=life_totals, creatures_died=creatures_died,
    ).to_dict()


def build_game_state_update(seq_num: int, state: dict) -> dict:
    return GameStateUpdatePDU(seq_num=seq_num, state=state).to_dict()


def build_game_over(seq_num: int, winner_id: str, loser_id: str,
                     reason: str) -> dict:
    return GameOverPDU(
        seq_num=seq_num, winner_id=winner_id, loser_id=loser_id, reason=reason,
    ).to_dict()


def build_error(seq_num: int, code: str, message: str,
                 rejected_action: Optional[dict] = None) -> dict:
    return ErrorPDU(
        seq_num=seq_num, code=code, message=message,
        rejected_action=rejected_action,
    ).to_dict()


def build_trigger_order(seq_num: int, player_id: str,
                         trigger_ids: list) -> dict:
    return TriggerOrderPDU(
        seq_num=seq_num, player_id=player_id, trigger_ids=trigger_ids,
    ).to_dict()


def build_trigger_choice(seq_num: int, trigger_id: str, source_id: str,
                          effect_summary: str, requires_target: bool = False,
                          legal_targets: Optional[list] = None) -> dict:
    return TriggerChoicePDU(
        seq_num=seq_num, trigger_id=trigger_id, source_id=source_id,
        effect_summary=effect_summary, requires_target=requires_target,
        legal_targets=legal_targets or [],
    ).to_dict()


def build_pong(seq_num: int, timestamp: int) -> dict:
    return PongPDU(seq_num=seq_num, timestamp=timestamp).to_dict()