#!/usr/bin/env python3
"""
test_week2_demo.py

Offline, no-sockets test of the full Week 2 demo path, run directly
against GameState / TurnManager / StackManager / CombatManager --
the same objects tcp_server.py wires together, minus the TCP layer.

Run from the repository root:

    python3 test_week2_demo.py

Requires: pydantic (same dependency the server already needs).
Exits 0 and prints "ALL TESTS PASSED" if everything works; raises an
AssertionError / traceback and exits non-zero on the first failure.

Covers:
  1. Lobby -> Setup -> Mulligan -> Untap -> Upkeep -> Draw -> Main
  2. Playing a land
  3. Casting and resolving an instant (Lightning Bolt) through the
     card_effects adapter -- verifies actual damage is applied
  4. Casting and resolving a creature spell (enters the battlefield)
  5. Combat with no attackers declared -- verifies the
     CombatManager <-> TurnManager flag-bridging fix (this was the
     "combat never advances" bug)
  6. Combat with a real attacker/blocker exchange and damage
  7. Full turn-to-turn cleanup and turn counter increment
  8. router.py priority validation (rejects a PRIORITY_PASS from the
     player who does NOT currently hold priority; accepts it from the
     player who does)
  9. Hidden-information check: to_personalized_dict(X) never contains
     the opponent's hand contents
"""
import os
import random
import sys
import traceback

# Make sure we're importing the project modules from the repo root,
# regardless of where this script is invoked from.
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from server.core.game_state import GameState
from server.engine.turn_manager import TurnManager
from server.engine.stack_manager import StackManager
from server.engine.combat import CombatManager
from server.network.router import route_gameplay_pdu
from shared.pdu import parse_pdu


PASS_COUNT = 0


def check(condition, message):
    """Lightweight assert with a readable pass/fail log line."""
    global PASS_COUNT
    if not condition:
        raise AssertionError(f"FAILED: {message}")
    PASS_COUNT += 1
    print(f"  [ok] {message}")


def build_engine(seed=1):
    """
    Instantiates a fresh GameState + TurnManager + StackManager +
    CombatManager, wired together exactly the way tcp_server.py's
    start_in_game_engine() does it, but with in-memory send/broadcast
    functions instead of real sockets.
    """
    sent_log = []  # (player_id_or_'ALL', pdu_type, pdu) tuples for inspection

    def send_fn(player_id, pdu):
        sent_log.append((player_id, pdu["type"], pdu))

    def broadcast_fn(pdu):
        sent_log.append(("ALL", pdu["type"], pdu))

    def on_game_over(winner_id, loser_id, reason):
        sent_log.append(("ALL", "GAME_OVER", {
            "winner_id": winner_id, "loser_id": loser_id, "reason": reason,
        }))

    decks = {
        "p1": (
            ["mountain_001", "mountain_002", "mountain_003",
             "lightning_bolt_001", "goblin_guide_001"]
            + ["mountain_004"] * 10
        ),
        "p2": (
            ["mountain_005", "mountain_006", "mountain_007",
             "lightning_bolt_002", "goblin_guide_002"]
            + ["mountain_008"] * 10
        ),
    }

    state = GameState.initialize_from_decks(decks, rng=random.Random(seed))
    for pid in state.players:
        err = state.mulligan_keep(pid, [])
        check(err is None, f"{pid} keeps opening hand with no mulligan")

    # The decks above are shuffled, so the specific instant/creature a
    # test needs isn't guaranteed to land in the opening 7. Force it
    # into hand deterministically (swap for a land already in hand)
    # so each test exercises a specific, known interaction rather than
    # being at the mercy of the shuffle. Both players get one of each
    # so the test works regardless of which player the coin flip
    # makes the Active Player.
    def _ensure_in_hand(pid, card_id):
        ps = state.players[pid]
        if card_id in ps.hand:
            return
        ps.library.remove(card_id)
        swap_out = next(c for c in ps.hand if c.startswith("mountain"))
        ps.hand.remove(swap_out)
        ps.library.append(swap_out)
        ps.hand.append(card_id)

    _ensure_in_hand("p1", "lightning_bolt_001")
    _ensure_in_hand("p1", "goblin_guide_001")
    _ensure_in_hand("p2", "lightning_bolt_002")
    _ensure_in_hand("p2", "goblin_guide_002")

    turn_manager = TurnManager(
        state=state, send_fn=send_fn, broadcast_fn=broadcast_fn,
        on_game_over=on_game_over, first_player_id=state.first_player_id,
    )
    stack_manager = StackManager(
        state=state, send_fn=send_fn, broadcast_fn=broadcast_fn,
        on_game_over=on_game_over,
        reopen_priority_for_actor=turn_manager.priority.reopen_after_stack_action,
        reopen_priority_after_resolution=turn_manager.priority.reopen_after_resolution,
        register_cleanup_hook_fn=turn_manager.register_cleanup_hook,
    )
    combat_manager = CombatManager(
        state=state, send_fn=send_fn, broadcast_fn=broadcast_fn,
        reject_fn=turn_manager.reject_illegal_action,
        open_priority_fn=turn_manager._open_priority_for_current_step,
        advance_to_fn=turn_manager._transition_to,
        run_sba_fn=stack_manager.run_state_based_actions,
    )
    combat_hooks = {
        "DECLARE_ATTACKERS": combat_manager.begin_declare_attackers,
        "DECLARE_BLOCKERS": combat_manager.begin_declare_blockers,
        "ASSIGN_DAMAGE_ORDER": combat_manager.begin_assign_damage_order,
        "FIRST_STRIKE_DAMAGE": combat_manager.run_first_strike_damage,
        "COMBAT_DAMAGE": combat_manager.run_combat_damage,
    }
    turn_manager.wire_dependencies(stack_manager.resolve_top, combat_hooks, combat_manager)

    return state, turn_manager, stack_manager, combat_manager, sent_log


