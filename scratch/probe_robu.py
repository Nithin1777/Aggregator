import asyncio
import json
import sys

sys.path.insert(0, ".")
from adapters.robu import RobuAdapter


async def main():
    a = RobuAdapter()
    try:
        prods = await a.search_live("raspberry pi pico", limit=3)
        print("robu products:", len(prods))
        for p in prods:
            print(" -", p.title[:70], "|", p.price)
            print("   extra:", json.dumps(p.extra, default=str)[:800])
    except Exception as e:
        print("robu failed:", type(e).__name__, e)
    finally:
        await a.close()


asyncio.run(main())
