from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Optional
import asyncio
import json
import os
import uuid
from pathlib import Path
from datetime import datetime

import asyncpg
import aioboto3
from botocore.exceptions import ClientError
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/soundboard")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")  # MinIO endpoint
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "soundboard-sounds")
S3_REGION = os.getenv("S3_REGION", "us-east-1")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    raise ValueError("ADMIN_TOKEN environment variable is not set")

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

# ---------------------------------------------------------------------------
# Globals set up on startup
# ---------------------------------------------------------------------------

db_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[redis.Redis] = None
pubsub_task: Optional[asyncio.Task] = None
s3_session = aioboto3.Session()

# Local (per-pod) websocket connections: room_id -> {conn_id: WebSocket}
rooms: Dict[str, Dict[str, WebSocket]] = {}


def get_s3_client():
    return s3_session.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
    )


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sounds (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def init_bucket():
    async with get_s3_client() as s3:
        try:
            await s3.head_bucket(Bucket=S3_BUCKET)
        except ClientError:
            await s3.create_bucket(Bucket=S3_BUCKET)


async def redis_listener():
    """Runs on every pod: fans out published events to LOCAL websocket connections."""
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("room:*")
    async for message in pubsub.listen():
        if message["type"] != "pmessage":
            continue
        room_id = message["channel"].split(":", 1)[1]
        try:
            payload = json.loads(message["data"])
        except (TypeError, ValueError):
            continue

        data = payload["message"]
        exclude_conn_id = payload.get("exclude_conn_id")

        connections = rooms.get(room_id, {})
        dead = []
        for conn_id, ws in connections.items():
            if conn_id == exclude_conn_id:
                continue
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            connections.pop(conn_id, None)


async def publish_to_room(room_id: str, message: dict, exclude_conn_id: Optional[str] = None):
    await redis_client.publish(
        f"room:{room_id}",
        json.dumps({"message": message, "exclude_conn_id": exclude_conn_id}),
    )


@app.on_event("startup")
async def on_startup():
    global redis_client, pubsub_task
    await init_db()
    await init_bucket()
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    pubsub_task = asyncio.create_task(redis_listener())


@app.on_event("shutdown")
async def on_shutdown():
    if pubsub_task:
        pubsub_task.cancel()
    if redis_client:
        await redis_client.close()
    if db_pool:
        await db_pool.close()


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/sounds")
async def get_sounds():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, filename FROM sounds ORDER BY created_at DESC"
        )
    return {"sounds": [dict(r) for r in rows]}


@app.post("/api/sounds")
async def upload_sound(
    name: str = Form(...),
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
):
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")

    file_ext = Path(file.filename).suffix or ".mp3"
    sound_id = str(uuid.uuid4())
    filename = f"{sound_id}{file_ext}"

    contents = await file.read()

    # Upload to S3/MinIO first
    try:
        async with get_s3_client() as s3:
            await s3.put_object(
                Bucket=S3_BUCKET,
                Key=filename,
                Body=contents,
                ContentType=file.content_type,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload to storage: {str(e)}")

    # Then save metadata to Postgres
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sounds (id, name, filename, created_at) VALUES ($1, $2, $3, $4)",
                sound_id, name, filename, datetime.utcnow(),
            )
    except Exception as e:
        # Roll back the upload if DB insert fails
        async with get_s3_client() as s3:
            await s3.delete_object(Bucket=S3_BUCKET, Key=filename)
        raise HTTPException(status_code=500, detail=f"Failed to save to database: {str(e)}")

    return {
        "id": sound_id,
        "name": name,
        "filename": filename,
        "message": "Sound uploaded successfully",
    }


@app.delete("/api/sounds/{sound_id}")
async def delete_sound(sound_id: str, token: str = Depends(verify_token)):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT filename FROM sounds WHERE id = $1", sound_id)
        if not row:
            raise HTTPException(status_code=404, detail="Sound not found")

        await conn.execute("DELETE FROM sounds WHERE id = $1", sound_id)

    async with get_s3_client() as s3:
        await s3.delete_object(Bucket=S3_BUCKET, Key=row["filename"])

    return {"message": "Sound deleted successfully"}


@app.get("/api/sounds/{sound_id}/audio")
async def get_sound_audio(sound_id: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT filename FROM sounds WHERE id = $1", sound_id)
    if not row:
        raise HTTPException(status_code=404, detail="Sound not found")

    try:
        async with get_s3_client() as s3:
            obj = await s3.get_object(Bucket=S3_BUCKET, Key=row["filename"])
            body = await obj["Body"].read()
    except ClientError:
        raise HTTPException(status_code=404, detail="Audio file not found in storage")

    return StreamingResponse(
        iter([body]),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        },
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    conn_id = str(uuid.uuid4())

    rooms.setdefault(room_id, {})[conn_id] = websocket

    await redis_client.sadd(f"room_users:{room_id}", conn_id)
    count = await redis_client.scard(f"room_users:{room_id}")
    await publish_to_room(room_id, {"type": "user_count", "count": count})

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message.get("type") == "play_sound":
                await publish_to_room(room_id, message, exclude_conn_id=conn_id)

    except WebSocketDisconnect:
        rooms[room_id].pop(conn_id, None)
        if not rooms[room_id]:
            del rooms[room_id]

        await redis_client.srem(f"room_users:{room_id}", conn_id)
        count = await redis_client.scard(f"room_users:{room_id}")
        await publish_to_room(room_id, {"type": "user_count", "count": count})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
