import os
import json

class BattlefieldUI:
    def __init__(self, player_id):
        self.player_id = player_id
        # Load the card catalog
        with open("shared/data/card_catalog.json", "r") as f:
            self.catalog = json.load(f)

    def get_card_name(self, card_id):
        """Helper to look up the human-readable name of a card."""
        base_id = "_".join(card_id.split("_")[:-1])
        card = self.catalog.get(card_id) or self.catalog.get(base_id)
        return card.get("name", card_id) if card else card_id

    def clear_screen(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def render(self, state_data):
        self.clear_screen()
        state = state_data.get("state", {})
        
        # Determine opponent ID
        life_totals = state.get("life_totals", {})
        opponent_id = next((pid for pid in life_totals.keys() if pid != self.player_id), "Opponent")

        print("="*60)
        print(f" TURN: {state.get('turn', 0)} | PHASE: {state.get('phase', 'UNKNOWN')} | ACTIVE: {state.get('active_player', 'N/A')}")
        print("="*60)

        # --- OPPONENT ZONE ---
        opp_life = life_totals.get(opponent_id, 20)
        opp_deck = state.get("library_counts", {}).get(opponent_id, 0)
        opp_hand = state.get("hand_counts", {}).get(opponent_id, 0)
        print(f"[ OPPONENT: {opponent_id} ]")
        print(f"Life: {opp_life} | Hand: {opp_hand} cards | Deck: {opp_deck} cards")
        print("-" * 60)

        # --- BATTLEFIELD ZONE ---
        battlefield = state.get("battlefield", {})
        print("[ BATTLEFIELD ]")
        
        # Opponent's Permanents
        print(f"{opponent_id}'s Board:")
        for perm in battlefield.get(opponent_id, []):
            status = " (Tapped)" if perm.get("tapped") else ""
            print(f"  - {self.get_card_name(perm['id'])}{status}")
            
        print("\nYour Board:")
        # Your Permanents
        for perm in battlefield.get(self.player_id, []):
            status = " (Tapped)" if perm.get("tapped") else ""
            print(f"  - {self.get_card_name(perm['id'])}{status}")
        print("-" * 60)

        # --- STACK ZONE ---
        stack = state.get("stack", [])
        if stack:
            print("[ THE STACK ]")
            for item in reversed(stack):
                print(f"  -> {self.get_card_name(item['source'])} (Target: {item.get('targets', [])})")
            print("-" * 60)

        # --- PLAYER ZONE ---
        my_life = life_totals.get(self.player_id, 20)
        my_deck = state.get("library_counts", {}).get(self.player_id, 0)
        print(f"[ YOU: {self.player_id} ]")
        print(f"Life: {my_life} | Deck: {my_deck} cards")
        
        print("\nYour Hand:")
        my_hand = state.get("hand", {}).get(self.player_id, [])
        for i, card_id in enumerate(my_hand):
            print(f"  [{i}] {self.get_card_name(card_id)} ({card_id})")
        print("="*60)
        
        priority = state.get("priority_holder")
        if priority == self.player_id:
            print("\n*** YOU HAVE PRIORITY ***")