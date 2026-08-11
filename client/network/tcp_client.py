"""
MTGNP TCP client implementation.

Handles the client-side network connection, heartbeat monitoring,
server PDU processing, reconnect state restoration, user prompts,
and construction of gameplay PDUs.

The server remains authoritative: this client renders received state
and sends player requests without independently determining game results.
"""

import socket
import json
import threading
import time
import argparse
import sys
import os
import builtins
import struct

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from shared.network_utils import receive_exact, send_pdu
from client.ui.battlefield import BattlefieldUI

# ---------------------------------------------------------------------------
# Client configuration and shared runtime state
# ---------------------------------------------------------------------------
# These values are updated by the listener thread as authoritative PDUs arrive
# from the server. Threading events wake the main input loop when the client
# needs to respond to priority, combat, mulligan, reconnect, or game-over state.
HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False
input_active = False
verbose_buffer = []
verbose_lock = threading.Lock()
last_pong_time = time.time()
latest_server_seq = 0
latest_mulligan_hand = []
latest_mulligan_count = 0
mulligan_state_received = threading.Event()
latest_priority_seq = 0
priority_grant_received = threading.Event()
latest_phase = "LOBBY"
latest_hand = []
latest_active_player = None
latest_battlefield = {}
latest_life_totals = {}
latest_game_state = {}
latest_phase_seq = 0
attackers_request_received = threading.Event()
blockers_request_received = threading.Event()
latest_attackers = []
latest_combat_blockers = {}
damage_order_request_received = threading.Event()
latest_cleanup_seq = 0
discard_request_received = threading.Event()
game_over_received = threading.Event()
connection_lost_received = threading.Event()
reconnect_state_received = threading.Event()

# ---------------------------------------------------------------------------
# Console input and verbose-log synchronization
# ---------------------------------------------------------------------------
# Network and heartbeat threads may receive PDUs while the user is typing.
# Verbose messages are temporarily buffered during input so background logs
# do not overwrite or split interactive prompts.
def buffered_verbose_print(*lines):
    """Print verbose logs immediately, or buffer them while input is active."""
    global input_active

    if not VERBOSE:
        return

    with verbose_lock:
        if input_active:
            verbose_buffer.extend(lines)
        else:
            for line in lines:
                print(line)


def client_input(prompt=""):
    """Input wrapper that prevents verbose logs from interrupting prompts."""
    global input_active

    with verbose_lock:
        input_active = True

    try:
        return builtins.input(prompt)

    finally:
        with verbose_lock:
            input_active = False

            if verbose_buffer:
                print()

                for line in verbose_buffer:
                    print(line)

                verbose_buffer.clear()

# ---------------------------------------------------------------------------
# Background network threads
# ---------------------------------------------------------------------------
# The heartbeat thread periodically checks server liveness using PING/PONG.
# The listener thread continuously reads framed PDUs, updates authoritative
# client-side state, and signals the main loop when player input is required.
def heartbeat_loop(sock):
    seq_num = 9000 
    global last_pong_time
    
    while True:
        time.sleep(30)
        try:
            ping_send_time = time.time()
            pdu = {
                "type": "PING",
                "seq_num": seq_num,
                "timestamp": int(ping_send_time * 1000)
            }
            json_data = json.dumps(pdu).encode("utf-8")

            buffered_verbose_print(
                f"\n[VERBOSE] SENT to Server | {len(json_data)} bytes",
                f"[VERBOSE] RAW: {json_data.decode('utf-8')}"
            )

            send_pdu(sock, pdu, False, "to Server")
            
            time.sleep(10)
            
            if last_pong_time < ping_send_time:
                print(
                    "\n[CLIENT] FATAL ERROR: Server heartbeat timeout. "
                    "No PONG received within 10 seconds."
                )

                connection_lost_received.set()

                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

                sock.close()
                return
                
            seq_num += 1
            
        except Exception:
            break 

