import asyncio
import json
import sys

sys.path.insert(0, ".")
from adapters import robu as robu_mod


async def main():
    a = robu_mod.RobuAdapter()
    try:
        session = await a._get_session()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://robu.in",
            "Referer": "https://robu.in/",
        }
        r = await session.post(
            "https://robu.in/api/search/",
            headers=headers,
            json={"q": "raspberry pi pico", "limit": 2, "offset": 0, "semantic": False, "sort": ""},
            timeout=30,
        )
        print("status", r.status_code)
        data = r.json()
        prods = data.get("data", {}).get("products", [])
        print("n", len(prods))
        if prods:
            print(json.dumps(prods[0], indent=2, default=str)[:4000])
    finally:
        await a.close()


asyncio.run(main())
