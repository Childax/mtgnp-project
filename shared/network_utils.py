import struct
import json

def receive_exact(sock, num_bytes):
    """Reads exactly num_bytes from the TCP socket."""
    data = bytearray()
    while len(data) < num_bytes:
        packet = sock.recv(num_bytes - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

def send_pdu(sock, pdu_dict, verbose=False, log_label=""):
    """Encodes a dictionary to JSON, frames it with a 4-byte prefix, and sends it."""
    try:
        json_data = json.dumps(pdu_dict).encode('utf-8')
        message_length = len(json_data)
        
        if verbose:
            print(f"\n[VERBOSE] SENT {log_label} | {message_length} bytes")
            print(f"[VERBOSE] RAW: {json_data.decode('utf-8')}")
            
        framed_message = struct.pack('>I', message_length) + json_data
        sock.sendall(framed_message)
    except Exception as e:
        print(f"\n[ERROR] Failed to send PDU {log_label}: {e}")