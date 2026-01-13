import asyncio

import httpx


async def test_params():
    base_url = "https://boardgamegeek.com/xmlapi2/collection"
    username = "Zman"

    # Define test cases for parameters
    test_cases = [
        ("Minimal (username only)", {"username": username}),
        ("With own=1", {"username": username, "own": 1}),
        ("With stats=1", {"username": username, "stats": 1}),
        ("With excludesubtype", {"username": username, "excludesubtype": "boardgameexpansion"}),
        (
            "Full params",
            {"username": username, "own": 1, "stats": 1, "excludesubtype": "boardgameexpansion"},
        ),
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print(f"Testing Parameters for {base_url}")
    print("-" * 60)

    async with httpx.AsyncClient() as client:
        for label, params in test_cases:
            try:
                response = await client.get(base_url, params=params, headers=headers, timeout=10.0)
                print(f"Params: {label}")
                print(f"  Status: {response.status_code}")
                if response.status_code == 200:
                    print("  ✓ SUCCESS")
                elif response.status_code == 401:
                    print("  ✗ 401 Unauthorized")
                else:
                    print(f"  ? {response.status_code}")
            except Exception as e:
                print(f"  ! Error: {e}")
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(test_params())
