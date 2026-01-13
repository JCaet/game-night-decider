import asyncio

import httpx


async def test_collection_with_and_without_auth():
    """Test if collection endpoint works without auth header"""

    test_username = "Zman"
    base_url = "https://boardgamegeek.com/xmlapi2/collection"
    params = {"username": test_username, "own": 1}

    # Test 1: WITHOUT any auth header
    print("Test 1: Collection WITHOUT Authorization header")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                base_url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0
            )
            print(f"  Status: {response.status_code}")
            if response.status_code == 200:
                print(f"  ✓ SUCCESS - Got {len(response.content)} bytes")
            elif response.status_code == 202:
                print("  ⏳ Queued (202)")
            else:
                print(f"  ✗ Failed: {response.status_code}")
        except Exception as e:
            print(f"  ✗ Error: {e}")

    # Test 2: WITH fake auth header (like the invalid token)
    print("\nTest 2: Collection WITH fake Authorization header")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                base_url,
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Authorization": "Bearer fake_token_12345"},
                timeout=10.0,
            )
            print(f"  Status: {response.status_code}")
            if response.status_code == 401:
                print("  ✓ CONFIRMED - Invalid auth causes 401")
        except Exception as e:
            print(f"  ✗ Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_collection_with_and_without_auth())
