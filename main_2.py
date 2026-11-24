import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import ObjectId
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import auth
import database as d
from auth import admin_user, current_user
from models_2 import BookingIn, GateVerifyIn, LoginIn, RegisterIn, SlotReading, TelemetryIn

BASE = Path(__file__).parent
ESP32_API_KEY = os.getenv("ESP32_API_KEY", "change-me")
RATE_PER_HOUR = int(os.getenv("RATE_PER_HOUR", "20"))
SIMULATOR = os.getenv("ENABLE_SIMULATOR", "true").lower() == "true"
GATE_OTP_TTL_SECONDS = int(os.getenv("GATE_OTP_TTL_SECONDS", "90"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def ser(v):
    """Make Mongo documents JSON friendly."""
    if isinstance(v, dict):
        return {k: ser(x) for k, x in v.items()}
    if isinstance(v, list):
        return [ser(x) for x in v]
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return v


def client_ip(req: Request):
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else None)


async def trigger_building_gate(building_id: int) -> int:
    """Increment monotonic gate sequence counter so ESP32 opens that building's servo once."""
    doc = await d.meta.find_one_and_update(
        {"_id": f"gate_{building_id}"},
        {"$inc": {"seq": 1}, "$set": {"updated_at": now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc.get("seq", 1))


# ---------------------------------------------------------------- expiry
async def expire_no_shows() -> int:
    """BOOKED + no vehicle + past start_time+grace -> EXPIRED_NO_SHOW, slot FREE."""
    t, n = now(), 0
    async for b in d.bookings.find({"status": "BOOKED", "expires_at": {"$lt": t}}):
        freed = await d.slots.find_one_and_update(
            {"active_booking_id": b["_id"], "status": "BOOKED", "ir_occupied": False},
            {"$set": {"status": "FREE", "active_booking_id": None, "updated_at": t}},
        )
        if freed:
            await d.bookings.update_one(
                {"_id": b["_id"], "status": "BOOKED"},
                {"$set": {"status": "EXPIRED_NO_SHOW", "expired_at": t,
                          "gate_otp": None, "gate_otp_expires_at": None}},
            )
            n += 1
    return n


async def expiry_loop():
    while True:
        try:
            await expire_no_shows()
        except Exception as e:
            print("expiry loop error:", e)
        await asyncio.sleep(20)


async def seed_admin():
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    pw = os.getenv("ADMIN_PASSWORD", "")
    if not email or not pw:
        return
    salt, h = auth.hash_pw(pw)
    await d.users.update_one(
        {"email": email},
        {"$setOnInsert": {"name": os.getenv("ADMIN_NAME", "Parking Admin"), "email": email,
                          "phone": "", "salt": salt, "password_hash": h, "role": "admin",
                          "active": True, "created_at": now()}},
        upsert=True,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await d.init_db()
    await seed_admin()
    task = None
    if not os.getenv("VERCEL"):
        task = asyncio.create_task(expiry_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Smart Parking", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def require_key(x_esp32_key: str = Header(default="")):
    if not secrets.compare_digest(x_esp32_key, ESP32_API_KEY):
        raise HTTPException(401, "Invalid ESP32 key")


# ---------------------------------------------------------------- views
async def building_view():
    all_slots = await d.slots.find().sort([("building_id", 1), ("slot_number", 1)]).to_list(50)
    ids = [s["active_booking_id"] for s in all_slots if s.get("active_booking_id")]
    bk = {b["_id"]: b async for b in d.bookings.find({"_id": {"$in": ids}})}
    out = []
    for bid in d.BUILDINGS:
        items = []
        for s in (x for x in all_slots if x["building_id"] == bid):
            b = bk.get(s.get("active_booking_id"))
            items.append({
                "slot_number": s["slot_number"], "status": s["status"],
                "ir_occupied": s["ir_occupied"], "updated_at": s["updated_at"],
                "booking": b and {
                    "booking_id": b["_id"], "vehicle_tail": b["vehicle_number"][-4:],
                    "start_time": b["start_time"], "expires_at": b["expires_at"],
                    "end_time": b["start_time"] + timedelta(hours=b["duration_hours"]),
                    "status": b["status"],
                },
            })
        out.append({"building_id": bid, "total": len(items),
                    "free": sum(1 for i in items if i["status"] == "FREE"), "slots": items})
    return ser(out)


async def esp32_compact_state():
    """Lightweight slots + active LCD gate OTP + gate trigger seq for ESP32."""
    t = now()
    all_slots = await d.slots.find(
        {}, {"building_id": 1, "slot_number": 1, "status": 1}
    ).sort([("building_id", 1), ("slot_number", 1)]).to_list(50)

    active_codes: dict[int, str] = {}
    async for b in d.bookings.find(
        {"status": "BOOKED", "gate_otp": {"$ne": None}, "gate_otp_expires_at": {"$gt": t}},
        {"building_id": 1, "gate_otp": 1},
    ).sort("gate_otp_requested_at", -1):
        bid = b["building_id"]
        if bid not in active_codes and b.get("gate_otp"):
            active_codes[bid] = b["gate_otp"]

    gate_ids = [f"gate_{bid}" for bid in d.BUILDINGS]
    gate_seqs = {
        int(doc["_id"].split("_")[1]): int(doc.get("seq", 0))
        async for doc in d.meta.find({"_id": {"$in": gate_ids}})
    }

    buildings = []
    for bid in d.BUILDINGS:
        statuses = [s["status"] for s in all_slots if s["building_id"] == bid]
        buildings.append({
            "building_id": bid,
            "free": sum(1 for st in statuses if st == "FREE"),
            "slots": statuses,
            "gate_code": active_codes.get(bid),
            "gate_seq": gate_seqs.get(bid, 0),
        })
    return {"ts": t.isoformat(), "buildings": buildings}


# ---------------------------------------------------------------- IR logic
async def apply_ir(building_id: int, slot_number: int, ir: bool) -> str:
    t = now()
    key = {"building_id": building_id, "slot_number": slot_number}
    current = await d.slots.find_one(key, {"status": 1})
    if not current:
        raise HTTPException(404, "Slot not found")

    if ir:
        before = await d.slots.find_one_and_update(
            {**key, "status": {"$in": ["FREE", "BOOKED"]}},
            {"$set": {"status": "OCCUPIED", "ir_occupied": True, "updated_at": t}},
            return_document=ReturnDocument.BEFORE,
        )
        if before and before.get("active_booking_id"):
            await d.bookings.update_one(
                {"_id": before["active_booking_id"], "status": "BOOKED"},
                {"$set": {"status": "CHECKED_IN", "checked_in_at": t,
                          "gate_otp": None, "gate_otp_expires_at": None}},
            )
        elif not before:
            await d.slots.update_one(key, {"$set": {"ir_occupied": True, "updated_at": t}})
        return "OCCUPIED"
    else:
        before = await d.slots.find_one_and_update(
            {**key, "status": "OCCUPIED"},
            {"$set": {"status": "FREE", "ir_occupied": False,
                      "active_booking_id": None, "updated_at": t}},
            return_document=ReturnDocument.BEFORE,
        )
        if before and before.get("active_booking_id"):
            await d.bookings.update_one(
                {"_id": before["active_booking_id"]},
                {"$set": {"status": "COMPLETED", "completed_at": t}},
            )
        elif not before:
            await d.slots.update_one(key, {"$set": {"ir_occupied": False, "updated_at": t}})
        return "FREE" if before else current["status"]


# ---------------------------------------------------------------- auth
@app.post("/api/auth/register", status_code=201)
async def register(r: RegisterIn):
    salt, h = auth.hash_pw(r.password)
    doc = {"_id": ObjectId(), "name": r.name, "email": r.email, "phone": r.phone,
           "salt": salt, "password_hash": h, "role": "user", "active": True, "created_at": now()}
    try:
        await d.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists")
    return {"token": auth.make_token(doc), "user": auth.public_user(doc)}


@app.post("/api/auth/login")
async def login(r: LoginIn):
    u = await d.users.find_one({"email": r.email})
    if not u or not auth.verify_pw(r.password, u["salt"], u["password_hash"]):
        raise HTTPException(401, "Wrong email or password")
    if not u.get("active", True):
        raise HTTPException(403, "This account is blocked. Contact the admin")
    return {"token": auth.make_token(u), "user": auth.public_user(u)}


@app.get("/api/auth/me")
async def me(u: dict = Depends(current_user)):
    return auth.public_user(u)


# ---------------------------------------------------------------- public + user API
@app.get("/api/buildings")
async def list_buildings():
    await expire_no_shows()
    return {"rate_per_hour": RATE_PER_HOUR, "simulator": SIMULATOR, "buildings": await building_view()}


@app.post("/api/bookings", status_code=201)
async def create_booking(b: BookingIn, request: Request, user: dict = Depends(current_user)):
    await expire_no_shows()
    t = now()
    start = t + timedelta(minutes=b.start_offset_minutes)
    booking_id = ObjectId()

    # Atomic: only one request can flip FREE -> BOOKED.
    slot = await d.slots.find_one_and_update(
        {"building_id": b.building_id, "slot_number": b.slot_number, "status": "FREE",
         "ir_occupied": False, "active_booking_id": None},
        {"$set": {"status": "BOOKED", "active_booking_id": booking_id, "updated_at": t}})
    if not slot:
        raise HTTPException(409, "This slot is no longer free. Pick another one.")

    amount = RATE_PER_HOUR * b.duration_hours
    txn_id = "TXN-" + uuid.uuid4().hex[:12].upper()
    ip = client_ip(request)
    txn = {
        "_id": ObjectId(), "txn_id": txn_id, "booking_id": booking_id, "user_id": user["_id"],
        "user_name": user["name"], "user_email": user["email"], "user_phone": user.get("phone"),
        "building_id": b.building_id, "slot_number": b.slot_number, "vehicle_number": b.vehicle_number,
        "amount": amount, "currency": "INR", "method": b.payment_method.value,
        "status": "PAID_DUMMY", "device_id": b.device.device_id,
        "device_info": b.device.model_dump(exclude={"device_id"}), "ip_address": ip,
        "created_at": t,
    }
    doc = {
        "_id": booking_id, "user_id": user["_id"], "building_id": b.building_id,
        "slot_number": b.slot_number, "user_name": user["name"], "vehicle_number": b.vehicle_number,
        "start_time": start, "duration_hours": b.duration_hours, "grace_minutes": b.grace_minutes,
        "expires_at": start + timedelta(minutes=b.grace_minutes), "status": "BOOKED",
        "dummy_payment": {"payment_id": txn_id, "method": b.payment_method.value, "amount": amount,
                          "status": "PAID_DUMMY", "timestamp": t},
        "device_id": b.device.device_id, "created_at": t,
    }
    try:
        await d.bookings.insert_one(doc)
        await d.transactions.insert_one(txn)
    except Exception:
        await d.bookings.delete_one({"_id": booking_id})
        await d.slots.update_one({"_id": slot["_id"], "active_booking_id": booking_id},
                                 {"$set": {"status": "FREE", "active_booking_id": None}})
        raise HTTPException(500, "Could not save booking")
    doc["end_time"] = start + timedelta(hours=b.duration_hours)
    return ser(doc)


@app.get("/api/me/bookings")
async def my_bookings(user: dict = Depends(current_user)):
    await expire_no_shows()
    t = now()
    rows = await d.bookings.find({"user_id": user["_id"]}).sort("created_at", -1).to_list(50)
    for r in rows:
        r["end_time"] = r["start_time"] + timedelta(hours=r["duration_hours"])
        exp = r.get("gate_otp_expires_at")
        r["otp_pending"] = bool(r.get("gate_otp") and exp and exp > t)
        r.pop("gate_otp", None)  # Keep OTP secret on backend + physical LCD only
    return ser(rows)


@app.post("/api/me/bookings/{booking_id}/arrived")
async def booking_arrived(booking_id: str, user: dict = Depends(current_user)):
    await expire_no_shows()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(404, "Booking not found")

    t = now()
    code = f"{secrets.randbelow(9000) + 1000}"
    otp_exp = t + timedelta(seconds=GATE_OTP_TTL_SECONDS)

    bk = await d.bookings.find_one_and_update(
        {"_id": oid, "user_id": user["_id"], "status": "BOOKED", "expires_at": {"$gt": t}},
        {"$set": {"gate_otp": code, "gate_otp_expires_at": otp_exp, "gate_otp_requested_at": t}},
    )
    if not bk:
        raise HTTPException(409, "Booking is no longer active or has expired")

    return {
        "ok": True,
        "building_id": bk["building_id"],
        "slot_number": bk["slot_number"],
        "otp_expires_at": otp_exp.isoformat(),
    }


@app.post("/api/me/bookings/{booking_id}/verify-gate")
async def verify_gate_otp(booking_id: str, body: GateVerifyIn, user: dict = Depends(current_user)):
    await expire_no_shows()
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(404, "Booking not found")

    t = now()
    bk = await d.bookings.find_one({"_id": oid, "user_id": user["_id"], "status": "BOOKED"})
    if not bk:
        raise HTTPException(409, "Booking is no longer active")

    expected = bk.get("gate_otp")
    exp = bk.get("gate_otp_expires_at")
    if not expected or not exp or exp <= t:
        raise HTTPException(400, "Gate code expired. Tap 'I'm Here' again for a new code.")

    if not secrets.compare_digest(expected, body.code):
        raise HTTPException(400, f"Wrong code. Check the LCD at Building {bk['building_id']}.")

    consumed = await d.bookings.find_one_and_update(
        {"_id": oid, "status": "BOOKED", "gate_otp": body.code},
        {"$set": {"gate_otp": None, "gate_otp_expires_at": None, "gate_verified_at": t}},
    )
    if not consumed:
        raise HTTPException(409, "Code already used or booking changed")

    seq = await trigger_building_gate(bk["building_id"])
    return {
        "ok": True,
        "building_id": bk["building_id"],
        "slot_number": bk["slot_number"],
        "gate_seq": seq,
    }


@app.post("/api/me/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str, user: dict = Depends(current_user)):
    try:
        oid = ObjectId(booking_id)
    except Exception:
        raise HTTPException(404, "Booking not found")
    t = now()
    bk = await d.bookings.find_one_and_update(
        {"_id": oid, "user_id": user["_id"], "status": "BOOKED"},
        {"$set": {"status": "CANCELLED", "cancelled_at": t,
                  "gate_otp": None, "gate_otp_expires_at": None}})
    if not bk:
        raise HTTPException(409, "Only bookings that haven't started parking can be cancelled")
    await d.slots.update_one({"active_booking_id": oid, "status": "BOOKED"},
                             {"$set": {"status": "FREE", "active_booking_id": None, "updated_at": t}})
    await d.transactions.update_one({"booking_id": oid}, {"$set": {"status": "REFUNDED_DUMMY", "refunded_at": t}})
    return {"ok": True}


# ---------------------------------------------------------------- admin API
@app.get("/api/admin/stats")
async def admin_stats(_: dict = Depends(admin_user)):
    await expire_no_shows()
    sl = {r["_id"]: r["n"] for r in await d.slots.aggregate(
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(10)}
    bk = {r["_id"]: r["n"] for r in await d.bookings.aggregate(
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(20)}
    rev = await d.transactions.aggregate([{"$match": {"status": "PAID_DUMMY"}},
                                          {"$group": {"_id": None, "total": {"$sum": "$amount"}, "n": {"$sum": 1}}}]).to_list(1)
    m = await d.meta.find_one({"_id": "esp32"})
    online = bool(m and (now() - m["last_seen"]).total_seconds() < 30)
    return ser({"slots": sl, "bookings": bk, "revenue": rev[0]["total"] if rev else 0,
                "paid_txns": rev[0]["n"] if rev else 0, "users": await d.users.count_documents({"role": "user"}),
                "esp32": {"online": online, "last_seen": m and m["last_seen"], "ip": m and m.get("ip")}})


@app.get("/api/admin/bookings")
async def admin_bookings(_: dict = Depends(admin_user)):
    await expire_no_shows()
    return ser(await d.bookings.find().sort("created_at", -1).to_list(200))


@app.get("/api/admin/transactions")
async def admin_txns(_: dict = Depends(admin_user)):
    return ser(await d.transactions.find().sort("created_at", -1).to_list(200))


@app.get("/api/admin/users")
async def admin_users(_: dict = Depends(admin_user)):
    rows = await d.users.find({}, {"salt": 0, "password_hash": 0}).sort("created_at", -1).to_list(200)
    return ser(rows)


@app.post("/api/admin/users/{user_id}/toggle")
async def toggle_user(user_id: str, admin: dict = Depends(admin_user)):
    try:
        oid = ObjectId(user_id)
    except Exception:
        raise HTTPException(404, "User not found")
    if oid == admin["_id"]:
        raise HTTPException(400, "You can't block yourself")
    u = await d.users.find_one({"_id": oid})
    if not u:
        raise HTTPException(404, "User not found")
    await d.users.update_one({"_id": oid}, {"$set": {"active": not u.get("active", True)}})
    return {"active": not u.get("active", True)}


@app.post("/api/admin/slots/{building_id}/{slot_number}/release")
async def admin_release(building_id: int, slot_number: int, _: dict = Depends(admin_user)):
    t = now()
    slot = await d.slots.find_one_and_update(
        {"building_id": building_id, "slot_number": slot_number},
        {"$set": {"status": "FREE", "ir_occupied": False, "active_booking_id": None, "updated_at": t}},
        return_document=ReturnDocument.BEFORE)
    if not slot:
        raise HTTPException(404, "Slot not found")
    bid = slot.get("active_booking_id")
    bk = bid and await d.bookings.find_one({"_id": bid})
    if bk and bk["status"] == "BOOKED":
        await d.bookings.update_one({"_id": bid}, {"$set": {"status": "CANCELLED_BY_ADMIN", "cancelled_at": t,
                                                            "gate_otp": None, "gate_otp_expires_at": None}})
        await d.transactions.update_one({"booking_id": bid}, {"$set": {"status": "REFUNDED_DUMMY", "refunded_at": t}})
    elif bk and bk["status"] == "CHECKED_IN":
        await d.bookings.update_one({"_id": bid}, {"$set": {"status": "COMPLETED", "completed_at": t}})
    return {"status": "FREE"}


@app.post("/api/admin/buildings/{building_id}/open-gate")
async def admin_open_gate(building_id: int, _: dict = Depends(admin_user)):
    if building_id not in d.BUILDINGS:
        raise HTTPException(404, "Building not found")
    seq = await trigger_building_gate(building_id)
    return {"ok": True, "building_id": building_id, "gate_seq": seq}


@app.post("/api/simulate/ir")
async def simulate_ir(r: SlotReading, _: dict = Depends(admin_user)):
    if not SIMULATOR:
        raise HTTPException(403, "Simulator disabled")
    await expire_no_shows()
    return {"status": await apply_ir(r.building_id, r.slot_number, r.ir_occupied)}


# ---------------------------------------------------------------- ESP32 API
async def heartbeat(request: Request):
    await d.meta.update_one({"_id": "esp32"}, {"$set": {"last_seen": now(), "ip": client_ip(request)}}, upsert=True)


@app.post("/api/esp32/telemetry", dependencies=[Depends(require_key)])
async def telemetry(body: TelemetryIn, request: Request):
    await heartbeat(request)
    await expire_no_shows()

    all_slots = await d.slots.find(
        {}, {"building_id": 1, "slot_number": 1, "status": 1, "ir_occupied": 1}
    ).to_list(50)
    slot_map = {(s["building_id"], s["slot_number"]): s for s in all_slots}

    updated = []
    for r in body.slots:
        curr = slot_map.get((r.building_id, r.slot_number))
        if not curr:
            st = await apply_ir(r.building_id, r.slot_number, r.ir_occupied)
        else:
            curr_ir = bool(curr.get("ir_occupied", False))
            curr_status = curr.get("status", "FREE")
            changed = (
                (curr_ir != r.ir_occupied)
                or (r.ir_occupied and curr_status in ["FREE", "BOOKED"])
                or (not r.ir_occupied and curr_status == "OCCUPIED")
            )
            if changed:
                st = await apply_ir(r.building_id, r.slot_number, r.ir_occupied)
            else:
                st = curr_status
        updated.append({"building_id": r.building_id, "slot_number": r.slot_number, "status": st})

    return {"updated": updated, **(await esp32_compact_state())}


@app.get("/api/esp32/state", dependencies=[Depends(require_key)])
async def esp32_state(request: Request):
    await heartbeat(request)
    await expire_no_shows()
    return await esp32_compact_state()


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/healthz")
async def health():
    return {"ok": True}
