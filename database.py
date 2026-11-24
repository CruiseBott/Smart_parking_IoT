import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import dns.resolver
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ["8.8.8.8", "1.1.1.1"]

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "smart_parking")

client = AsyncIOMotorClient(MONGO_URI, tz_aware=True)
db = client[DB_NAME]
slots = db["slots"]
bookings = db["bookings"]
users = db["users"]
transactions = db["transactions"]
meta = db["meta"]  # ESP32 heartbeat etc.

BUILDINGS = (1, 2, 3)
SLOTS_PER_BUILDING = (1, 2, 3)


async def init_db() -> None:
    """Create indexes and seed 3 buildings x 3 slots if missing."""
    await slots.create_index([("building_id", 1), ("slot_number", 1)], unique=True)
    await bookings.create_index([("status", 1), ("expires_at", 1)])
    await bookings.create_index([("user_id", 1), ("created_at", -1)])
    await users.create_index("email", unique=True)
    await transactions.create_index("booking_id")
    now = datetime.now(timezone.utc)
    for b in BUILDINGS:
        for s in SLOTS_PER_BUILDING:
            await slots.update_one(
                {"_id": f"B{b}-S{s}"},
                {"$setOnInsert": {
                    "building_id": b, "slot_number": s, "status": "FREE",
                    "ir_occupied": False, "active_booking_id": None, "updated_at": now,
                }},
                upsert=True,
            )
