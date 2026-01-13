import asyncio

import httpx


async def test_bgg_api():
    """Test BGG API directly to understand the 401 error"""

    test_username = "Zman"

    # Test different endpoints and parameters
    tests = [
        (
            "xmlapi2 with stats",
            "https://boardgamegeek.com/xmlapi2/collection",
            {"username": test_username, "own": 1, "stats": 1},
        ),
        (
            "xmlapi2 without stats",
            "https://boardgamegeek.com/xmlapi2/collection",
            {"username": test_username, "own": 1},
        ),
        (
            "xmlapi2 minimal",
            "https://boardgamegeek.com/xmlapi2/collection",
            {"username": test_username},
        ),
        ("xmlapi (older)", "https://boardgamegeek.com/xmlapi/collection/" + test_username, {}),
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient() as client:
        for name, url, params in tests:
            print(f"\nTesting {name}:")
            print(f"  URL: {url}")
            print(f"  Params: {params}")
            try:
                response = await client.get(url, params=params, headers=headers, timeout=10.0)
                print(f"  ✓ Status: {response.status_code}")
                if response.status_code == 200:
                    print(f"  ✓ Content length: {len(response.content)} bytes")
                    # Check if it's valid XML
                    if response.content.startswith(b"<?xml") or response.content.startswith(b"<"):
                        print("  ✓ Valid XML response")
                    else:
                        print(f"  ✗ Not XML: {response.content[:100]}")
                elif response.status_code == 202:
                    print("  ⏳ Request queued (202)")
                else:
                    print(f"  ✗ Response: {response.text[:200]}")
            except Exception as e:
                print(f"  ✗ Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_bgg_api())