def listen_for_messages(sock, ui=None):
    global last_pong_time, latest_server_seq
    global latest_mulligan_hand, latest_mulligan_count
    global latest_priority_seq, latest_phase
    global latest_hand, latest_active_player
    global latest_battlefield, latest_life_totals
    global latest_game_state
    global latest_phase_seq
    global latest_attackers
    global latest_combat_blockers
    global latest_cleanup_seq
    try:
        while True:
            length_prefix = receive_exact(sock, 4)
            if not length_prefix:
                print("\n[CLIENT] Disconnected from server.")
                connection_lost_received.set()
                break
            
            message_length = struct.unpack('>I', length_prefix)[0]

            if message_length > 65535:
                print(
                    "\n[CLIENT] FATAL ERROR: "
                    "Incoming PDU exceeds maximum size of 65,535 bytes."
                )
                break

            payload_bytes = receive_exact(sock, message_length)
            
            if payload_bytes:
                payload_str = payload_bytes.decode('utf-8')
                
                buffered_verbose_print(
                    f"\n[VERBOSE] RECV from Server | {message_length} bytes",
                    f"[VERBOSE] RAW: {payload_str}"
                )
                    
                pdu = json.loads(payload_str)
                
                pdu_type = pdu.get("type")

                if pdu_type == "PONG":
                    last_pong_time = time.time()

                elif pdu_type == "GAME_STATE_UPDATE":
                    state = pdu.get("state", {})
                    latest_game_state = state
                    phase = state.get("phase")

                    latest_phase = phase or latest_phase
                    latest_active_player = state.get(
                        "active_player",
                        latest_active_player
                    )

                    if phase not in {"LOBBY", "MULLIGAN", "GAME_SETUP"}:
                        reconnect_state_received.set()

                    if ui:
                        latest_hand = list(
                            state.get("hand", {}).get(ui.player_id, [])
                        )

                    if (
                        ui
                        and phase == "CLEANUP"
                        and latest_active_player == ui.player_id
                        and len(latest_hand) > 7
                    ):
                        latest_cleanup_seq = pdu.get("seq_num", 0)
                        discard_request_received.set()

                    previous_battlefield = latest_battlefield

                    latest_battlefield = state.get(
                        "battlefield",
                        latest_battlefield
                    )

                    if phase == "DECLARE_ATTACKERS" and latest_active_player:
                        previous_by_id = {
                            permanent.get("id"): permanent
                            for permanent in previous_battlefield.get(
                                latest_active_player,
                                []
                            )
                        }

                        latest_attackers = []

                        for permanent in latest_battlefield.get(
                            latest_active_player,
                            []
                        ):
                            card_id = permanent.get("id")
                            card_info = ui.catalog.get(card_id, {}) if ui else {}

                            is_creature = (
                                str(card_info.get("type", "")).lower()
                                == "creature"
                            )

                            was_tapped = previous_by_id.get(
                                card_id,
                                {}
                            ).get("tapped", False)

                            if (
                                is_creature
                                and permanent.get("tapped", False)
                                and not was_tapped
                            ):
                                latest_attackers.append(card_id)

                    latest_life_totals = state.get(
                        "life_totals",
                        latest_life_totals
                    )

                    if "combat_blockers" in state:
                        latest_combat_blockers = state["combat_blockers"]

                    if phase == "LOBBY":
                        players_ready = state.get("players_ready", 0)
                        waiting_for = state.get("waiting_for", [])

                        print(f"\n[CLIENT] Lobby: {players_ready}/2 players ready.")

                        if waiting_for:
                            print(f"[CLIENT] Waiting for: {', '.join(waiting_for)}")
                    else:
                        if ui:
                            ui.render(pdu)
                        else:
                            print(f"\n[CLIENT] Game state updated. Current phase: {phase}")

                        if phase == "MULLIGAN":
                            latest_server_seq = pdu.get("seq_num", 0)
                            latest_mulligan_count = state.get("mulligan_count", 0)

                            if ui:
                                latest_mulligan_hand = list(
                                    state.get("hand", {}).get(ui.player_id, [])
                                )

                            mulligan_state_received.set()

                elif pdu_type == "PHASE_TRANSITION":
                    latest_phase = pdu.get("to_phase", latest_phase)
                    latest_phase_seq = pdu.get("seq_num", 0)
                    latest_active_player = pdu.get(
                        "active_player",
                        latest_active_player
                    )

                    print(
                        f"\n[CLIENT] Phase changed: "
                        f"{pdu.get('from_phase')} -> {latest_phase}"
                    )

                    if (
                        ui
                        and latest_phase == "DECLARE_ATTACKERS"
                        and latest_active_player == ui.player_id
                    ):
                        attackers_request_received.set()

                    if (
                        ui
                        and latest_phase == "DECLARE_BLOCKERS"
                        and latest_active_player != ui.player_id
                    ):
                        blockers_request_received.set()

                    if (
                        ui
                        and latest_phase == "ASSIGN_DAMAGE_ORDER"
                        and latest_active_player == ui.player_id
                    ):
                        damage_order_request_received.set()

                elif pdu_type == "PRIORITY_GRANT":
                    latest_priority_seq = pdu.get("seq_num", 0)

                    if ui and pdu.get("player_id") == ui.player_id:
                        print("\n[CLIENT] You have priority.")
                        priority_grant_received.set()

                elif pdu_type == "STACK_PUSH":
                    if latest_game_state:
                        stack = latest_game_state.setdefault("stack", [])

                        stack.append({
                            "stack_item_id": pdu.get("stack_item_id"),
                            "item_type": pdu.get("item_type"),
                            "source": pdu.get("source"),
                            "targets": pdu.get("targets", []),
                            "controller": pdu.get("controller")
                        })

                        if ui:
                            ui.render({
                                "state": latest_game_state
                            })

                elif pdu_type == "STACK_RESOLVE":
                    if latest_game_state:
                        resolved_id = pdu.get("stack_item_id")

                        latest_game_state["stack"] = [
                            item
                            for item in latest_game_state.get("stack", [])
                            if item.get("stack_item_id") != resolved_id
                        ]

                        if ui:
                            ui.render({
                                "state": latest_game_state
                            })

                elif pdu_type == "GAME_OVER":
                    winner_id = pdu.get("winner_id")
                    loser_id = pdu.get("loser_id")
                    reason = pdu.get("reason")

                    print("\n" + "=" * 45)
                    print("[CLIENT] GAME OVER")
                    print(f"Winner: {winner_id}")
                    print(f"Loser: {loser_id}")
                    print(f"Reason: {reason}")

                    if ui:
                        if winner_id == ui.player_id:
                            print("Result: YOU WIN")
                        elif loser_id == ui.player_id:
                            print("Result: YOU LOSE")

                    print("=" * 45)

                    game_over_received.set()

                elif pdu_type == "ERROR":
                    error_message = (
                        f"{pdu.get('code')}: "
                        f"{pdu.get('message')}"
                    )

                    if ui:
                        ui.set_status(
                            f"ERROR - {error_message}"
                        )

                    print(
                        f"\n[CLIENT ERROR] "
                        f"{error_message}"
                    )

                else:
                    print(f"\n[CLIENT] Received {pdu_type} PDU")
                
    except (ConnectionResetError, json.JSONDecodeError, struct.error, OSError):
        connection_lost_received.set()

        if not game_over_received.is_set():
            print("\n[CLIENT] Connection closed or network error occurred.")

        return

