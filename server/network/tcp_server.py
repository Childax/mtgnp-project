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
from shared.pdu import parse_pdu, BasePDU
from server.core.lifecycle import LobbyManager


HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False

# Track player sessions and connection states
sessions = {}
lobby = LobbyManager()
RECONNECT_TIMEOUT = 60.0 # 60 seconds to reconnect

def trigger_forfeit(player_id):
    """Called when a player fails to reconnect within the time limit."""
    if sessions.get(player_id) and not sessions[player_id]["connected"]:
        print(f"\n[SERVER] Player {player_id} failed to reconnect in time. FORFEIT.")
        # TODO: Broadcast GAME_OVER (DISCONNECT) to the remaining player

def handle_client(conn, addr, player_id):
    print(f"[SERVER] Player {player_id} connected from {addr}")
    
    # Cancel any existing disconnect timer since they just reconnected
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
                
                # 1. Strict parsing and validation via pdu.py
                pdu = parse_pdu(raw_dict)
                
                # 2. Handle System Messages
                if pdu.type == "PING":
                    response = {
                        "type": "PONG", 
                        "seq_num": pdu.seq_num, 
                        "timestamp": pdu.timestamp
                    }
                    send_pdu(conn, response, VERBOSE, f"to Player {player_id}")
                    continue

                # 3. Route Protocol Messages to the appropriate state manager
                if pdu.type == "PLAYER_READY":
                    success, status, data = lobby.process_player_ready(player_id, pdu)
                    
                    if not success:
                        # DUPLICATE_ID violation
                        error_msg = {
                            "type": "ERROR",
                            "seq_num": pdu.seq_num,
                            "code": status,
                            "message": data,
                            "rejected_action": raw_dict
                        }
                        send_pdu(conn, error_msg, VERBOSE, f"ERROR to Player {player_id}")
                    else:
                        # Success! Send the GAME_STATE_UPDATE back to the client(s)
                        update_msg = {
                            "type": "GAME_STATE_UPDATE",
                            "seq_num": 2, # Hardcoded for Week 1 milestone testing
                            "state": data
                        }
                        # If status is GAME_SETUP, we broadcast to ALL clients. 
                        # For now, let's just send it back to the active client to confirm ready state.
                        if status == "GAME_SETUP":
                            print("\n[SERVER] AUTOMATA TRANSITION: LOBBY -> GAME_SETUP")

                            # Both players must be informed that game setup has started.
                            for session_player_id, session in sessions.items():
                                if session["connected"]:
                                    send_pdu(
                                        session["conn"],
                                        update_msg,
                                        VERBOSE,
                                        f"GAME_SETUP to Player {session_player_id}"
                                    )
                        else:
                            # While still waiting in the lobby, reply only to the player
                            # who most recently sent PLAYER_READY.
                            send_pdu(
                                conn,
                                update_msg,
                                VERBOSE,
                                f"LOBBY STATE to Player {player_id}"
                            )
                            # We will add the logic to broadcast to both clients and start 
                            # shuffling decks in the next step.

            except ValidationError as e:
                # Catch invalid schemas, missing fields, or illegal decks
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
                # Catch UNKNOWN_TYPE
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
        
        # Flag the player as disconnected and start the countdown
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
        # Continuous loop to accept new connections and reconnections
        while True:
            conn, addr = server_sock.accept()
            assigned_id = None
            
            # 1. Check if there is a disconnected slot we can put this player into
            for pid, state in sessions.items():
                if not state["connected"]:
                    assigned_id = pid
                    break
            
            # 2. If no disconnected slots, check if there is room for a brand new player
            if not assigned_id and len(sessions) < 2:
                assigned_id = len(sessions) + 1
                
            # 3. Route them to the game or reject them
            if assigned_id:
                thread = threading.Thread(target=handle_client, args=(conn, addr, assigned_id), daemon=True)
                thread.start()
            else:
                print(f"[SERVER] Rejected connection from {addr}: Lobby full.")
                error_msg = {
                    "type": "ERROR",
                    "error_code": "LOBBY_FULL",
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