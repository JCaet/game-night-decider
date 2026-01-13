import asyncio
import re

import httpx


async def test_real_token():
    # Read .env manually
    try:
        with open(".env") as f:
            content = f.read()
    except:
        print("Could not read .env")
        return

    # Look for BGG_API_TOKEN (commented or not)
    match = re.search(r'BGG_API_TOKEN\s*=\s*["\']?([^"\']+)["\']?', content)
    if not match:
        print("Could not find BGG_API_TOKEN in .env")
        return

    token = match.group(1).strip()
    print(f"Found token: {token[:5]}...{token[-5:]}")

    url = "https://boardgamegeek.com/xmlapi2/search"
    params = {"query": "Catan", "type": "boardgame"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {token}",
    }

    print("Testing SEARCH with REAL token...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=10.0)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print("✓ SEARCH SUCCESS!")
            else:
                print("✗ SEARCH Failed")
        except Exception as e:
            print(f"Error: {e}")

    print("\nTesting COLLECTION with REAL token...")
    async with httpx.AsyncClient() as client:
        try:
            col_url = "https://boardgamegeek.com/xmlapi2/collection"
            col_params = {"username": "Zman", "own": 1}
            # Add stats=1 as per BGGClient
            # col_params["stats"] = 1

            resp = await client.get(col_url, params=col_params, headers=headers, timeout=10.0)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print("✓ COLLECTION SUCCESS!")
            else:
                print(f"✗ COLLECTION Failed: {resp.text[:100]}")
        except Exception as e:
            print(f"Error: {e}")

    print("\nTesting COLLECTION with REAL token AND STATS=1...")
    async with httpx.AsyncClient() as client:
        try:
            col_url = "https://boardgamegeek.com/xmlapi2/collection"
            col_params = {"username": "Zman", "own": 1, "stats": 1}

            resp = await client.get(col_url, params=col_params, headers=headers, timeout=10.0)
            print(f"Status: {resp.status_code}")
            if resp.status_code in [200, 202]:
                print(f"✓ COLLECTION+STATS SUCCESS! (Status {resp.status_code})")
            elif resp.status_code == 401:
                print("✗ COLLECTION+STATS Failed with 401 UNAUTHORIZED")
            else:
                print(f"✗ Failed: {resp.status_code}")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_real_token())
