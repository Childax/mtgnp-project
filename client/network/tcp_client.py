import socket
import struct
import json
import threading
import time

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

def listen_for_messages(sock):
    """Background thread function to listen for and parse incoming PDUs."""
    try:
        while True:
            length_prefix = receive_exact(sock, 4)
            if not length_prefix:
                print("\n[CLIENT] Disconnected from server.")
                break
            
            message_length = struct.unpack('>I', length_prefix)[0]
            payload_bytes = receive_exact(sock, message_length)
            
            if payload_bytes:
                pdu = json.loads(payload_bytes.decode('utf-8'))
                print(f"\n[CLIENT] Received PDU:")
                print(json.dumps(pdu, indent=2))
                print("> Enter command (PING, READY, EXIT): ", end="", flush=True)
                
    except (ConnectionResetError, json.JSONDecodeError, struct.error):
        print("\n[CLIENT] Connection closed or network error occurred.")

def send_pdu(sock, pdu_dict):
    """Frames and sends a JSON PDU over the socket."""
    json_data = json.dumps(pdu_dict).encode('utf-8')
    message_length = len(json_data)
    framed_message = struct.pack('>I', message_length) + json_data
    sock.sendall(framed_message)

def main():
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        client_sock.connect((HOST, PORT))
        print(f"[CLIENT] Connected to Game Server at {HOST}:{PORT}")
    except ConnectionRefusedError:
        print("[CLIENT] Connection refused. Make sure the server is running.")
        return

    # Start listening thread
    listener_thread = threading.Thread(target=listen_for_messages, args=(client_sock,), daemon=True)
    listener_thread.start()

    seq_num = 1
    
    try:
        while True:
            time.sleep(0.2)
            command = input("> Enter command (PING, READY, EXIT): ").strip().upper()
            
            if command == 'EXIT':
                break
            elif command == 'PING':
                pdu = {
                    "type": "PING",
                    "seq_num": seq_num,
                    "timestamp": int(time.time() * 1000)
                }
            elif command == 'READY':
                pdu = {
                    "type": "PLAYER_READY",
                    "seq_num": seq_num,
                    "player_id": "player_1_test",
                    "deck_list": ["lightning_bolt_001", "mountain_001"]
                }
            else:
                print("[CLIENT] Unknown command. Use PING or READY.")
                continue

            send_pdu(client_sock, pdu)
            print(f"[CLIENT] Sent {command} PDU (seq_num: {seq_num})")
            seq_num += 1
            
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[CLIENT] Closing connection.")
        client_sock.close()

if __name__ == "__main__":
    main()