def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="MTGNP Client")
    parser.add_argument('-v', '--verbose', action='store_true', help="Enable verbose logging")
    args = parser.parse_args()
    VERBOSE = args.verbose

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_sock.connect((HOST, PORT))
        print(f"[CLIENT] Connected to Game Server at {HOST}:{PORT}")
        if VERBOSE:
            print("[CLIENT] Verbose mode is ON.")
    except ConnectionRefusedError:
        print("[CLIENT] Connection refused. Make sure the server is running.")
        return

    global last_pong_time
    last_pong_time = time.time()

    heartbeat_thread = threading.Thread(target=heartbeat_loop, args=(client_sock,), daemon=True)
    heartbeat_thread.start()

    listener_thread = threading.Thread(target=listen_for_messages, args=(client_sock,), daemon=True)
    listener_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[CLIENT] Closing connection.")
        client_sock.close()

# ---------------------------------------------------------------------------
# Interactive input helpers
# ---------------------------------------------------------------------------
# These functions collect and validate user choices for mulligans, land plays,
# spell targets, combat declarations, damage order, and cleanup discards.
# They only build player requests; the server remains responsible for enforcing
# the actual game rules and accepting or rejecting each action.
def prompt_cards_to_bottom(hand, count):
    """Ask the player which cards to place at the bottom after mulligans."""
    if count == 0:
        return []

    while True:
        raw_indexes = client_input(
            f"Choose {count} card index(es) to put on the bottom "
            "(separated by spaces): "
        ).strip()

        try:
            indexes = [int(value) for value in raw_indexes.split()]
        except ValueError:
            print("Please enter card indexes using numbers only.")
            continue

        if len(indexes) != count:
            print(f"You must choose exactly {count} card(s).")
            continue

        if len(set(indexes)) != len(indexes):
            print("Do not choose the same index more than once.")
            continue

        if any(index < 0 or index >= len(hand) for index in indexes):
            print("One or more card indexes are invalid.")
            continue

        return [hand[index] for index in indexes]

