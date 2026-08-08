import socket
import json
import threading
import argparse
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.network_utils import receive_exact, send_pdu
from pydantic import ValidationError
from shared.pdu import parse_pdu, build_phase_transition, build_game_over
from server.core.lifecycle import LobbyManager
from server.core.game_state import GameState
from server.network.router import route_gameplay_pdu


HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False

sessions = {}
lobby = LobbyManager()
RECONNECT_TIMEOUT = 60.0

# Member 3 initialize this
current_game_state = None
turn_manager = None
stack_manager = None
priority_manager = None

def trigger_forfeit(player_id):
    """Called when a player fails to reconnect within the time limit."""
    global current_game_state, turn_manager, stack_manager, priority_manager
    if sessions.get(player_id) and not sessions[player_id]["connected"]:
        print(f"\n[SERVER] Player {player_id} failed to reconnect in time. FORFEIT.")
        
        remaining_player = 1 if player_id == 2 else 2
        
        if sessions.get(remaining_player) and sessions[remaining_player]["connected"]:
            seq = current_game_state.next_seq() if current_game_state else 999
            
            loser_logical_id = lobby.ready_players.get(player_id, {}).get("player_id", str(player_id))
            winner_logical_id = lobby.ready_players.get(remaining_player, {}).get("player_id", str(remaining_player))
            
            game_over_pdu = build_game_over(
                seq_num=seq, 
                winner_id=winner_logical_id, 
                loser_id=loser_logical_id, 
                reason="DISCONNECT"
            )
            send_pdu(
                sessions[remaining_player]["conn"], 
                game_over_pdu, 
                VERBOSE, 
                f"GAME_OVER to Player {remaining_player}"
            )
            
        current_game_state = None
        turn_manager = None
        stack_manager = None
        priority_manager = None
        lobby.ready_players.clear()
        print("[SERVER] Game over. Returned to LOBBY state.")