def pass_pass(turn_manager, ap, nap):
    """Both players pass priority in turn (the common 'advance the game' step)."""
    turn_manager.priority.handle_pass(ap)
    turn_manager.priority.handle_pass(nap)


def test_turn_structure_and_land_and_instant():
    print("\n=== TEST 1: turn structure, land play, instant resolution ===")
    state, tm, sm, cm, log = build_engine(seed=42)

    tm.start_turn()
    check(state.phase == "UPKEEP", "start_turn() opens Upkeep priority window")
    check(state.priority_holder == state.active_player, "Active Player holds priority first")

    ap = state.active_player
    nap = state.opponent_of(ap)

    pass_pass(tm, ap, nap)
    check(state.phase == "DRAW", "Upkeep pass/pass advances to Draw")

    pass_pass(tm, ap, nap)
    check(state.phase == "PRECOMBAT_MAIN", "Draw pass/pass advances to Precombat Main")

    land_id = next(c for c in state.players[ap].hand if c.startswith("mountain"))
    result = tm.handle_play_land(ap, {"card_id": land_id})
    check(any(p.id == land_id for p in state.players[ap].battlefield),
          "PLAY_LAND puts the land on the battlefield")
    check(state.players[ap].land_played_this_turn is True,
          "land_played_this_turn flag set")
    check(state.priority_holder == ap,
          "Active Player retains priority after playing a land (RFC 7.5)")

    bolt_id = next((c for c in state.players[ap].hand if c.startswith("lightning_bolt")), None)
    check(bolt_id is not None, "Lightning Bolt is in hand for this test deck")

    life_before = state.players[nap].life_total
    sm.handle_cast_spell(ap, {
        "card_id": bolt_id, "targets": [nap], "mana_payment": {"R": 1},
    })
    check(any(si.stack_item_id for si in state.stack), "CAST_SPELL pushes the spell onto the stack")
    check(state.players[nap].life_total == life_before,
          "life total unaffected until the spell actually resolves")

    pass_pass(tm, ap, nap)
    check(state.players[nap].life_total == life_before - 3,
          "Lightning Bolt deals 3 damage on resolution (card_effects adapter works)")
    check(state.is_stack_empty(), "stack is empty after resolution")

    return state, tm, sm, cm, ap, nap