def prompt_land_to_play(hand, ui):
    """Ask the player which land card to play."""
    land_options = []

    for index, card_id in enumerate(hand):
        card_info = ui.catalog.get(card_id, {})

        if str(card_info.get("type", "")).lower() == "land":
            land_options.append((index, card_id))

    if not land_options:
        print("[CLIENT] You have no land cards in your hand.")
        return None

    print("\nLand cards in your hand:")

    for index, card_id in land_options:
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    while True:
        choice = client_input(
            "Choose the hand index of the land to play "
            "(or type 'cancel'): "
        ).strip().lower()

        if choice == "cancel":
            return None

        try:
            index = int(choice)
        except ValueError:
            print("Please enter a valid card index.")
            continue

        for land_index, card_id in land_options:
            if index == land_index:
                return card_id

        print("That index is not a land card.")

def prompt_creature_to_cast(hand, ui):
    """Ask the player which creature spell to cast."""
    creature_options = []

    for index, card_id in enumerate(hand):
        card_info = ui.catalog.get(card_id, {})

        if str(card_info.get("type", "")).lower() == "creature":
            creature_options.append((index, card_id))

    if not creature_options:
        print("[CLIENT] You have no creature cards in your hand.")
        return None

    print("\nCreature cards in your hand:")

    for index, card_id in creature_options:
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    while True:
        choice = client_input(
            "Choose the hand index of the creature to cast "
            "(or type 'cancel'): "
        ).strip().lower()

        if choice == "cancel":
            return None

        try:
            index = int(choice)
        except ValueError:
            print("Please enter a valid card index.")
            continue

        for creature_index, card_id in creature_options:
            if index == creature_index:
                return card_id

        print("That index is not a creature card.")

def prompt_spell_target(ui, creature_only=False):
    """Ask the player to select a legal target."""
    options = []

    if not creature_only:
        for player_id in latest_life_totals:
            options.append(
                (player_id, f"Player: {player_id}")
            )

    for player_id, permanents in latest_battlefield.items():
        for permanent in permanents:
            card_id = permanent.get("id")

            card_info = ui.catalog.get(card_id, {})

            if str(card_info.get("type", "")).lower() == "creature":
                options.append(
                    (
                        card_id,
                        f"{ui.get_card_name(card_id)} "
                        f"({player_id})"
                    )
                )

    if not options:
        print("[CLIENT] No valid targets are available.")
        return None

    print("\nAvailable targets:")

    for index, (_, description) in enumerate(options):
        print(f"  [{index}] {description}")

    while True:
        choice = client_input(
            "Choose target index (or type 'cancel'): "
        ).strip().lower()

        if choice == "cancel":
            return None

        try:
            index = int(choice)
        except ValueError:
            print("Please enter a valid target index.")
            continue

        if 0 <= index < len(options):
            return options[index][0]

        print("That target index is invalid.")

def prompt_attackers(player_id, ui):
    """Ask the active player which legal creatures should attack."""
    legal_attackers = []

    for permanent in latest_battlefield.get(player_id, []):
        card_id = permanent.get("id")
        card_info = ui.catalog.get(card_id, {})

        is_creature = (
            str(card_info.get("type", "")).lower()
            == "creature"
        )

        if (
            is_creature
            and not permanent.get("tapped", False)
            and not permanent.get("summoning_sick", False)
        ):
            legal_attackers.append(card_id)

    if not legal_attackers:
        print("[CLIENT] You have no legal attackers.")
        return []

    print("\nLegal attackers:")

    for index, card_id in enumerate(legal_attackers):
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    while True:
        choice = client_input(
            "Choose attacker index(es), separated by spaces "
            "(or press Enter for no attack): "
        ).strip()

        if not choice:
            return []

        try:
            indexes = [int(value) for value in choice.split()]
        except ValueError:
            print("Please enter valid attacker indexes.")
            continue

        if len(set(indexes)) != len(indexes):
            print("Do not choose the same attacker twice.")
            continue

        if any(
            index < 0 or index >= len(legal_attackers)
            for index in indexes
        ):
            print("One or more attacker indexes are invalid.")
            continue

        opponent_id = next(
            (
                pid
                for pid in latest_life_totals
                if pid != player_id
            ),
            None
        )

        return [
            {
                "creature_id": legal_attackers[index],
                "target": opponent_id
            }
            for index in indexes
        ]

