from typing import Dict, Any, Tuple
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from shared.pdu import PlayerReadyPDU

class LobbyManager:
    def __init__(self):
        # Maps the server's internal assigned_id (1 or 2) to their ready data.
        self.ready_players: Dict[int, Dict[str, Any]] = {}

    def process_player_ready(self, assigned_id: int, pdu: PlayerReadyPDU) -> Tuple[bool, str, Any]:
        """
        Processes a PLAYER_READY PDU.
        Returns a tuple: (success: bool, error_code_or_message: str, broadcast_data: Any)
        """
        # Enforce unique player_id constraint
        for pid, data in self.ready_players.items():
            if pid != assigned_id and data["player_id"] == pdu.player_id:
                return False, "DUPLICATE_ID", "The player_id is already claimed by the other player."

        # Upsert the player's data (if they send it again, it overwrites the old deck)
        self.ready_players[assigned_id] = {
            "player_id": pdu.player_id,
            "deck_list": pdu.deck_list
        }

        # Check transition condition: Are both players ready?
        if len(self.ready_players) == 2:
            return True, "GAME_SETUP", self._generate_setup_state()
        
        # Otherwise, stay in LOBBY and return the updated waiting state
        return True, "LOBBY", self._generate_lobby_state()

    def _generate_lobby_state(self) -> Dict[str, Any]:
        """Generates the LOBBY variant of the GAME_STATE_UPDATE payload."""
        waiting_for = []
        # If we have 1 ready player, figure out who we are waiting for
        if len(self.ready_players) == 1:
            ready_client_id = list(self.ready_players.keys())[0]
            waiting_for.append(f"Player {3 - ready_client_id}") # If 1 is ready, wait for 2.

        return {
            "phase": "LOBBY",
            "players_ready": len(self.ready_players),
            "waiting_for": waiting_for
        }

    def _generate_setup_state(self) -> Dict[str, Any]:
        """Generates the GAME_SETUP transition state."""
        return {
            "phase": "GAME_SETUP",
            "players_ready": 2,
            "waiting_for": []
        }