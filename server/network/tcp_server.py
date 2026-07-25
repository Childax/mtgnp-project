import socket
import json
import threading
import argparse
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from shared.network_utils import receive_exact, send_pdu

HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False

def handle_client(conn, addr, player_id):
    print(f"[SERVER] Player {player_id} connected from {addr}")
    try:
        while True:
            # We use the shared receive function here
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
                pdu = json.loads(payload_str)
                
                # --- NEW STRICT VALIDATION ---
                if "type" not in pdu or "seq_num" not in pdu:
                    print(f"[SERVER] Rejected invalid PDU from Player {player_id}")
                    error_msg = {
                        "type": "ERROR",
                        "error_code": "UNKNOWN_TYPE",
                        "message": "PDU must contain 'type' and 'seq_num'."
                    }
                    send_pdu(conn, error_msg, VERBOSE, f"to Player {player_id}")
                    continue
                
                # Echo PING with PONG
                if pdu.get("type") == "PING":
                    response = {
                        "type": "PONG", 
                        "seq_num": pdu.get("seq_num"), 
                        "timestamp": pdu.get("timestamp", 0)
                    }
                    send_pdu(conn, response, VERBOSE, f"to Player {player_id}")
                    
            except json.JSONDecodeError:
                print(f"[SERVER] Error: Invalid JSON received from Player {player_id}.")

    except (ConnectionResetError, OSError):
        print(f"[SERVER] Network drop detected for Player {player_id}.")
    finally:
        print(f"[SERVER] Player {player_id} disconnected.")
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
    server_sock.listen(2)
    
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    if VERBOSE:
        print("[SERVER] Verbose mode is ON.")
    print("[SERVER] Waiting for exactly 2 players to connect...")
    
    clients = []
    try:
        while len(clients) < 2:
            conn, addr = server_sock.accept()
            player_id = len(clients) + 1
            clients.append(conn)
            
            thread = threading.Thread(target=handle_client, args=(conn, addr, player_id), daemon=True)
            thread.start()
            
        print("[SERVER] Two players connected. Game Server ready. Refusing further connections.")
        
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join()
                
    except KeyboardInterrupt:
        print("\n[SERVER] Shutting down.")
    finally:
        for c in clients:
            c.close()
        server_sock.close()

if __name__ == "__main__":
    main()