def prompt_blockers(player_id, ui):
    """Ask the defending player to assign blockers."""
    legal_blockers = []

    for permanent in latest_battlefield.get(player_id, []):
        card_id = permanent.get("id")
        card_info = ui.catalog.get(card_id, {})

        is_creature = (
            str(card_info.get("type", "")).lower()
            == "creature"
        )

        if is_creature and not permanent.get("tapped", False):
            legal_blockers.append(card_id)

    if not latest_attackers:
        print("[CLIENT] No attackers to block.")
        return []

    if not legal_blockers:
        print("[CLIENT] You have no legal blockers.")
        return []

    print("\nAttacking creatures:")

    for index, card_id in enumerate(latest_attackers):
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    print("\nYour legal blockers:")

    for index, card_id in enumerate(legal_blockers):
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    assignments = []
    used_blockers = set()

    while True:
        choice = client_input(
            "Choose blocker index "
            "(or press Enter when finished): "
        ).strip()

        if not choice:
            return assignments

        try:
            blocker_index = int(choice)
        except ValueError:
            print("Please enter a valid blocker index.")
            continue

        if (
            blocker_index < 0
            or blocker_index >= len(legal_blockers)
        ):
            print("That blocker index is invalid.")
            continue

        if blocker_index in used_blockers:
            print("That creature is already blocking.")
            continue

        target_choice = client_input(
            "Choose attacker index to block: "
        ).strip()

        try:
            attacker_index = int(target_choice)
        except ValueError:
            print("Please enter a valid attacker index.")
            continue

        if (
            attacker_index < 0
            or attacker_index >= len(latest_attackers)
        ):
            print("That attacker index is invalid.")
            continue

        assignments.append({
            "creature_id": legal_blockers[blocker_index],
            "blocking_id": latest_attackers[attacker_index]
        })

        used_blockers.add(blocker_index)

        print("[CLIENT] Block assignment added.")

def prompt_damage_orders(ui):
    """Ask the active player to order blockers for multi-blocked attackers."""
    assignments = []

    for attacker_id, blocker_ids in latest_combat_blockers.items():
        if len(blocker_ids) < 2:
            continue

        print(
            f"\nDamage order for "
            f"{ui.get_card_name(attacker_id)} "
            f"({attacker_id}):"
        )

        for index, blocker_id in enumerate(blocker_ids):
            print(
                f"  [{index}] {ui.get_card_name(blocker_id)} "
                f"({blocker_id})"
            )

        while True:
            choice = client_input(
                "Enter blocker indexes in damage order "
                "(example: 1 0): "
            ).strip()

            try:
                indexes = [
                    int(value)
                    for value in choice.split()
                ]
            except ValueError:
                print("Please enter blocker indexes using numbers only.")
                continue

            expected_indexes = set(range(len(blocker_ids)))

            if (
                len(indexes) != len(blocker_ids)
                or set(indexes) != expected_indexes
            ):
                print(
                    "Enter every blocker index exactly once."
                )
                continue

            assignments.append({
                "attacker_id": attacker_id,
                "blocker_order": [
                    blocker_ids[index]
                    for index in indexes
                ]
            })

            break

    return assignments

def prompt_cleanup_discard(hand, ui):
    """Ask the active player which excess cards to discard."""
    discard_count = len(hand) - 7

    if discard_count <= 0:
        return []

    print(
        f"\n[CLIENT] Cleanup: you must discard "
        f"{discard_count} card(s)."
    )

    print("\nYour hand:")

    for index, card_id in enumerate(hand):
        print(
            f"  [{index}] {ui.get_card_name(card_id)} "
            f"({card_id})"
        )

    while True:
        choice = client_input(
            f"Choose exactly {discard_count} card index(es) "
            "to discard, separated by spaces: "
        ).strip()

        try:
            indexes = [
                int(value)
                for value in choice.split()
            ]
        except ValueError:
            print("Please enter card indexes using numbers only.")
            continue

        if len(indexes) != discard_count:
            print(
                f"Choose exactly {discard_count} card(s)."
            )
            continue

        if len(set(indexes)) != len(indexes):
            print("Do not choose the same card twice.")
            continue

        if any(
            index < 0 or index >= len(hand)
            for index in indexes
        ):
            print("One or more card indexes are invalid.")
            continue

        return [hand[index] for index in indexes]

