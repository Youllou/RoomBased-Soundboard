from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Set, Optional
import json
import os
import sqlite3
import uuid
from pathlib import Path
from contextlib import contextmanager
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create sounds directory if it doesn't exist
SOUNDS_DIR = Path("sounds")
SOUNDS_DIR.mkdir(exist_ok=True)

# Database setup
DB_PATH = "soundboard.db"

def init_db():
    """Initialize the SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS sounds (
                                                         id TEXT PRIMARY KEY,
                                                         name TEXT NOT NULL,
                                                         filename TEXT NOT NULL,
                                                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )
                   """)
    conn.commit()
    conn.close()

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# Initialize database on startup
init_db()

# Store active connections per room
rooms: Dict[str, Set[WebSocket]] = {}

# Security
security = HTTPBearer()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

if not ADMIN_TOKEN:
    raise ValueError("ADMIN_TOKEN environment variable is not set")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify the bearer token"""
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

@app.get("/api/sounds")
async def get_sounds():
    """Return all sounds metadata from database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, filename FROM sounds ORDER BY created_at DESC")
        sounds = [dict(row) for row in cursor.fetchall()]

    return {"sounds": sounds}

@app.post("/api/sounds")
async def upload_sound(
        name: str = Form(...),
        file: UploadFile = File(...),
        token: str = Depends(verify_token)
):
    """Upload a new sound file (requires authentication)"""

    # Validate file type
    if not file.content_type or not file.content_type.startswith('audio/'):
        raise HTTPException(status_code=400, detail="File must be an audio file")

    # Get file extension
    file_ext = Path(file.filename).suffix
    if not file_ext:
        file_ext = '.mp3'  # Default to mp3 if no extension

    # Generate unique ID and filename
    sound_id = str(uuid.uuid4())
    filename = f"{sound_id}{file_ext}"
    file_path = SOUNDS_DIR / filename

    # Save file to disk
    try:
        contents = await file.read()
        with open(file_path, 'wb') as f:
            f.write(contents)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

    # Save metadata to database
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sounds (id, name, filename) VALUES (?, ?, ?)",
                (sound_id, name, filename)
            )
            conn.commit()
    except Exception as e:
        # Clean up file if database insert fails
        if file_path.exists():
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=f"Failed to save to database: {str(e)}")

    return {
        "id": sound_id,
        "name": name,
        "filename": filename,
        "message": "Sound uploaded successfully"
    }

@app.delete("/api/sounds/{sound_id}")
async def delete_sound(sound_id: str, token: str = Depends(verify_token)):
    """Delete a sound (requires authentication)"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Get sound info
        cursor.execute("SELECT filename FROM sounds WHERE id = ?", (sound_id,))
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Sound not found")

        filename = row['filename']

        # Delete from database
        cursor.execute("DELETE FROM sounds WHERE id = ?", (sound_id,))
        conn.commit()

        # Delete file from disk
        file_path = SOUNDS_DIR / filename
        if file_path.exists():
            os.remove(file_path)

    return {"message": "Sound deleted successfully"}

@app.get("/api/sounds/{sound_id}/audio")
async def get_sound_audio(sound_id: str):
    """Serve the actual audio file for a specific sound"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM sounds WHERE id = ?", (sound_id,))
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Sound not found")

    file_path = SOUNDS_DIR / row['filename']

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600"
        }
    )

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()

    # Add connection to room
    if room_id not in rooms:
        rooms[room_id] = set()
    rooms[room_id].add(websocket)

    # Notify about user count
    await broadcast_to_room(room_id, {
        "type": "user_count",
        "count": len(rooms[room_id])
    })

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            # Broadcast sound to all users in room
            if message.get("type") == "play_sound":
                await broadcast_to_room(room_id, message, exclude=websocket)

    except WebSocketDisconnect:
        rooms[room_id].remove(websocket)
        if len(rooms[room_id]) == 0:
            del rooms[room_id]
        else:
            await broadcast_to_room(room_id, {
                "type": "user_count",
                "count": len(rooms[room_id])
            })

async def broadcast_to_room(room_id: str, message: dict, exclude: WebSocket = None):
    if room_id in rooms:
        for connection in rooms[room_id]:
            if connection != exclude:
                try:
                    await connection.send_text(json.dumps(message))
                except:
                    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
