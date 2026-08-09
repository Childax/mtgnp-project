import json
import os

current_dir = os.path.dirname(__file__)
catalog_path = os.path.abspath(os.path.join(current_dir, '../../shared/data/card_catalog.json'))
with open(catalog_path, "r") as f:
    CARD_CATALOG = json.load(f)

def resolve_direct_damage(server_state, target, damage_amount):
    """Base function used by Lightning Bolt (3) and Shock (2)."""
    if target in server_state.get("life_totals", {}):
        server_state["life_totals"][target] -= damage_amount
    else:
        for pid, board in server_state.get("battlefield", {}).items():
            for perm in board:
                if perm["id"] == target:
                    perm["damage"] = perm.get("damage", 0) + damage_amount
                    return server_state
    return server_state

def resolve_lightning_bolt(server_state, controller_id, targets):
    if not targets: return server_state
    return resolve_direct_damage(server_state, targets[0], 3)

def resolve_shock(server_state, controller_id, targets):
    if not targets: return server_state
    return resolve_direct_damage(server_state, targets[0], 2)

def resolve_giant_growth(server_state, controller_id, targets):
    """Target creature gets +3/+3 until end of turn."""
    if not targets: return server_state
    target = targets[0]

    for pid, board in server_state.get("battlefield", {}).items():
        for perm in board:
            if perm["id"] == target:
                perm["power"] = perm.get("power", 0) + 3
                perm["toughness"] = perm.get("toughness", 0) + 3
                
                if "temp_buffs" not in perm:
                    perm["temp_buffs"] = []
                perm["temp_buffs"].append({"power": 3, "toughness": 3})
                return server_state
    return server_state

def resolve_unsummon(server_state, controller_id, targets):
    """Return target creature to its owner's hand."""
    if not targets: return server_state
    target = targets[0]

    for pid, board in server_state.get("battlefield", {}).items():
        for i, perm in enumerate(board):
            if perm["id"] == target:
                removed_card = board.pop(i)
                if pid not in server_state["hand"]:
                    server_state["hand"][pid] = []
                server_state["hand"][pid].append(removed_card["id"])
                return server_state
    return server_state

def resolve_doom_blade(server_state, controller_id, targets):
    """Destroy target nonblack creature."""
    if not targets: return server_state
    target = targets[0]

    # The catalog is keyed by full card instance IDs (e.g.
    # 'grizzly_bears_001'), not base names, so look the target up directly.
    catalog_entry = CARD_CATALOG.get(target)
    
    if not catalog_entry: 
        return server_state # Invalid target

    # Cross-reference with the catalog's "color" key
    if catalog_entry.get("color") == "B":
        print(f"[ENGINE] Spell fizzled: {target} is black.")
        return server_state # Spell fails to resolve legally

    for pid, board in server_state.get("battlefield", {}).items():
        for i, perm in enumerate(board):
            if perm["id"] == target:
                removed_card = board.pop(i)
                if pid not in server_state["graveyard"]:
                    server_state["graveyard"][pid] = []
                server_state["graveyard"][pid].append(removed_card["id"])
                return server_state
    return server_state

def resolve_terror(server_state, controller_id, targets):
    """Destroy target nonartifact, nonblack creature."""
    if not targets: return server_state
    target = targets[0]

    # BUGFIX: this previously looked up CARD_CATALOG by the target's
    # base name (e.g. 'grizzly_bears'), but the catalog is keyed by
    # full card instance IDs (e.g. 'grizzly_bears_001'). That lookup
    # always returned None, so Terror silently fizzled on every legal
    # target. Look the target up directly, same as resolve_doom_blade.
    catalog_entry = CARD_CATALOG.get(target)
    
    if not catalog_entry: 
        return server_state 

    # Fizzle if the target is Black OR an Artifact
    if catalog_entry.get("color") == "B":
        print(f"[ENGINE] Spell fizzled: {target} is black.")
        return server_state 
        
    if "Artifact" in catalog_entry.get("type", ""):
        print(f"[ENGINE] Spell fizzled: {target} is an artifact.")
        return server_state 

    for pid, board in server_state.get("battlefield", {}).items():
        for i, perm in enumerate(board):
            if perm["id"] == target:
                removed_card = board.pop(i)
                if pid not in server_state["graveyard"]:
                    server_state["graveyard"][pid] = []
                server_state["graveyard"][pid].append(removed_card["id"])
                return server_state
    return server_state

def resolve_counterspell(server_state, controller_id, targets):
    """Counter target spell."""
    if not targets: return server_state
    target_stack_id = targets[0] 

    stack = server_state.get("stack", [])
    for i, item in enumerate(stack):
        if item["stack_item_id"] == target_stack_id:
            countered_spell = stack.pop(i)
            owner = countered_spell["controller"]
            
            if owner not in server_state["graveyard"]:
                server_state["graveyard"][owner] = []
            server_state["graveyard"][owner].append(countered_spell["source"])
            return server_state
    return server_state

# Mapping of card names to their corresponding effect resolution functions
EFFECT_REGISTRY = {
    "lightning_bolt": resolve_lightning_bolt,
    "shock": resolve_shock,
    "giant_growth": resolve_giant_growth,
    "unsummon": resolve_unsummon,
    "doom_blade": resolve_doom_blade,
    "terror": resolve_terror, 
    "counterspell": resolve_counterspell
}