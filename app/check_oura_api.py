import asyncio
import httpx
from datetime import datetime, timedelta
from app.database import SessionLocal, RingDevice

async def test_api():
    db = SessionLocal()
    ring = db.query(RingDevice).filter(RingDevice.is_active == True).first()
    if not ring or not ring.pat_token:
        print("No active ring found.")
        return

    headers = {
        "Authorization": f"Bearer {ring.pat_token}",
        "Content-Type": "application/json"
    }

    now = datetime.utcnow()
    start_str = "2026-07-15"
    end_str = now.strftime("%Y-%m-%d")

    endpoints = [
        "https://api.ouraring.com/v2/usercollection/ring_battery_level",
        "https://api.ouraring.com/v2/usercollection/ring_configuration",
        "https://api.ouraring.com/v2/usercollection/daily_activity"
    ]

    async with httpx.AsyncClient() as client:
        for url in endpoints:
            print(f"\n--- Fetching {url} ({start_str} to {end_str}) ---")
            try:
                resp = await client.get(url, headers=headers, params={"start_date": start_str, "end_date": end_str})
                print(f"Status: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"Items count: {len(data.get('data', []))}")
                    for item in data.get('data', [])[:15]:
                        print("  Item:", item)
                else:
                    print("Response:", resp.text)
            except Exception as e:
                print("Error:", e)

if __name__ == "__main__":
    asyncio.run(test_api())
