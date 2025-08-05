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
HEARTBEAT_TIMEOUT = 60  # seconds
CLEANUP_INTERVAL = 60

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

def validate_token(token: str, sender_id: str) -> bool:
    try:
        parts = token.split('|')
        if len(parts) != 3:
            log(f"Invalid token format: {token}", level="WARNING")
            return False
        token_user_id = parts[0]
        token_expiry = int(parts[1])
        token_scope = parts[2]

        if token_user_id != sender_id:
            log(f"Token user ID mismatch: Expected {sender_id}, Got {token_user_id}", level="WARNING")
            return False
        if time.time() > token_expiry:
            log(f"Token expired for {sender_id}", level="WARNING")
            return False
        if token_scope != "chat": # Ensure token is for chat
            log(f"Invalid token scope: {token_scope}", level="WARNING")
            return False
        return True
    except Exception as e:
        log(f"Error validating token: {e}", level="ERROR")
        return False

def handle_message(data: bytes, addr):
    ip, port = addr
    # log(f"🔍 RAW MESSAGE RECEIVED from {addr}: {data}")
    try:
        raw = data.decode('utf-8', errors='ignore').strip()
        if not raw.endswith("\n\n"):
            raw += "\n\n"
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        
        #for debug:
        headers = {}
        for line in lines:
            if ':' in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()

        msg_type = headers.get("TYPE")
        sender_id_full = headers.get("USER_ID") or headers.get("FROM")
        sender_id = (sender_id_full or "").split("@")[0]
        
        log(f"🔍 DECODED MESSAGE: {raw}") # DEBUG
        # if not sender_id and headers.get("FROM"): # for follow (it doesnt hve user_id field)
        #     sender_id = headers.get("FROM").split("@")[0]
            
        if not msg_type or not sender_id:
            log(f"❌ Missing TYPE or SENDER_ID  | Headers: {headers}") 
            return
        
        messages_to_send = []
        
        log(f"2nd DECODED MESSAGE: {raw}") # DEBUG
        
        with lock:
            if msg_type == "REGISTER":
                clients[sender_id] = {
                    "ip": ip,
                    "port": port,
                    "name": headers.get("DISPLAY_NAME", "Anonymous"),
                    "status": headers.get("STATUS", ""),
                    "last_seen": time.time(),
                    "follows": [], "followers": []   
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
            # Forward message
            elif msg_type == "DM":
                target = headers.get("TO")
                if target in clients:
                    dest = clients[target]
                    send_udp(raw, dest["ip"], dest["port"])
                    log(f"📤 DM forwarded from {sender_id} to {target}")
                else:
                    log(f"❌ DM target {target} not found")
            ##
            elif msg_type == "POST":
                sender = clients.get(sender_id)
                if not sender:
                    return
                token = headers.get("TOKEN", "")
                ttl = int(headers.get("TTL", 3600))
                message_id = headers.get("MESSAGE_ID")
                try:
                    # Validate token
                    parts = token.split("|")
                    if len(parts) != 3 or parts[0] != sender_id or parts[2] != "broadcast":
                        return
                    timestamp = int(parts[1]) - ttl
                    if time.time() > timestamp + ttl:
                        return  # expired token
                except:
                    return
                # Broadcast to followers only
                for uid, info in clients.items():
                    if sender_id in info.get("follows", []):
                        send_udp(raw, info["ip"], info["port"])
                        
            elif msg_type == "FOLLOW":
                target = headers.get("TO") #error validation on who user wants to follow
                log(f"🔍 FOLLOW request: {sender_id} wants to follow {target}")
                
                if not target or target == sender_id:
                    return
                if target not in clients:
                    log(f"❌ Target {target} not online")
                    return
                
                #store new following
                if target not in clients[sender_id]["follows"]:
                    clients[sender_id]["follows"].append(target)
                    clients[target]["followers"].append(sender_id)
                    log(f"✅ {sender_id} now follows {target}")
                    
                    # log(f"📊 {target} has followers: {clients[target]['followers']}")

                    notify = (
                        f"TYPE: FOLLOW_NOTIFY\n"
                        f"USER_ID: {sender_id}\n"
                        f"DISPLAY_NAME: {clients[sender_id]['name']}"
                    )
                    log(f"📤 Sending FOLLOW_NOTIFY to {target}") 
                    send_udp(notify, clients[target]["ip"], clients[target]["port"])
                
            elif msg_type == "UNFOLLOW":
                target = headers.get("TO")
                log(f"🔍 UNFOLLOW request: {sender_id} wants to unfollow {target}")

                if not target or target == sender_id:
                    return
                if sender_id not in clients or target not in clients:
                    log(f"❌ Invalid UNFOLLOW request: Sender {sender_id} or target {target} not online")
                    return
                
                #store the previous state before modification for logging/notification
                unfollowed_name = clients[target]["name"]

                # check if following first
                if target in clients[sender_id]["follows"]:
                    clients[sender_id]["follows"].remove(target)
                    clients[target]["followers"].remove(sender_id)
                    log(f"✅ {sender_id} unfollowed {target}")

                    notify = (
                        f"TYPE: UNFOLLOW_NOTIFY\n"
                        f"USER_ID: {sender_id}\n"
                        f"DISPLAY_NAME: {clients[sender_id]['name']}"
                    )
                    send_udp(notify, clients[target]["ip"], clients[target]["port"])
                    
            elif msg_type == "PROFILE":
                log(f"Received PROFILE message from {sender_id}. Updating info...")
                display_name = headers.get("DISPLAY_NAME")
                status = headers.get("STATUS")
                
                # Update server's client registry with new info
                if sender_id in clients:
                    clients[sender_id]["name"] = display_name
                    clients[sender_id]["status"] = status
                    clients[sender_id]["last_seen"] = time.time()
                    log(f"📝 Profile update from {sender_id}: '{status}'")

                # Broadcast the profile message to all other clients
                for uid, info in clients.items():
                    send_udp(raw, info["ip"], info["port"])

            elif msg_type == "TICTACTOE_INVITE":
                gameid = headers.get("GAMEID")
                player_x = headers.get("PLAYER_X", sender_id)
                player_o = headers.get("PLAYER_O")
                symbol = headers.get("SYMBOL", "X")
                target = headers.get("TO")

                if not gameid or not target:
                    log(f"❌ Invalid TICTACTOE_INVITE from {sender_id} (missing GAMEID or TO)")
                    return

                # Rebuild invite message cleanly
                invite_msg = (
                    f"TYPE: TICTACTOE_INVITE\n"
                    f"GAMEID: {gameid}\n"
                    f"PLAYER_X: {player_x}\n"
                    f"PLAYER_O: {player_o or 'waiting'}\n"
                    f"SYMBOL: {symbol}\n"
                    f"FROM: {sender_id}@{ip}\n"
                    f"TO: {target}"
                )

                if target in clients:
                    send_udp(invite_msg, clients[target]["ip"], clients[target]["port"])
                    log(f"📤 TICTACTOE_INVITE sent from {sender_id} to {target}")
                else:
                    log(f"❌ TICTACTOE_INVITE target {target} not found")
                        
            else:
                # Broadcast other message types
                for uid, info in clients.items():
                    if uid != sender_id:
                        send_udp(raw, info["ip"], info["port"])
                        
        # Perform all network I/O *after* the lock has been released
        for msg, dest_ip, dest_port in messages_to_send:
            send_udp(msg, dest_ip, dest_port)
    except Exception as e:
        log(f"Error handling packet: {e}", level="ERROR")

def forward_message(headers: Dict[str, Any], target: str):
    msg_lines = []
    for k, v in headers.items():
        msg_lines.append(f"{k}: {v}")
    msg = "\n".join(msg_lines)

    with lock:
        if target in clients:
            dest = clients[target]
            send_udp(msg, dest["ip"], dest["port"])
        else:
            log(f"Attempted to forward message to unknown target {target}", level="WARNING")

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
