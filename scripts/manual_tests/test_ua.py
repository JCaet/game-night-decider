import asyncio

import httpx


async def test_user_agents():
    url = "https://boardgamegeek.com/xmlapi2/collection"
    params = {"username": "Zman", "own": 1}

    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "GameNightDecider/1.0 (Telegram Bot)",
        "python-httpx/0.27.0",
        "PostmanRuntime/7.26.8",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "",  # No User-Agent
    ]

    print(f"Testing URL: {url}")
    print("-" * 60)

    async with httpx.AsyncClient() as client:
        for ua in user_agents:
            headers = {"User-Agent": ua} if ua else {}
            ua_label = ua if ua else "[None]"

            try:
                response = await client.get(url, params=params, headers=headers, timeout=10.0)
                print(f"UA: {ua_label[:50]}...")
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
    asyncio.run(test_user_agents())
