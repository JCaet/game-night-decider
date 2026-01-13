import asyncio
import os

from dotenv import load_dotenv

from src.core.bgg import BGGClient

load_dotenv()


async def test_bgg_fetch():
    token = os.getenv("BGG_API_TOKEN")
    print(f"DEBUG: Loaded Token: {repr(token)}")
    client = BGGClient()

    # Test with a known valid BGG username
    test_username = "Zman"  # BGG designer, should have games

    print(f"Testing BGG collection fetch for '{test_username}'...")
    try:
        games = await client.fetch_collection(test_username)
        print(f"✓ Successfully fetched {len(games)} games")
        if games:
            print(f"  First game: {games[0].name}")
    except ValueError as e:
        print(f"✗ ValueError: {e}")
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")

    # Test with invalid username
    print("\nTesting with invalid username...")
    try:
        games = await client.fetch_collection("thisuserdoesnotexist12345")
        print(f"✗ Should have raised ValueError but got {len(games)} games")
    except ValueError as e:
        print(f"✓ Correctly raised ValueError: {e}")
    except Exception as e:
        print(f"✗ Wrong exception type: {type(e).__name__}: {e}")

    # Test with trailing spaces
    print("\nTesting username with trailing spaces...")
    try:
        games = await client.fetch_collection("  Zman  ".strip())
        print(f"✓ Successfully fetched {len(games)} games (with stripped spaces)")
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test_bgg_fetch())
