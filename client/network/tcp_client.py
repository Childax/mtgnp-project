import socket
import json
import threading
import time
import argparse
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from shared.network_utils import receive_exact, send_pdu
from client.ui.battlefield import BattlefieldUI

HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False
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
            send_pdu(sock, pdu, VERBOSE, "to Server")
            
            time.sleep(10)
            
            if last_pong_time < ping_send_time:
                print("\n[CLIENT] FATAL ERROR: Server heartbeat timeout. No PONG received within 10 seconds.")
                sock.close() 
                sys.exit(1)
                
            seq_num += 1
            
        except Exception:
            break 

def listen_for_messages(sock, ui=None):
    global last_pong_time, latest_server_seq
    global latest_mulligan_hand, latest_mulligan_count
    global latest_priority_seq, latest_phase
    global latest_hand, latest_active_player
    global latest_battlefield, latest_life_totals
    try:
        while True:
            length_prefix = receive_exact(sock, 4)
            if not length_prefix:
                print("\n[CLIENT] Disconnected from server.")
                break
            
            import struct
            message_length = struct.unpack('>I', length_prefix)[0]
            payload_bytes = receive_exact(sock, message_length)
            
            if payload_bytes:
                payload_str = payload_bytes.decode('utf-8')
                
                if VERBOSE:
                    print(f"\n[VERBOSE] RECV from Server | {message_length} bytes")
                    print(f"[VERBOSE] RAW: {payload_str}")
                    
                pdu = json.loads(payload_str)
                
                pdu_type = pdu.get("type")

                if pdu_type == "PONG":
                    last_pong_time = time.time()

                elif pdu_type == "GAME_STATE_UPDATE":
                    state = pdu.get("state", {})
                    phase = state.get("phase")

                    latest_phase = phase or latest_phase
                    latest_active_player = state.get(
                        "active_player",
                        latest_active_player
                    )

                    if ui:
                        latest_hand = list(
                            state.get("hand", {}).get(ui.player_id, [])
                        )

                    latest_battlefield = state.get(
                        "battlefield",
                        latest_battlefield
                    )

                    latest_life_totals = state.get(
                        "life_totals",
                        latest_life_totals
                    )

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
                    latest_active_player = pdu.get(
                        "active_player",
                        latest_active_player
                    )

                    print(
                        f"\n[CLIENT] Phase changed: "
                        f"{pdu.get('from_phase')} -> {latest_phase}"
                    )

                elif pdu_type == "PRIORITY_GRANT":
                    latest_priority_seq = pdu.get("seq_num", 0)

                    if ui and pdu.get("player_id") == ui.player_id:
                        print("\n[CLIENT] You have priority.")
                        priority_grant_received.set()

                elif pdu_type == "ERROR":
                    print(f"\n[CLIENT ERROR] {pdu.get('code')}: {pdu.get('message')}")

                else:
                    print(f"\n[CLIENT] Received {pdu_type} PDU")
                
    except (ConnectionResetError, json.JSONDecodeError, struct.error, OSError):
        print("\n[CLIENT] Connection closed or network error occurred.")
        sys.exit(1)

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

def prompt_cards_to_bottom(hand, count):
    """Ask the player which cards to place at the bottom after mulligans."""
    if count == 0:
        return []

    while True:
        raw_indexes = input(
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
        choice = input(
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
        choice = input(
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
        choice = input(
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
        mulligan_state_received.wait()
        mulligan_state_received.clear()

        while True:
            choice = input("\nKeep or mulligan? [K/M]: ").strip().upper()

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

    try:
        while True:
            priority_grant_received.wait()
            priority_grant_received.clear()

            while True:
                can_play_land = (
                    latest_active_player == player_id
                    and latest_phase in {
                        "PRECOMBAT_MAIN",
                        "POSTCOMBAT_MAIN"
                    }
                )

                if can_play_land:
                    action = input(
                        "\nChoose action [pass/land/cast]: "
                    ).strip().lower()
                else:
                    action = input(
                        "\nType 'pass' to pass priority: "
                    ).strip().lower()

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

                    print(
                        f"[CLIENT] Requested to play "
                        f"{ui.get_card_name(card_id)}."
                    )
                    break

                if action == "cast" and can_play_land:
                    cast_options = []

                    for index, possible_card_id in enumerate(latest_hand):
                        card_info = ui.catalog.get(possible_card_id, {})
                        card_type = str(card_info.get("type", "")).lower()

                        if card_type == "creature" or possible_card_id in {
                            "lightning_bolt_001",
                            "shock_001"
                        }:
                            cast_options.append((index, possible_card_id))

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
                        choice = input(
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
        client_sock.close()

if __name__ == "__main__":
    main()