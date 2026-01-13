import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BGG_API_TOKEN")

print(f"Token loaded: {repr(TOKEN)}")


async def replicate_user_code():
    url = "https://boardgamegeek.com/xmlapi2/search"
    params = {"query": "Catan", "type": "boardgame"}

    headers = {}
    if TOKEN:
        # User code: headers["Authorization"] = f"Bearer {BGGClient.API_KEY}"
        headers["Authorization"] = f"Bearer {TOKEN}"
        print("Using Authorization header")
    else:
        print("No token found")

    print(f"Headers: {headers}")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=10.0)
            print(f"Status: {resp.status_code}")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(replicate_user_code())