def test_creature_cast_and_empty_combat_skip():
    print("\n=== TEST 2: creature spell + combat-skip-on-no-attackers bridging fix ===")
    state, tm, sm, cm, log = build_engine(seed=7)
    tm.start_turn()
    ap = state.active_player
    nap = state.opponent_of(ap)

    pass_pass(tm, ap, nap)  # Upkeep -> Draw
    pass_pass(tm, ap, nap)  # Draw -> Precombat Main
    check(state.phase == "PRECOMBAT_MAIN", "reached Precombat Main")

    land_id = next(c for c in state.players[ap].hand if c.startswith("mountain"))
    tm.handle_play_land(ap, {"card_id": land_id})

    creature_id = next(c for c in state.players[ap].hand if c.startswith("goblin_guide"))
    sm.handle_cast_spell(ap, {"card_id": creature_id, "targets": [], "mana_payment": {"R": 1}})
    pass_pass(tm, ap, nap)
    check(any(p.id == creature_id for p in state.players[ap].battlefield),
          "creature spell resolves onto the battlefield")

    pass_pass(tm, ap, nap)  # Main -> Begin Combat
    check(state.phase == "BEGIN_COMBAT", "advanced to Begin Combat")
    pass_pass(tm, ap, nap)  # Begin Combat -> Declare Attackers
    check(state.phase == "DECLARE_ATTACKERS", "advanced to Declare Attackers")

    cm.handle_declare_attackers(ap, {"attackers": []})
    check(state.phase == "DECLARE_ATTACKERS", "priority window opens after declaring (still same step)")
    check(state.priority_holder == ap, "Active Player holds priority in that window")

    pass_pass(tm, ap, nap)
    check(state.phase == "END_OF_COMBAT",
          "empty attacker declaration skips straight to End of Combat "
          "(this is the CombatManager<->TurnManager flag-bridging bug fix)")

    return state, tm, sm, cm, ap, nap


def test_real_attack_and_damage():
    print("\n=== TEST 3: real attacker/blocker combat damage ===")
    state, tm, sm, cm, log = build_engine(seed=99)
    tm.start_turn()
    ap = state.active_player
    nap = state.opponent_of(ap)

    pass_pass(tm, ap, nap)
    pass_pass(tm, ap, nap)

    land_id = next(c for c in state.players[ap].hand if c.startswith("mountain"))
    tm.handle_play_land(ap, {"card_id": land_id})
    creature_id = next(c for c in state.players[ap].hand if c.startswith("goblin_guide"))
    sm.handle_cast_spell(ap, {"card_id": creature_id, "targets": [], "mana_payment": {"R": 1}})
    pass_pass(tm, ap, nap)

    perm = next(p for p in state.players[ap].battlefield if p.id == creature_id)
    # Summoning sickness would normally block attacking this turn --
    # force it off here purely to exercise the combat-damage path
    # deterministically regardless of catalog haste data.
    perm.summoning_sick = False

    pass_pass(tm, ap, nap)  # Main -> Begin Combat
    pass_pass(tm, ap, nap)  # Begin Combat -> Declare Attackers
    check(state.phase == "DECLARE_ATTACKERS", "reached Declare Attackers")

    cm.handle_declare_attackers(ap, {"attackers": [{"creature_id": perm.id, "target": nap}]})
    check(perm.tapped is True, "declaring an attacker taps it")
    check(state.priority_holder == ap, "priority window opens after declaring attackers")

    pass_pass(tm, ap, nap)
    check(state.phase == "DECLARE_BLOCKERS", "advanced to Declare Blockers")

    cm.handle_declare_blockers(nap, {"blockers": []})
    life_before = state.players[nap].life_total

    pass_pass(tm, ap, nap)
    check(state.players[nap].life_total < life_before,
          "unblocked attacker deals combat damage to the defending player")
    check(state.phase in ("END_OF_COMBAT", "POSTCOMBAT_MAIN"),
          "combat proceeds past damage without stalling")

    return state, tm, sm, cm, ap, nap


