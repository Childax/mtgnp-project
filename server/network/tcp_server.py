import socket
import struct
import json
import threading

HOST = '127.0.0.1'
PORT = 4444

def receive_exact(sock, num_bytes):
    """Helper function to read exactly 'num_bytes' from the socket."""
    data = bytearray()
    while len(data) < num_bytes:
        packet = sock.recv(num_bytes - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

def send_pdu(sock, pdu_dict):
    """Frames and sends a JSON PDU over the socket."""
    json_data = json.dumps(pdu_dict).encode('utf-8')
    message_length = len(json_data)
    
    # Pack length as a 4-byte big-endian unsigned integer (>I)
    framed_message = struct.pack('>I', message_length) + json_data
    sock.sendall(framed_message)

def handle_client(conn, addr, player_id):
    print(f"[SERVER] Player {player_id} connected from {addr}")
    try:
        while True:
            # Read 4-byte length prefix
            length_prefix = receive_exact(conn, 4)
            if not length_prefix:
                break
            
            # Unpack the big-endian unsigned integer
            message_length = struct.unpack('>I', length_prefix)[0]
            
            # Enforce max PDU size of 65,535 bytes
            if message_length > 65535:
                print(f"[SERVER] Error: Message exceeds max PDU size (got {message_length} bytes).")
                break

            # Read JSON payload
            payload_bytes = receive_exact(conn, message_length)
            if not payload_bytes:
                break
            
            payload_str = payload_bytes.decode('utf-8')
            
            try:
                pdu = json.loads(payload_str)
                print(f"[SERVER] Received from Player {player_id}: {pdu}")
                
                # Test functionality: Respond to PING with PONG
                if pdu.get("type") == "PING":
                    response = {
                        "type": "PONG", 
                        "seq_num": pdu.get("seq_num", 0), 
                        "timestamp": pdu.get("timestamp", 0)
                    }
                    send_pdu(conn, response)
                    print(f"[SERVER] Sent PONG to Player {player_id}")
                    
            except json.JSONDecodeError:
                print(f"[SERVER] Error: Invalid JSON received from Player {player_id}.")

    except ConnectionResetError:
        pass
    finally:
        print(f"[SERVER] Player {player_id} disconnected.")
        conn.close()

def main():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allow port reuse
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
    server_sock.bind((HOST, PORT))
    
    # Listen for connections
    server_sock.listen(2)
    
    print(f"[SERVER] Listening on {HOST}:{PORT}")
    print("[SERVER] Waiting for exactly 2 players to connect...")
    
    clients = []
    try:
        # Accept exactly 2 clients, then stop accepting connections
        while len(clients) < 2:
            conn, addr = server_sock.accept()
            player_id = len(clients) + 1
            clients.append(conn)
            
            thread = threading.Thread(target=handle_client, args=(conn, addr, player_id), daemon=True)
            thread.start()
            
        print("[SERVER] Two players connected. Game Server ready. Refusing further connections.")
        
        # Keep the main thread alive to watch over the client threads
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