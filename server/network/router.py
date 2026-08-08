from shared.pdu import PDUType, BasePDU

def route_gameplay_pdu(pdu: BasePDU, player_id: str, turn_manager, stack_manager, priority_manager):
    """Routes validated PDUs from the TCP server to the specific game engine managers."""
    
    # Priority and Turn Actions
    if pdu.type == PDUType.PRIORITY_PASS:
        if priority_manager: 
            priority_manager.pass_priority(player_id)
        else:
            print("[ROUTER] PriorityManager not initialized.")
            
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
            
    # Combat Actions for later integration
    elif pdu.type == PDUType.DECLARE_ATTACKERS:
        print(f"[ROUTER] Routing DECLARE_ATTACKERS for {player_id}")
        
    elif pdu.type == PDUType.DECLARE_BLOCKERS:
        print(f"[ROUTER] Routing DECLARE_BLOCKERS for {player_id}")
        
    elif pdu.type == PDUType.ASSIGN_DAMAGE_ORDER:
        print(f"[ROUTER] Routing ASSIGN_DAMAGE_ORDER for {player_id}")
        
    else:
        print(f"[ROUTER] Unhandled gameplay PDU type: {pdu.type}")