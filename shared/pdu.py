from typing import List, Dict, Any, Optional, Union, Literal
from pydantic import BaseModel, Field, ValidationError

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