import socket
import struct
import json
import threading
import time
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

def send_pdu(sock, pdu_dict):
    json_data = json.dumps(pdu_dict).encode('utf-8')
    message_length = len(json_data)
    
    if VERBOSE:
        print(f"\n[VERBOSE] SENT to Server | {message_length} bytes")
        print(f"[VERBOSE] RAW: {json_data.decode('utf-8')}")
        
    framed_message = struct.pack('>I', message_length) + json_data
    sock.sendall(framed_message)

def heartbeat_loop(sock):
    """Sends a PING automatically in the background."""
    seq_num = 9000 
    while True:
        time.sleep(15) 
        try:
            pdu = {
                "type": "PING",
                "seq_num": seq_num,
                "timestamp": int(time.time() * 1000)
            }
            send_pdu(sock, pdu)
            seq_num += 1
        except Exception:
            break 

def listen_for_messages(sock):
    try:
        while True:
            length_prefix = receive_exact(sock, 4)
            if not length_prefix:
                print("\n[CLIENT] Disconnected from server.")
                break
            
            message_length = struct.unpack('>I', length_prefix)[0]
            payload_bytes = receive_exact(sock, message_length)
            
            if payload_bytes:
                payload_str = payload_bytes.decode('utf-8')
                
                if VERBOSE:
                    print(f"\n[VERBOSE] RECV from Server | {message_length} bytes")
                    print(f"[VERBOSE] RAW: {payload_str}")
                    
                pdu = json.loads(payload_str)
                
                if not VERBOSE:
                    print(f"[CLIENT] Received {pdu.get('type')} PDU")
                
    except (ConnectionResetError, json.JSONDecodeError, struct.error):
        print("\n[CLIENT] Connection closed or network error occurred.")

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

    # Start the automated heartbeat
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

if __name__ == "__main__":
    main()