import socket
import time
import sys
import json
import struct
import select

def receive_exact(sock, num_bytes):
    data = bytearray()
    while len(data) < num_bytes:
        packet = sock.recv(num_bytes - len(data))
        if not packet: return None
        data.extend(packet)
    return data

def send_pdu(sock, pdu_dict):
    json_data = json.dumps(pdu_dict).encode('utf-8')
    sock.sendall(struct.pack('>I', len(json_data)) + json_data)

def receive_pdu(sock):
    length_prefix = receive_exact(sock, 4)
    if not length_prefix: return None
    message_length = struct.unpack('>I', length_prefix)[0]
    payload = receive_exact(sock, message_length)
    return json.loads(payload.decode('utf-8'))

def flush_socket(sock):
    """Safely reads all pending PDUs from the socket buffer."""
    pdus = []
    while True:
        readable, _, _ = select.select([sock], [], [], 0.1)
        if not readable: break
        pdu = receive_pdu(sock)
        if pdu: pdus.append(pdu)
        else: break
    return pdus

def main():
    print("=== MTGNP WEEK 2 DELIVERABLES DEMO ===")

    client_a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_b = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_a.connect(('127.0.0.1', 4444))
    client_b.connect(('127.0.0.1', 4444))

    deck_a = ["mountain_001", "goblin_guide_001"] + ["mountain_002"] * 8
    deck_b = ["island_001", "counterspell_001"] + ["island_002"] * 8

    print("\n[+] Advancing through LOBBY and MULLIGAN...")
    send_pdu(client_a, {"type": "PLAYER_READY", "seq_num": 1, "player_id": "p1", "deck_list": deck_a})
    send_pdu(client_b, {"type": "PLAYER_READY", "seq_num": 1, "player_id": "p2", "deck_list": deck_b})
    time.sleep(0.5)

    send_pdu(client_a, {"type": "MULLIGAN_CHOICE", "seq_num": 2, "keep": True, "cards_to_bottom": []})
    send_pdu(client_b, {"type": "MULLIGAN_CHOICE", "seq_num": 2, "keep": True, "cards_to_bottom": []})
    time.sleep(1.0) # Wait for engine to boot and dispatch turn 1 PDUs

    # 3. Dynamically capture the coin flip winner
    pdus_a = flush_socket(client_a)
    pdus_b = flush_socket(client_b)
    
    active_client, inactive_client, active_seq_num = None, None, None

    for pdu in pdus_a:
        if pdu.get("type") == "PRIORITY_GRANT":
            active_client, inactive_client = client_a, client_b
            active_seq_num = pdu.get("seq_num")
            print("[+] Player 1 won the coin flip.")
            break
            
    if not active_seq_num:
        for pdu in pdus_b:
            if pdu.get("type") == "PRIORITY_GRANT":
                active_client, inactive_client = client_b, client_a
                active_seq_num = pdu.get("seq_num")
                print("[+] Player 2 won the coin flip.")
                break

    if not active_seq_num:
        print("[-] ERROR: Could not find PRIORITY_GRANT token. Engine boot failed.")
        return

    print(f"[+] IN_GAME Reached. Active Priority seq_num: {active_seq_num}")

    # =================================================================
    # DEMO 1: A player without priority cannot cast a spell
    # =================================================================
    print("\n--- DEMO 1: Action Without Priority ---")
    send_pdu(inactive_client, {
        "type": "PRIORITY_PASS", 
        "seq_num": active_seq_num 
    })
    err_pdu = next((p for p in flush_socket(inactive_client) if p.get("type") == "ERROR"), {})
    print(f"Inactive Client Response: {err_pdu.get('type')} - {err_pdu.get('code')}")

    # =================================================================
    # DEMO 2: A stale action does not modify the state
    # =================================================================
    print("\n--- DEMO 2: Stale Sequence Number ---")
    send_pdu(active_client, {
        "type": "PRIORITY_PASS", 
        "seq_num": 0 
    })
    err_pdu = next((p for p in flush_socket(active_client) if p.get("type") == "ERROR"), {})
    print(f"Active Client Response: {err_pdu.get('type')} - {err_pdu.get('code')}")

    # =================================================================
    # DEMO 3: Invalid gameplay actions return the correct ERROR
    # =================================================================
    print("\n--- DEMO 3: Invalid Gameplay Action (Wrong Phase) ---")
    send_pdu(active_client, {
        "type": "PLAY_LAND", 
        "seq_num": active_seq_num, # Correct token, correct player
        "card_id": "mountain_001"
    })
    err_pdu = next((p for p in flush_socket(active_client) if p.get("type") == "ERROR"), {})
    print(f"Active Client Response: {err_pdu.get('type')} - {err_pdu.get('code')}")

    # =================================================================
    # DEMO 4: CONCEDE triggers GAME_OVER
    # =================================================================
    print("\n--- DEMO 4: CONCEDE triggers GAME_OVER ---")
    send_pdu(active_client, {"type": "CONCEDE", "seq_num": 999, "player_id": "p1"})
    time.sleep(0.5)
    
    game_over_pdu = next((p for p in flush_socket(inactive_client) if p.get("type") == "GAME_OVER"), {})
    print(f"Inactive Client Received: {game_over_pdu.get('type')} | Reason: {game_over_pdu.get('reason')}")

    # =================================================================
    # DEMO 5: Return to LOBBY after GAME_OVER
    # =================================================================
    print("\n--- DEMO 5: Return to LOBBY after GAME_OVER ---")
    send_pdu(active_client, {"type": "PLAYER_READY", "seq_num": 1, "player_id": "p1", "deck_list": deck_a})
    send_pdu(inactive_client, {"type": "PLAYER_READY", "seq_num": 1, "player_id": "p2", "deck_list": deck_b})
    time.sleep(0.5)
    
    lobby_pdu = next((p for p in flush_socket(active_client) if p.get("type") == "GAME_STATE_UPDATE"), {})
    print(f"Client Received: {lobby_pdu.get('type')} | Phase: {lobby_pdu.get('state', {}).get('phase')}")

    print("\n=== DEMO COMPLETE ===")
    client_a.close()
    client_b.close()

if __name__ == "__main__":
    main()