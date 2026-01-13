import asyncio

import httpx


async def test_search_endpoint():
    # Test valid search query
    url = "https://boardgamegeek.com/xmlapi2/search"
    params = {"query": "Catan", "type": "boardgame"}
    header = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print(f"Testing Search URL: {url}")
    print("-" * 60)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params, headers=header, timeout=10.0)
            print(f"Status: {response.status_code}")
            if response.status_code == 200:
                print(f"✓ SUCCESS - Search works! Length: {len(response.content)}")
            else:
                print(f"✗ Failed: {response.status_code}")
        except Exception as e:
            print(f"! Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_search_endpoint())
