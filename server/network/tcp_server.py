import socket
import struct
import json
import threading
import argparse

HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False

def receive_exact(sock, num_bytes):
    data = bytearray()
    while len(data) < num_bytes:
        packet = sock.recv(num_bytes - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

def send_pdu(sock, pdu_dict, player_id):
    try:
        json_data = json.dumps(pdu_dict).encode('utf-8')
        message_length = len(json_data)
        
        if VERBOSE:
            print(f"\n[VERBOSE] SENT to Player {player_id} | {message_length} bytes")
            print(f"[VERBOSE] RAW: {json_data.decode('utf-8')}")
            
        framed_message = struct.pack('>I', message_length) + json_data
        sock.sendall(framed_message)
    except Exception as e:
        print(f"\n[SERVER] Failed to send PDU to Player {player_id}: {e}")

def handle_client(conn, addr, player_id):
    print(f"[SERVER] Player {player_id} connected from {addr}")
    try:
        while True:
            length_prefix = receive_exact(conn, 4)
            if not length_prefix:
                break
            
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
                
                # Echo PING with PONG
                if pdu.get("type") == "PING":
                    response = {
                        "type": "PONG", 
                        "seq_num": pdu.get("seq_num", 0), 
                        "timestamp": pdu.get("timestamp", 0)
                    }
                    send_pdu(conn, response, player_id)
                    
            except json.JSONDecodeError:
                print(f"[SERVER] Error: Invalid JSON received from Player {player_id}.")

    except (ConnectionResetError, OSError):
        # This catches unexpected drops
        print(f"[SERVER] Network drop detected for Player {player_id}.")
    finally:
        print(f"[SERVER] Player {player_id} disconnected.")
        # TODO: trigger GAME_OVER logic here if IN_GAME (once implemented in lifecycle)
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