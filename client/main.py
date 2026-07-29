from client.network.tcp_client import start_client

TEST_DECKS = {
    "A": [
        "mountain_001",
        "mountain_002",
        "mountain_003",
        "mountain_004",
        "mountain_005",
        "mountain_006",
        "mountain_007",
        "lightning_bolt_001",
        "shock_001",
        "goblin_guide_001",
    ],
    "B": [
        "forest_001",
        "forest_002",
        "forest_003",
        "forest_004",
        "forest_005",
        "forest_006",
        "forest_007",
        "giant_growth_001",
        "grizzly_bears_001",
        "llanowar_elves_001",
    ],
}


def prompt_player_id():
    while True:
        player_id = input("Enter your player ID: ").strip()

        if player_id:
            return player_id

        print("Player ID cannot be empty.")


def prompt_deck():
    while True:
        deck_choice = input("Choose test deck A or B: ").strip().upper()

        if deck_choice in TEST_DECKS:
            return TEST_DECKS[deck_choice]

        print("Invalid choice. Please enter A or B.")


def main():
    print("=== MTGNP Client Setup ===")

    player_id = prompt_player_id()
    deck_list = prompt_deck()

    print("\nClient configuration:")
    print(f"Player ID: {player_id}")
    print(f"Deck size: {len(deck_list)} cards")
    print(f"Deck: {deck_list}")
    
    print("\nConnecting to the game server...")
    start_client(player_id, deck_list)


if __name__ == "__main__":
    main()