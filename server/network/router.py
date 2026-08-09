from shared.pdu import PDUType, BasePDU
from server.engine.priority import PriorityError

# RFC 5.4: these client-to-server PDUs happen inside an open priority
# window and must echo the CURRENT PRIORITY_GRANT's seq_num, from the
# player who currently holds priority. DECLARE_ATTACKERS/BLOCKERS/
# ASSIGN_DAMAGE_ORDER are priority-bearing per the RFC too, but their
# "token" is the PHASE_TRANSITION seq_num, not a PRIORITY_GRANT -- they
# are validated by CombatManager itself (active/non-active player
# checks), not by PriorityManager.validate_action().
PRIORITY_WINDOW_TYPES = {
    PDUType.PRIORITY_PASS,
    PDUType.CAST_SPELL,
    PDUType.ACTIVATE_ABILITY,
    PDUType.PLAY_LAND,
}


def route_gameplay_pdu(pdu: BasePDU, player_id: str, turn_manager, stack_manager,
                        priority_manager, combat_manager=None):
    """Routes validated PDUs from the TCP server to the specific game engine managers."""

    if pdu.type in PRIORITY_WINDOW_TYPES:
        if priority_manager is None:
            print("[ROUTER] PriorityManager not initialized.")
            return
        try:
            # RFC 5.4 / Section 11: reject on STALE_ACTION / NOT_YOUR_PRIORITY
            # BEFORE dispatching to the owning manager. This was previously
            # never called, so a client could act without holding priority
            # or with a stale seq_num and it would go straight through.
            priority_manager.validate_action(player_id, pdu.to_dict())
        except PriorityError as err:
            priority_manager.reject(player_id, err)
            return

    # Priority and Turn Actions
    if pdu.type == PDUType.PRIORITY_PASS:
        # NOTE: PriorityManager defines handle_pass(), not pass_priority().
        priority_manager.handle_pass(player_id)

    elif pdu.type == PDUType.PLAY_LAND:
        if turn_manager:
            turn_manager.handle_play_land(player_id, pdu.to_dict())
        else:
            print("[ROUTER] TurnManager not initialized.")

    # Stack Actions
    elif pdu.type == PDUType.CAST_SPELL:
        if stack_manager:
            stack_manager.handle_cast_spell(player_id, pdu.to_dict())
        else:
            print("[ROUTER] StackManager not initialized.")

    elif pdu.type == PDUType.ACTIVATE_ABILITY:
        if stack_manager:
            stack_manager.handle_activate_ability(player_id, pdu.to_dict())
        else:
            print("[ROUTER] StackManager not initialized.")

    elif pdu.type == PDUType.DISCARD:
        if turn_manager:
            turn_manager.handle_discard(player_id, pdu.to_dict())
        else:
            print("[ROUTER] TurnManager not initialized.")

    # Combat Actions
    elif pdu.type == PDUType.DECLARE_ATTACKERS:
        if combat_manager:
            combat_manager.handle_declare_attackers(player_id, pdu.to_dict())
        else:
            print("[ROUTER] CombatManager not initialized.")

    elif pdu.type == PDUType.DECLARE_BLOCKERS:
        if combat_manager:
            combat_manager.handle_declare_blockers(player_id, pdu.to_dict())
        else:
            print("[ROUTER] CombatManager not initialized.")

    elif pdu.type == PDUType.ASSIGN_DAMAGE_ORDER:
        if combat_manager:
            combat_manager.handle_assign_damage_order(player_id, pdu.to_dict())
        else:
            print("[ROUTER] CombatManager not initialized.")

    else:
        print(f"[ROUTER] Unhandled gameplay PDU type: {pdu.type}")