def run_lobby_and_mulligan(client_sock, player_id, deck_list):
    """Send PLAYER_READY and complete the mulligan phase."""

    mulligan_state_received.clear()
    reconnect_state_received.clear()

    ready_pdu = {
        "type": "PLAYER_READY",
        "seq_num": 1,
        "player_id": player_id,
        "deck_list": deck_list
    }

    send_pdu(
        client_sock,
        ready_pdu,
        VERBOSE,
        "PLAYER_READY to Server"
    )

    print(f"[CLIENT] Sent PLAYER_READY for {player_id}.")
    print("[CLIENT] Waiting for opening hand...")

    while True:
        while (
            not mulligan_state_received.is_set()
            and not reconnect_state_received.is_set()
            and not connection_lost_received.is_set()
        ):
            time.sleep(0.05)

        if connection_lost_received.is_set():
            return False

        if reconnect_state_received.is_set():
            reconnect_state_received.clear()

            print(
                "[CLIENT] Existing game state restored. "
                "Reconnection successful."
            )

            if (
                latest_phase == "DECLARE_ATTACKERS"
                and latest_active_player == player_id
            ):
                attackers_request_received.set()

            elif (
                latest_phase == "DECLARE_BLOCKERS"
                and latest_active_player != player_id
            ):
                blockers_request_received.set()

            elif (
                latest_phase == "ASSIGN_DAMAGE_ORDER"
                and latest_active_player == player_id
            ):
                damage_order_request_received.set()

            return

        mulligan_state_received.clear()

        while True:
            choice = client_input(
                "\nKeep or mulligan? [K/M]: "
            ).strip().upper()

            if choice in {"K", "M"}:
                break

            print("Please enter K to keep or M to mulligan.")

        if choice == "M":
            mulligan_pdu = {
                "type": "MULLIGAN_CHOICE",
                "seq_num": latest_server_seq,
                "keep": False,
                "cards_to_bottom": []
            }

            send_pdu(
                client_sock,
                mulligan_pdu,
                VERBOSE,
                "MULLIGAN_CHOICE to Server"
            )

            print("[CLIENT] Sent MULLIGAN_CHOICE: MULLIGAN.")
            print("[CLIENT] Waiting for a new opening hand...")
            continue

        cards_to_bottom = prompt_cards_to_bottom(
            latest_mulligan_hand,
            latest_mulligan_count
        )

        keep_pdu = {
            "type": "MULLIGAN_CHOICE",
            "seq_num": latest_server_seq,
            "keep": True,
            "cards_to_bottom": cards_to_bottom
        }

        send_pdu(
            client_sock,
            keep_pdu,
            VERBOSE,
            "MULLIGAN_CHOICE to Server"
        )

        print("[CLIENT] Sent MULLIGAN_CHOICE: KEEP.")
        break
    return True