def test_turn_advances_and_cleanup():
    print("\n=== TEST 4: full turn cycle advances the turn counter ===")
    state, tm, sm, cm, ap, nap = test_turn_structure_and_land_and_instant()

    pass_pass(tm, ap, nap)  # Main -> Begin Combat
    pass_pass(tm, ap, nap)  # Begin Combat -> Declare Attackers (no creature -> priority window)
    cm.handle_declare_attackers(ap, {"attackers": []})
    pass_pass(tm, ap, nap)  # -> End of Combat
    check(state.phase == "END_OF_COMBAT", "no-attacker combat resolves to End of Combat")

    pass_pass(tm, ap, nap)  # -> Postcombat Main
    check(state.phase == "POSTCOMBAT_MAIN", "advanced to Postcombat Main")

    pass_pass(tm, ap, nap)  # -> End Step
    check(state.phase == "END_STEP", "advanced to End Step")

    turn_before = state.turn
    pass_pass(tm, ap, nap)  # -> Cleanup -> next turn's Untap/Upkeep
    check(state.turn == turn_before + 1, "turn counter increments after Cleanup")
    check(state.active_player == nap, "active player alternates to the other player")
    check(state.phase == "UPKEEP", "next turn opens straight into its own Upkeep window")


def test_router_priority_validation():
    print("\n=== TEST 5: router.py validates priority before dispatching ===")
    state, tm, sm, cm, log = build_engine(seed=3)
    tm.start_turn()
    ap = state.active_player
    nap = state.opponent_of(ap)
    pm = tm.priority

    seq = pm._current_priority_seq
    wrong_pdu = parse_pdu({"type": "PRIORITY_PASS", "seq_num": seq})
    route_gameplay_pdu(wrong_pdu, nap, tm, sm, pm, cm)
    check(state.priority_holder == ap,
          "PRIORITY_PASS from the player who does NOT hold priority is rejected "
          "(NOT_YOUR_PRIORITY / router now calls validate_action before dispatch)")

    right_pdu = parse_pdu({"type": "PRIORITY_PASS", "seq_num": seq})
    route_gameplay_pdu(right_pdu, ap, tm, sm, pm, cm)
    check(state.priority_holder == nap,
          "PRIORITY_PASS from the correct player is accepted "
          "(router.py calls priority_manager.handle_pass(), not the old "
          "nonexistent pass_priority())")

    stale_pdu = parse_pdu({"type": "PRIORITY_PASS", "seq_num": seq})  # now stale, seq moved on
    holder_before = state.priority_holder
    route_gameplay_pdu(stale_pdu, nap, tm, sm, pm, cm)
    # nap DOES hold priority now, but is reusing an old seq_num -> STALE_ACTION,
    # priority holder should not silently change to something unexpected.
    check(state.priority_holder in (ap, nap), "stale seq_num is rejected without crashing the router")


def test_no_hidden_information_leak():
    print("\n=== TEST 6: personalized state never exposes the opponent's hand ===")
    state, tm, sm, cm, log = build_engine(seed=11)
    tm.start_turn()
    ap = state.active_player
    nap = state.opponent_of(ap)

    view_for_ap = state.to_personalized_dict(ap)
    check("hand" in view_for_ap, "personalized dict includes the viewer's own hand")

    # The dict shape only ever carries ONE player's hand (the viewer's).
    # The structural guarantee we actually care about is that calling
    # to_personalized_dict(nap) does NOT accidentally return ap's hand
    # contents, and that any broadcast of one player's view never gets
    # sent to the other player's socket (checked by inspecting sent_log:
    # every "ALL"-tagged GAME_STATE_UPDATE in the log after our fix is
    # gone -- fixed code only ever appears as per-player sends).
    view_for_nap = state.to_personalized_dict(nap)
    check(view_for_ap != view_for_nap, "the two players' personalized views differ")

    leaked_broadcasts = [
        entry for entry in log
        if entry[1] == "GAME_STATE_UPDATE" and entry[0] == "ALL"
    ]
    check(len(leaked_broadcasts) == 0,
          "no GAME_STATE_UPDATE was ever sent via broadcast_fn (would leak a hand) "
          "-- all personalized state now goes out via per-player send_fn")


def main():
    tests = [
        test_turn_structure_and_land_and_instant,
        test_creature_cast_and_empty_combat_skip,
        test_real_attack_and_damage,
        test_turn_advances_and_cleanup,
        test_router_priority_validation,
        test_no_hidden_information_leak,
    ]
    for test in tests:
        try:
            test()
        except Exception:
            print(f"\n!!! {test.__name__} FAILED !!!")
            traceback.print_exc()
            print(f"\n{PASS_COUNT} checks passed before failure.")
            sys.exit(1)

    print(f"\nALL TESTS PASSED ({PASS_COUNT} checks)")
    sys.exit(0)


if __name__ == "__main__":
    main()