def handle_client(conn, addr, player_id):
    global current_game_state, turn_manager, stack_manager, priority_manager
    print(f"[SERVER] Player {player_id} connected from {addr}")
    
    if player_id in sessions and sessions[player_id].get("timer"):
        sessions[player_id]["timer"].cancel()
        print(f"[SERVER] Reconnect timer for Player {player_id} cancelled.")
        
    sessions[player_id] = {"conn": conn, "connected": True, "timer": None}

    try:
        while True:
            length_prefix = receive_exact(conn, 4)
            if not length_prefix:
                break
            
            import struct
            message_length = struct.unpack('>I', length_prefix)[0]
            
            if message_length > 65535:
                print(f"[SERVER] Error: Message exceeds max PDU size.")
                break

            payload_bytes = receive_exact(conn, message_length)
            if not payload_bytes:
                break
            
            payload_str = payload_bytes.decode('utf-8')
            
            if VERBOSE:
                print(f"\n[VERBOSE] RECV from Player {player_id} | {message_length} bytes")
                print(f"[VERBOSE] RAW: {payload_str}")
            
            try:
                raw_dict = json.loads(payload_str)
                pdu = parse_pdu(raw_dict)
                
                if pdu.type == "PING":
                    response = {
                        "type": "PONG", 
                        "seq_num": pdu.seq_num, 
                        "timestamp": pdu.timestamp
                    }
                    send_pdu(conn, response, VERBOSE, f"to Player {player_id}")
                    continue

                if pdu.type == "PLAYER_READY":
                    success, status, data = lobby.process_player_ready(player_id, pdu)
                    
                    if not success:
                        error_msg = {
                            "type": "ERROR",
                            "seq_num": pdu.seq_num,
                            "code": status,
                            "message": data,
                            "rejected_action": raw_dict
                        }
                        send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")
                    else:
                        update_msg = {
                            "type": "GAME_STATE_UPDATE",
                            "seq_num": 2, 
                            "state": data
                        }
                        if status == "GAME_SETUP":
                            print("\n[SERVER] AUTOMATA TRANSITION: LOBBY -> GAME_SETUP")

                            player_decks = {
                                ready_data["player_id"]: ready_data["deck_list"]
                                for ready_data in lobby.ready_players.values()
                            }

                            current_game_state = GameState.initialize_from_decks(player_decks)

                            print(
                                f"[SERVER] Game initialized. "
                                f"First player: {current_game_state.first_player_id}"
                            )

                            for session_player_id, session in sessions.items():
                                if not session["connected"]:
                                    continue

                                viewer_id = lobby.ready_players[session_player_id]["player_id"]
                                personalized_state = current_game_state.to_personalized_dict(viewer_id)

                                personalized_update = {
                                    "type": "GAME_STATE_UPDATE",
                                    "seq_num": current_game_state.next_seq(),
                                    "state": personalized_state
                                }

                                send_pdu(
                                    session["conn"],
                                    personalized_update,
                                    VERBOSE,
                                    f"INITIAL STATE to Player {session_player_id}"
                                )
                        else:
                            send_pdu(
                                conn,
                                update_msg,
                                VERBOSE,
                                f"LOBBY STATE to Player {player_id}"
                            )

                elif pdu.type == "MULLIGAN_CHOICE":
                    if current_game_state is None:
                        error_msg = {
                            "type": "ERROR",
                            "seq_num": pdu.seq_num,
                            "code": "ILLEGAL_ACTION",
                            "message": "No game is currently in the MULLIGAN phase."
                        }
                        send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")
                        continue

                    game_player_id = lobby.ready_players[player_id]["player_id"]

                    if not pdu.keep:
                        current_game_state.mulligan_redraw(game_player_id)
                        redraw_state = current_game_state.to_personalized_dict(game_player_id)

                        redraw_update = {
                            "type": "GAME_STATE_UPDATE",
                            "seq_num": current_game_state.next_seq(),
                            "state": redraw_state
                        }

                        send_pdu(
                            conn,
                            redraw_update,
                            VERBOSE,
                            f"MULLIGAN REDRAW to Player {player_id}"
                        )
                        continue

                    error = current_game_state.mulligan_keep(game_player_id, pdu.cards_to_bottom)

                    if error:
                        error_msg = {
                            "type": "ERROR",
                            "seq_num": pdu.seq_num,
                            "code": "ILLEGAL_ACTION",
                            "message": error
                        }
                        send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")
                        continue

                    print(f"[SERVER] {game_player_id} kept their opening hand.")

                    both_players_kept = all(
                        player.has_kept_hand
                        for player in current_game_state.players.values()
                    )

                    if both_players_kept:
                        current_game_state.turn = 1
                        current_game_state.phase = "UNTAP"
                        current_game_state.priority_holder = None

                        transition_pdu = build_phase_transition(
                            seq_num=current_game_state.next_seq(),
                            from_phase="MULLIGAN",
                            to_phase="UNTAP",
                            active_player=current_game_state.active_player,
                            turn=current_game_state.turn
                        )

                        print("[SERVER] AUTOMATA TRANSITION: MULLIGAN -> IN_GAME / UNTAP")

                        for session_player_id, session in sessions.items():
                            if not session["connected"]:
                                continue

                            send_pdu(
                                session["conn"],
                                transition_pdu,
                                VERBOSE,
                                f"PHASE_TRANSITION to Player {session_player_id}"
                            )

                            viewer_id = lobby.ready_players[session_player_id]["player_id"]
                            personalized_state = current_game_state.to_personalized_dict(viewer_id)

                            state_update = {
                                "type": "GAME_STATE_UPDATE",
                                "seq_num": current_game_state.next_seq(),
                                "state": personalized_state
                            }

                            send_pdu(
                                session["conn"],
                                state_update,
                                VERBOSE,
                                f"UPDATED STATE to Player {session_player_id}"
                            )

                elif pdu.type == "CONCEDE":
                    game_player_id = lobby.ready_players.get(player_id, {}).get("player_id", str(player_id))
                    print(f"\n[SERVER] {game_player_id} conceded.")
                    
                    remaining_player = 1 if player_id == 2 else 2
                    winner_logical_id = lobby.ready_players.get(remaining_player, {}).get("player_id", str(remaining_player))
                    
                    seq = current_game_state.next_seq() if current_game_state else 999
                    game_over_pdu = build_game_over(
                        seq_num=seq, 
                        winner_id=winner_logical_id, 
                        loser_id=game_player_id, 
                        reason="CONCEDE"
                    )
                    
                    for session_player_id, session in sessions.items():
                        if session["connected"]:
                            send_pdu(session["conn"], game_over_pdu, VERBOSE, f"GAME_OVER to Player {session_player_id}")
                            
                    current_game_state = None
                    turn_manager = None
                    stack_manager = None
                    priority_manager = None
                    lobby.ready_players.clear()
                    print("[SERVER] Game over. Returned to LOBBY state.")

                elif pdu.type in ["PRIORITY_PASS", "PLAY_LAND", "CAST_SPELL", "ACTIVATE_ABILITY", "DISCARD", "DECLARE_ATTACKERS", "DECLARE_BLOCKERS", "ASSIGN_DAMAGE_ORDER"]:
                    if current_game_state is None:
                        error_msg = {
                            "type": "ERROR",
                            "seq_num": pdu.seq_num,
                            "code": "ILLEGAL_ACTION",
                            "message": "Game has not started yet."
                        }
                        send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")
                        continue
                        
                    game_player_id = lobby.ready_players[player_id]["player_id"]
                    route_gameplay_pdu(pdu, game_player_id, turn_manager, stack_manager, priority_manager)

            except ValidationError as e:
                print(f"[SERVER] Validation rejected action from Player {player_id}")
                error_msg = {
                    "type": "ERROR",
                    "seq_num": raw_dict.get("seq_num", 0),
                    "code": "ILLEGAL_DECK" if "ILLEGAL_DECK" in str(e) else "INVALID_JSON",
                    "message": str(e),
                    "rejected_action": raw_dict
                }
                send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")

            except ValueError as e:
                error_msg = {
                    "type": "ERROR",
                    "seq_num": raw_dict.get("seq_num", 0),
                    "code": "UNKNOWN_TYPE",
                    "message": str(e),
                    "rejected_action": raw_dict
                }
                send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")

    except (ConnectionResetError, OSError):
        print(f"\n[SERVER] Network drop detected for Player {player_id}.")
    finally:
        print(f"[SERVER] Player {player_id} disconnected. Starting {RECONNECT_TIMEOUT}s reconnect timer...")
        if player_id in sessions:
            sessions[player_id]["connected"] = False
            timer = threading.Timer(RECONNECT_TIMEOUT, trigger_forfeit, args=[player_id])
            sessions[player_id]["timer"] = timer
            timer.start()
        conn.close()

def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="MTGNP Server")
    parser.add_argument('-v', '--verbose', action='store_true', help="Enable verbose logging")
    args = parser.parse_args()
    VERBOSE = args.verbose

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
    server_sock.bind((HOST, PORT))
    
    server_sock.listen(5) 
    
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    if VERBOSE:
        print("[SERVER] Verbose mode is ON.")
    
    try:
        while True:
            conn, addr = server_sock.accept()
            assigned_id = None
            
            for pid, state in sessions.items():
                if not state["connected"]:
                    assigned_id = pid
                    break
            
            if not assigned_id and len(sessions) < 2:
                assigned_id = len(sessions) + 1
                
            if assigned_id:
                thread = threading.Thread(target=handle_client, args=(conn, addr, assigned_id), daemon=True)
                thread.start()
            else:
                print(f"[SERVER] Rejected connection from {addr}: Lobby full.")
                error_msg = {
                    "type": "ERROR",
                    "code": "LOBBY_FULL",
                    "message": "The server already has two active players."
                }
                send_pdu(conn, error_msg, VERBOSE, "to rejected client")
                conn.close()
                
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
    finally:
        server_sock.close()

if __name__ == "__main__":
    main()