# ---------------------------------------------------------------------------
# Client session and gameplay loop
# ---------------------------------------------------------------------------
# Establishes the TCP session, starts background network threads, completes
# lobby/mulligan handling, and drives player actions based on server events.
# Reconnects restore the latest authoritative state rather than starting a
# new game, while connection-loss events terminate the client cleanly.
def start_client(player_id, deck_list, verbose=False):
    """Connects the configured player and sends PLAYER_READY."""
    global VERBOSE, last_pong_time

    VERBOSE = verbose

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    ui = BattlefieldUI(player_id)

    try:
        client_sock.connect((HOST, PORT))
        print(f"[CLIENT] Connected to Game Server at {HOST}:{PORT}")
    except ConnectionRefusedError:
        print("[CLIENT] Connection refused. Make sure the server is running.")
        return

    last_pong_time = time.time()

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(client_sock,),
        daemon=True
    )
    heartbeat_thread.start()

    listener_thread = threading.Thread(
        target=listen_for_messages,
        args=(client_sock, ui),
        daemon=True
    )
    listener_thread.start()

    lobby_completed = run_lobby_and_mulligan(
        client_sock,
        player_id,
        deck_list
    )

    if not lobby_completed:
        print("[CLIENT] Connection lost. Closing client.")
        return

    try:
        while True:
            while (
                not priority_grant_received.is_set()
                and not attackers_request_received.is_set()
                and not blockers_request_received.is_set()
                and not damage_order_request_received.is_set()
                and not discard_request_received.is_set()
                and not game_over_received.is_set()
                and not connection_lost_received.is_set()
            ):
                time.sleep(0.05)

            if connection_lost_received.is_set():
                print("[CLIENT] Connection lost. Closing client.")
                break

            if game_over_received.is_set():

                priority_grant_received.clear()
                attackers_request_received.clear()
                blockers_request_received.clear()
                discard_request_received.clear()
                mulligan_state_received.clear()
                latest_attackers.clear()

                while True:
                    play_again = client_input(
                        "\nPlay another game? [Y/N]: "
                    ).strip().upper()

                    if play_again in {"Y", "N"}:
                        break

                    print("Please enter Y or N.")

                if play_again == "N":
                    break

                game_over_received.clear()

                print(
                    "[CLIENT] Returning to lobby "
                    "on the same connection..."
                )

                lobby_completed = run_lobby_and_mulligan(
                    client_sock,
                    player_id,
                    deck_list
                )

                if not lobby_completed:
                    print("[CLIENT] Connection lost. Closing client.")
                    break

                continue

            if attackers_request_received.is_set():
                attackers_request_received.clear()

                attackers = prompt_attackers(
                    player_id,
                    ui
                )

                attackers_pdu = {
                    "type": "DECLARE_ATTACKERS",
                    "seq_num": latest_phase_seq,
                    "attackers": attackers
                }

                send_pdu(
                    client_sock,
                    attackers_pdu,
                    VERBOSE,
                    "DECLARE_ATTACKERS to Server"
                )

                ui.set_status(
                    f"Declared {len(attackers)} attacker(s)."
                )

                print(
                    f"[CLIENT] Declared "
                    f"{len(attackers)} attacker(s)."
                )

                continue

            if blockers_request_received.is_set():
                blockers_request_received.clear()

                blockers = prompt_blockers(
                    player_id,
                    ui
                )

                blockers_pdu = {
                    "type": "DECLARE_BLOCKERS",
                    "seq_num": latest_phase_seq,
                    "blockers": blockers
                }

                send_pdu(
                    client_sock,
                    blockers_pdu,
                    VERBOSE,
                    "DECLARE_BLOCKERS to Server"
                )

                ui.set_status(
                    f"Declared {len(attackers)} attacker(s)."
                )

                print(
                    f"[CLIENT] Declared "
                    f"{len(blockers)} blocker(s)."
                )

                continue

            if damage_order_request_received.is_set():
                damage_order_request_received.clear()

                damage_orders = prompt_damage_orders(ui)

                for assignment in damage_orders:
                    damage_order_pdu = {
                        "type": "ASSIGN_DAMAGE_ORDER",
                        "seq_num": latest_phase_seq,
                        "attacker_id": assignment["attacker_id"],
                        "blocker_order": assignment["blocker_order"]
                    }

                    send_pdu(
                        client_sock,
                        damage_order_pdu,
                        VERBOSE,
                        "ASSIGN_DAMAGE_ORDER to Server"
                    )

                    ui.set_status(
                        f"Damage order assigned for "
                        f"{ui.get_card_name(assignment['attacker_id'])}."
                    )

                    print(
                        f"[CLIENT] Damage order assigned for "
                        f"{ui.get_card_name(assignment['attacker_id'])}."
                    )

                continue

            if discard_request_received.is_set():
                discard_request_received.clear()

                card_ids = prompt_cleanup_discard(
                    latest_hand,
                    ui
                )

                discard_pdu = {
                    "type": "DISCARD",
                    "seq_num": latest_cleanup_seq,
                    "card_ids": card_ids
                }

                send_pdu(
                    client_sock,
                    discard_pdu,
                    VERBOSE,
                    "DISCARD to Server"
                )

                ui.set_status(
                    f"Discarded {len(card_ids)} card(s)."
                )

                print(
                    f"[CLIENT] Discarded "
                    f"{len(card_ids)} card(s)."
                )

                continue

            priority_grant_received.clear()

            while True:
                can_play_land = (
                    latest_active_player == player_id
                    and latest_phase in {
                        "PRECOMBAT_MAIN",
                        "POSTCOMBAT_MAIN"
                    }
                )

                has_supported_instant = any(
                    card_id in {
                        "lightning_bolt_001",
                        "shock_001",
                        "giant_growth_001"
                    }
                    for card_id in latest_hand
                )

                ability_sources = [
                    permanent.get("id")
                    for permanent in latest_battlefield.get(player_id, [])
                    if str(permanent.get("id", "")).startswith(
                        "llanowar_elves_"
                    )
                ]

                has_supported_ability = bool(ability_sources)

                actions = ["pass"]

                if can_play_land:
                    actions.extend(["land", "cast"])
                elif has_supported_instant:
                    actions.append("cast")

                if has_supported_ability:
                    actions.append("ability")

                actions.append("concede")

                action = client_input(
                    f"\nChoose action [{'/' .join(actions)}]: "
                ).strip().lower()

                if action == "concede":
                    concede_pdu = {
                        "type": "CONCEDE",
                        "seq_num": latest_priority_seq,
                        "player_id": player_id
                    }

                    send_pdu(
                        client_sock,
                        concede_pdu,
                        VERBOSE,
                        "CONCEDE to Server"
                    )

                    print("[CLIENT] Concession sent. Waiting for GAME_OVER...")

                    game_over_received.wait()
                    break

                if action == "pass":
                    pass_pdu = {
                        "type": "PRIORITY_PASS",
                        "seq_num": latest_priority_seq
                    }

                    send_pdu(
                        client_sock,
                        pass_pdu,
                        VERBOSE,
                        "PRIORITY_PASS to Server"
                    )

                    print("[CLIENT] Priority passed.")
                    break

                if action == "ability" and has_supported_ability:
                    source_id = ability_sources[0]

                    ability_pdu = {
                        "type": "ACTIVATE_ABILITY",
                        "seq_num": latest_priority_seq,
                        "source_id": source_id,
                        "ability_index": 0,
                        "targets": [],
                        "cost_payment": {
                            "tap": True,
                            "mana": {}
                        }
                    }

                    send_pdu(
                        client_sock,
                        ability_pdu,
                        VERBOSE,
                        "ACTIVATE_ABILITY to Server"
                    )

                    ui.set_status(
                        f"Requested ability of "
                        f"{ui.get_card_name(source_id)}."
                    )

                    print(
                        f"[CLIENT] Requested ability of "
                        f"{ui.get_card_name(source_id)}."
                    )

                    break

                if action == "land" and can_play_land:
                    card_id = prompt_land_to_play(
                        latest_hand,
                        ui
                    )

                    if card_id is None:
                        continue

                    land_pdu = {
                        "type": "PLAY_LAND",
                        "seq_num": latest_priority_seq,
                        "card_id": card_id
                    }

                    send_pdu(
                        client_sock,
                        land_pdu,
                        VERBOSE,
                        "PLAY_LAND to Server"
                    )

                    ui.set_status(
                        f"Requested to play {ui.get_card_name(card_id)}."
                    )

                    print(
                        f"[CLIENT] Requested to play "
                        f"{ui.get_card_name(card_id)}."
                    )
                    break

                if action == "cast" and (
                    can_play_land
                    or has_supported_instant
                ):
                    cast_options = []

                    for index, possible_card_id in enumerate(latest_hand):
                        card_info = ui.catalog.get(possible_card_id, {})
                        card_type = str(card_info.get("type", "")).lower()

                        if can_play_land:
                            if (
                                card_type == "creature"
                                or possible_card_id in {
                                    "lightning_bolt_001",
                                    "shock_001",
                                    "giant_growth_001"
                                }
                            ):
                                cast_options.append(
                                    (index, possible_card_id)
                                )

                        else:
                            if possible_card_id in {
                                "lightning_bolt_001",
                                "shock_001",
                                "giant_growth_001"
                            }:
                                cast_options.append(
                                    (index, possible_card_id)
                                )

                    if not cast_options:
                        print("[CLIENT] You have no supported spells to cast.")
                        continue

                    print("\nSpells in your hand:")

                    for index, possible_card_id in cast_options:
                        print(
                            f"  [{index}] {ui.get_card_name(possible_card_id)} "
                            f"({possible_card_id})"
                        )

                    while True:
                        choice = client_input(
                            "Choose the hand index of the spell to cast "
                            "(or type 'cancel'): "
                        ).strip().lower()

                        if choice == "cancel":
                            card_id = None
                            break

                        try:
                            selected_index = int(choice)
                        except ValueError:
                            print("Please enter a valid card index.")
                            continue

                        card_id = next(
                            (
                                possible_card_id
                                for index, possible_card_id in cast_options
                                if index == selected_index
                            ),
                            None
                        )

                        if card_id:
                            break

                        print("That index is not a supported spell.")

                    if card_id is None:
                        continue

                    card_info = ui.catalog.get(card_id, {})
                    mana_payment = dict(
                        card_info.get("mana_cost", {})
                    )

                    targets = []

                    if card_id in {
                        "lightning_bolt_001",
                        "shock_001"
                    }:
                        target = prompt_spell_target(ui)

                        if target is None:
                            continue

                        targets = [target]

                    elif card_id == "giant_growth_001":
                        target = prompt_spell_target(
                            ui,
                            creature_only=True
                        )

                        if target is None:
                            continue

                        targets = [target]

                    cast_pdu = {
                        "type": "CAST_SPELL",
                        "seq_num": latest_priority_seq,
                        "card_id": card_id,
                        "targets": targets,
                        "mana_payment": mana_payment
                    }

                    send_pdu(
                        client_sock,
                        cast_pdu,
                        VERBOSE,
                        "CAST_SPELL to Server"
                    )

                    ui.set_status(
                        f"Requested to cast {ui.get_card_name(card_id)}."
                    )

                    print(
                        f"[CLIENT] Requested to cast "
                        f"{ui.get_card_name(card_id)}."
                    )
                    break

                if can_play_land:
                    print("Available actions: pass, land, cast")
                else:
                    print("Available action: pass")
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[CLIENT] Closing connection.")

        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        client_sock.close()

        if listener_thread.is_alive():
            listener_thread.join(timeout=1)

if __name__ == "__main__":
    main()