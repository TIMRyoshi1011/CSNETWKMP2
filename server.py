# server.py
"""
LSNP Central Server
Handles peer registration, message routing, and heartbeats
"""
import socket
import threading
import time
import json
import sys
from datetime import datetime
from typing import Dict, Any

# =============================
# Configuration
# =============================
SERVER_IP = "127.0.0.1"
UDP_PORT = 50999
BUFFER_SIZE = 8192
HEARTBEAT_TIMEOUT = 10  # seconds
CLEANUP_INTERVAL = 5

# =============================
# Global State
# =============================
clients: Dict[str, dict] = {}  # user_id -> {ip, port, name, last_seen}
lock = threading.Lock()

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg, level="INFO"):
    print(f"[{timestamp()}] {level} | {msg}")

def send_udp(data: str, ip: str, port: int):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if not data.endswith("\n\n"):
            data += "\n\n"
        sock.sendto(data.encode('utf-8'), (ip, port))
        sock.close()
    except Exception as e:
        log(f"Send failed to {ip}:{port} - {e}", level="ERROR")

def cleanup_loop():
    """Remove inactive clients"""
    while True:
        now = time.time()
        removed = []
        with lock:
            for uid, info in list(clients.items()):
                if now - info["last_seen"] > HEARTBEAT_TIMEOUT:
                    removed.append(uid)
                    del clients[uid]
        for uid in removed:
            log(f"Client timed out: {uid}")
        time.sleep(CLEANUP_INTERVAL)

def handle_message(data: bytes, addr):
    ip, port = addr
    try:
        raw = data.decode('utf-8', errors='ignore').strip()
        if not raw.endswith("\n\n"):
            raw += "\n\n"
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        headers = {}
        for line in lines:
            if ':' in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()

        msg_type = headers.get("TYPE")
        sender_id = headers.get("USER_ID")

        if not msg_type or not sender_id:
            return

        with lock:
            if msg_type == "REGISTER":
                clients[sender_id] = {
                    "ip": ip,
                    "port": port,
                    "name": headers.get("DISPLAY_NAME", "Anonymous"),
                    "last_seen": time.time()
                }
                log(f"✅ Registered: {sender_id}")
                # Send welcome + peer list
                peer_list = {k: v["name"] for k, v in clients.items() if k != sender_id}
                welcome = (
                    f"TYPE: WELCOME\n"
                    f"PEERS: {json.dumps(peer_list)}"
                )
                send_udp(welcome, ip, port)

                # Notify others
                notify = (
                    f"TYPE: PEER_JOINED\n"
                    f"USER_ID: {sender_id}\n"
                    f"DISPLAY_NAME: {headers['DISPLAY_NAME']}"
                )
                for uid, info in clients.items():
                    if uid != sender_id:
                        send_udp(notify, info["ip"], info["port"])

            elif msg_type == "HEARTBEAT":
                if sender_id in clients:
                    clients[sender_id]["last_seen"] = time.time()
                    clients[sender_id]["ip"] = ip
                    clients[sender_id]["port"] = port

            else:
                # Forward message
                if msg_type == "DM":
                    target = headers.get("TO")
                    if target in clients:
                        forward_message(headers, target)
                else:
                    # Broadcast to all except sender
                    for uid, info in clients.items():
                        if uid != sender_id:
                            send_udp(raw, info["ip"], info["port"])

    except Exception as e:
        log(f"Error handling packet: {e}", level="ERROR")

def forward_message(headers: Dict[str, Any], target: str):
    msg = "\n".join([f"{k}: {v}" for k, v in headers.items()])
    with lock:
        if target in clients:
            dest = clients[target]
            send_udp(msg, dest["ip"], dest["port"])

def listen_loop():
    global sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_IP, UDP_PORT))
    log(f"📡 Server listening on {SERVER_IP}:{UDP_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            threading.Thread(target=handle_message, args=(data, addr), daemon=True).start()
        except Exception as e:
            log(f"Listen error: {e}", level="ERROR")

if __name__ == "__main__":
    log("🚀 Starting LSNP Server...")
    threading.Thread(target=cleanup_loop, daemon=True).start()
    listen_loop()