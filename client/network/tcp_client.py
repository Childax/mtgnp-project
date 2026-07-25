import socket
import json
import threading
import time
import argparse
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
from shared.network_utils import receive_exact, send_pdu

HOST = '127.0.0.1'
PORT = 4444
VERBOSE = False
last_pong_time = time.time()

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

def listen_for_messages(sock):
    global last_pong_time
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
                
                if pdu.get("type") == "PONG":
                    last_pong_time = time.time()
                
                if not VERBOSE:
                    print(f"[CLIENT] Received {pdu.get('type')} PDU")
                
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

if __name__ == "__main__":
    main()