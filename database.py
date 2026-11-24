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

_db_initialized = False


async def init_db() -> None:
    """Create indexes and seed 3 buildings x 3 slots if missing (cached after first run)."""
    global _db_initialized
    if _db_initialized:
        return

    # If slots already exist, skip index creation and seeding on serverless starts
    count = await slots.estimated_document_count()
    if count >= 9:
        _db_initialized = True
        return

    import asyncio
    await asyncio.gather(
        slots.create_index([("building_id", 1), ("slot_number", 1)], unique=True),
        bookings.create_index([("status", 1), ("expires_at", 1)]),
        bookings.create_index([("user_id", 1), ("created_at", -1)]),
        users.create_index("email", unique=True),
        transactions.create_index("booking_id"),
    )
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
    _db_initialized = True
