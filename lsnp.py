#!/usr/bin/env python3
"""
Lightweight Social Networking Protocol (LSNP)
Implementation with UDP, Discovery, Messaging, File Transfer, and Games
"""

import socket
import threading
import time
import json
import os
import base64
import sys
import argparse
import random
from datetime import datetime
from typing import Dict, List, Optional, Any

# =============================
# Configuration
# =============================

UDP_PORT = 50999
BROADCAST_INTERVAL = 300  # seconds
MAX_RETRIES = 3
ACK_TIMEOUT = 2
LOSS_RATE = 0.2  # 20% packet loss in test mode
BUFFER_SIZE = 8192
VERBOSE = False
TEST_LOSS = False

# =============================
# Global State
# =============================

running = True
sock = None
message_queue = []  # Thread-safe via locks
message_lock = threading.Lock()

# Local user config (load from file or args)
USER_ID = "user_" + str(random.randint(1000, 9999))
DISPLAY_NAME = "Anonymous"
AVATAR = "https://example.com/avatar.png"

# Data stores
peers = {}  # user_id -> {ip, display_name, avatar, last_seen}
tokens = {}  # token -> {scope, expiry, revoked}
outbox = {}  # msg_id -> {msg, dest_ip, retries, sent_time, acked}
games = {}  # game_id -> {player_x, player_o, board, turn, moves, status}
groups = {}  # group_id -> {name, members}
received_chunks = {}  # fileid -> {total, chunks: [data], sender}
log_file = open("peer_log.txt", "a", buffering=1)

# =============================
# Utilities
# =============================

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg, level="INFO"):
    t = timestamp()
    entry = f"[{t}] {level} | {msg}"
    print(entry)
    log_file.write(entry + "\n")

def is_valid_utf8(s):
    try:
        s.encode('utf-8').decode('utf-8')
        return True
    except:
        return False

def extract_field(msg: str, key: str) -> Optional[str]:
    for line in msg.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None

def generate_msgid():
    return f"msg_{int(time.time() * 1000)}_{random.randint(100, 999)}"

def generate_fileid():
    return f"file_{random.randint(10000, 99999)}"

def send_udp(data: str, ip: str, port: int = UDP_PORT):
    if TEST_LOSS and random.random() < LOSS_RATE:
        log(f"[LOSS] Dropped packet to {ip}: {data[:50]}...", level="DEBUG")
        return
    try:
        sock.sendto(data.encode('utf-8'), (ip, port))
    except Exception as e:
        log(f"Send failed to {ip}: {e}", level="ERROR")

# =============================
# Token System
# =============================

def create_token(scope: str, duration: int = 3600):
    token = f"tkn_{scope}_{random.randint(1000, 9999)}"
    tokens[token] = {
        "scope": scope,
        "expiry": time.time() + duration,
        "revoked": False
    }
    return token

def validate_token(token: str, required_scope: str) -> bool:
    if token not in tokens:
        return False
    t = tokens[token]
    if t["revoked"] or t["expiry"] < time.time():
        return False
    return t["scope"] == required_scope

# =============================
# Peer Discovery (Simulated mDNS)
# =============================

def broadcast_profile():
    while running:
        msg = (
            f"TYPE: PROFILE\n"
            f"USER_ID: {USER_ID}\n"
            f"DISPLAY_NAME: {DISPLAY_NAME}\n"
            f"AVATAR: {AVATAR}\n\n"
        )
        send_udp(msg, '<broadcast>')
        log(f"Broadcasted PROFILE", level="DEBUG")
        time.sleep(BROADCAST_INTERVAL)

def handle_profile(msg: str, sender_ip: str):
    user_id = extract_field(msg, "USER_ID")
    if not user_id:
        return
    display_name = extract_field(msg, "DISPLAY_NAME") or "Unknown"
    avatar = extract_field(msg, "AVATAR") or ""

    if user_id in peers:
        # Update if changed
        if (peers[user_id]["avatar"] != avatar or
            peers[user_id]["display_name"] != display_name):
            log(f"Updated peer: {user_id}")
        peers[user_id]["last_seen"] = time.time()
        peers[user_id]["ip"] = sender_ip
    else:
        peers[user_id] = {
            "ip": sender_ip,
            "display_name": display_name,
            "avatar": avatar,
            "last_seen": time.time()
        }
        log(f"Discovered peer: {user_id} @ {sender_ip}")

def handle_ping(msg: str, sender_ip: str, sender_port: int):
    # Reply with PONG
    pong = f"TYPE: PONG\nUSER_ID: {USER_ID}\n\n"
    send_udp(pong, sender_ip, sender_port)

# =============================
# ACK & Retry System
# =============================

def send_with_retry(msg: str, dest_ip: str, msg_id: str, scope: str):
    token = create_token(scope)
    msg = msg.replace("\n\n", f"TOKEN: {token}\nMSGID: {msg_id}\n\n")
    retries = 0
    while retries < MAX_RETRIES and running:
        send_udp(msg, dest_ip)
        log(f"Sent {msg_id} to {dest_ip} (retry {retries})", level="DEBUG")
        start = time.time()
        while time.time() - start < ACK_TIMEOUT:
            with message_lock:
                if msg_id in [m.get("ack_for") for m in message_queue if m.get("type") == "ACK"]:
                    log(f"ACK received for {msg_id}", level="DEBUG")
                    return True
            time.sleep(0.1)
        retries += 1
    log(f"Failed to send {msg_id} after {MAX_RETRIES} retries", level="ERROR")
    return False

def send_ack(msgid: str, dest_ip: str):
    ack = f"TYPE: ACK\nMSGID: {msgid}\n\n"
    send_udp(ack, dest_ip)
    log(f"Sent ACK for {msgid}", level="DEBUG")

# =============================
# Core Messaging
# =============================

def send_post(message: str, image: str = ""):
    msg = f"TYPE: POST\nMESSAGE: {message}"
    if image:
        msg += f"\nIMAGE: {image}"
    msg += "\n\n"
    msg_id = generate_msgid()
    for peer_id, info in peers.items():
        send_with_retry(msg, info["ip"], msg_id, "chat")

def send_dm(to_user: str, message: str):
    if to_user not in peers:
        log(f"User {to_user} not found", level="ERROR")
        return
    msg = f"TYPE: DM\nTO: {to_user}\nMESSAGE: {message}\n\n"
    msg_id = generate_msgid()
    send_with_retry(msg, peers[to_user]["ip"], msg_id, "dm")

def send_follow(target: str):
    if target not in peers:
        log(f"User {target} not found", level="ERROR")
        return
    msg = f"TYPE: FOLLOW\nTARGET_USER: {target}\n\n"
    msg_id = generate_msgid()
    send_with_retry(msg, peers[target]["ip"], msg_id, "follow")

def toggle_like(post_id: str):
    msg = f"TYPE: LIKE\nPOST_ID: {post_id}\n\n"
    msg_id = generate_msgid()
    for peer_id, info in peers.items():
        send_with_retry(msg, info["ip"], msg_id, "like")

# =============================
# File Transfer
# =============================

def send_file(to_user: str, filepath: str):
    if to_user not in peers:
        log(f"User {to_user} not found", level="ERROR")
        return
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    fileid = generate_fileid()

    offer = (
        f"TYPE: FILE_OFFER\n"
        f"FILENAME: {filename}\n"
        f"FILESIZE: {filesize}\n"
        f"FILEID: {fileid}\n"
        f"TO: {to_user}\n\n"
    )
    msg_id = generate_msgid()
    if send_with_retry(offer, peers[to_user]["ip"], msg_id, "file"):
        log(f"File offer sent: {filename}")
        # Start sending chunks after ACK
        threading.Thread(target=send_file_chunks, args=(filepath, fileid, peers[to_user]["ip"]), daemon=True).start()

def send_file_chunks(filepath: str, fileid: str, dest_ip: str):
    with open(filepath, "rb") as f:
        data = f.read()
    chunks = [data[i:i+1024] for i in range(0, len(data), 1024)]
    for idx, chunk in enumerate(chunks):
        b64_data = base64.b64encode(chunk).decode('utf-8')
        msg = (
            f"TYPE: FILE_CHUNK\n"
            f"FILEID: {fileid}\n"
            f"CHUNK_NUM: {idx+1}\n"
            f"TOTAL_CHUNKS: {len(chunks)}\n"
            f"DATA: {b64_data}\n\n"
        )
        chunk_id = f"chunk_{fileid}_{idx}"
        send_with_retry(msg, dest_ip, chunk_id, "file")

def handle_file_chunk(msg: str, sender_ip: str):
    fileid = extract_field(msg, "FILEID")
    chunk_num = int(extract_field(msg, "CHUNK_NUM"))
    total_chunks = int(extract_field(msg, "TOTAL_CHUNKS"))
    data_b64 = extract_field(msg, "DATA")
    try:
        data = base64.b64decode(data_b64)
    except:
        log("Invalid Base64 in chunk", level="ERROR")
        return

    if fileid not in received_chunks:
        received_chunks[fileid] = {
            "total": total_chunks,
            "chunks": [None] * total_chunks,
            "sender": sender_ip
        }

    if received_chunks[fileid]["chunks"][chunk_num - 1] is None:
        received_chunks[fileid]["chunks"][chunk_num - 1] = data
        log(f"Received chunk {chunk_num}/{total_chunks} of {fileid}")

    # Check if complete
    if all(received_chunks[fileid]["chunks"]):
        final_data = b''.join(received_chunks[fileid]["chunks"])
        filename = f"received/received_{fileid}"
        os.makedirs("received", exist_ok=True)
        with open(filename, "wb") as f:
            f.write(final_data)
        log(f"File saved: {filename}")
        # Send FILE_RECEIVED
        confirm = f"TYPE: FILE_RECEIVED\nFILEID: {fileid}\n\n"
        send_udp(confirm, received_chunks[fileid]["sender"])
        del received_chunks[fileid]

# =============================
# Tic Tac Toe
# =============================

def render_grid(gameid: str):
    if gameid not in games:
        return
    g = games[gameid]
    board = g["board"]
    symbols = {None: " ", "X": "X", "O": "O"}
    print("\n" + "="*20)
    print(f"🎮 Tic Tac Toe - Game: {gameid}")
    print(f"X: {g['player_x']} | O: {g['player_o']}")
    print("   0   1   2 ")
    for i in range(3):
        print(f"{i}  {symbols[board[i][0]]} | {symbols[board[i][1]]} | {symbols[board[i][2]]}")
        if i < 2: print("  -----------")
    print("="*20 + "\n")

def start_game(invite_to: str):
    if invite_to not in peers:
        log(f"User {invite_to} not found", level="ERROR")
        return
    gameid = f"game_{random.randint(1000, 9999)}"
    games[gameid] = {
        "player_x": USER_ID,
        "player_o": invite_to,
        "board": [[None]*3 for _ in range(3)],
        "turn": 1,
        "moves": [],
        "status": "active"
    }
    msg = f"TYPE: TICTACTOE_INVITE\nGAMEID: {gameid}\nPLAYER_X: {USER_ID}\nPLAYER_O: {invite_to}\n\n"
    msg_id = generate_msgid()
    send_with_retry(msg, peers[invite_to]["ip"], msg_id, "game")
    log(f"Invited {invite_to} to game {gameid}")

def make_move(gameid: str, pos: int):
    if gameid not in games:
        log("Game not found", level="ERROR")
        return
    g = games[gameid]
    if g["status"] != "active":
        log("Game over", level="ERROR")
        return
    row, col = pos // 3, pos % 3
    if g["board"][row][col] is not None:
        log("Invalid move", level="ERROR")
        return

    symbol = "X" if len(g["moves"]) % 2 == 0 else "O"
    g["board"][row][col] = symbol
    g["moves"].append((g["turn"], pos, USER_ID))
    g["turn"] += 1

    msg = f"TYPE: TICTACTOE_MOVE\nGAMEID: {gameid}\nPOS: {pos}\nTURN: {g['turn']-1}\n\n"
    # Send to opponent
    opp = g["player_o"] if USER_ID == g["player_x"] else g["player_x"]
    if opp in peers:
        msg_id = generate_msgid()
        send_with_retry(msg, peers[opp]["ip"], msg_id, "game")
    render_grid(gameid)

def handle_invite(msg: str, sender_ip: str):
    gameid = extract_field(msg, "GAMEID")
    player_x = extract_field(msg, "PLAYER_X")
    player_o = extract_field(msg, "PLAYER_O")
    if gameid in games:
        return  # Already exists
    games[gameid] = {
        "player_x": player_x,
        "player_o": player_o,
        "board": [[None]*3 for _ in range(3)],
        "turn": 1,
        "moves": [],
        "status": "active"
    }
    log(f"Invited to game {gameid} by {player_x}")
    render_grid(gameid)

def handle_move(msg: str, sender_ip: str):
    gameid = extract_field(msg, "GAMEID")
    pos = int(extract_field(msg, "POS"))
    turn = int(extract_field(msg, "TURN"))
    if gameid not in games:
        return
    g = games[gameid]
    # Prevent duplicates
    if turn <= len(g["moves"]):
        log(f"Duplicate move {turn} in game {gameid}", level="DEBUG")
        return
    row, col = pos // 3, pos % 3
    symbol = "O" if USER_ID == g["player_x"] else "X"
    g["board"][row][col] = symbol
    g["moves"].append((turn, pos, "opponent"))
    g["turn"] = turn + 1
    render_grid(gameid)

# =============================
# Group Messaging
# =============================

def create_group(group_id: str, members: List[str], name: str):
    valid_members = [m for m in members if m in peers]
    groups[group_id] = {
        "name": name,
        "members": valid_members
    }
    msg = (
        f"TYPE: GROUP_CREATE\n"
        f"GROUP_ID: {group_id}\n"
        f"NAME: {name}\n"
        f"MEMBERS: {','.join(valid_members)}\n\n"
    )
    msg_id = generate_msgid()
    for member in valid_members:
        if member != USER_ID:
            send_with_retry(msg, peers[member]["ip"], msg_id + "_" + member, "group")

def send_group_message(group_id: str, message: str):
    if group_id not in groups:
        log("Group not found", level="ERROR")
        return
    msg = f"TYPE: GROUP_MESSAGE\nGROUP_ID: {group_id}\nMESSAGE: {message}\n\n"
    msg_id = generate_msgid()
    for member in groups[group_id]["members"]:
        if member != USER_ID and member in peers:
            send_with_retry(msg, peers[member]["ip"], msg_id, "group")

# =============================
# UDP Listener
# =============================

def listen_loop():
    global running
    while running:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            ip, port = addr
            raw_msg = data.decode('utf-8', errors='ignore')
            if not raw_msg.endswith("\n\n"):
                log(f"Malformed: no \\n\\n: {raw_msg[:50]}", level="WARN")
                continue
            if not is_valid_utf8(raw_msg):
                log(f"Invalid UTF-8 from {ip}", level="ERROR")
                continue

            msg_type = extract_field(raw_msg, "TYPE")
            if not msg_type:
                log(f"No TYPE in message from {ip}", level="WARN")
                continue

            # IP validation
            claimed_from = extract_field(raw_msg, "FROM")
            if claimed_from:
                claimed_ip = claimed_from.split("@")[1] if "@" in claimed_from else None
                if claimed_ip and claimed_ip != ip:
                    log(f"SECURITY WARNING: IP mismatch. Claimed={claimed_ip}, Actual={ip}", level="WARN")
                    continue

            # Token validation
            token = extract_field(raw_msg, "TOKEN")
            scopes = {
                "POST": "chat", "DM": "dm", "FOLLOW": "follow", "LIKE": "like",
                "FILE_OFFER": "file", "FILE_CHUNK": "file", "TICTACTOE_MOVE": "game",
                "GROUP_MESSAGE": "group"
            }
            required_scope = scopes.get(msg_type)
            if required_scope and token and not validate_token(token, required_scope):
                log(f"Invalid token in {msg_type} from {ip}", level="WARN")
                continue

            # Add to queue
            with message_lock:
                message_queue.append({
                    "raw": raw_msg,
                    "type": msg_type,
                    "ip": ip,
                    "port": port,
                    "time": time.time()
                })

            # Send ACK if needed
            msgid = extract_field(raw_msg, "MSGID")
            if msgid:
                send_ack(msgid, ip)

        except socket.timeout:
            continue
        except Exception as e:
            log(f"Recv error: {e}", level="ERROR")

# =============================
# Message Processor
# =============================

def process_messages():
    while running:
        with message_lock:
            if message_queue:
                msg = message_queue.pop(0)
            else:
                msg = None
        if msg:
            t = msg["type"]
            ip = msg["ip"]
            raw = msg["raw"]
            log(f"RX {t} from {ip}", level="DEBUG")

            if t == "PROFILE":
                handle_profile(raw, ip)
            elif t == "PING":
                handle_ping(raw, ip, msg["port"])
            elif t == "PONG":
                user = extract_field(raw, "USER_ID")
                if user:
                    peers.setdefault(user, {})["ip"] = ip
            elif t == "FILE_CHUNK":
                handle_file_chunk(raw, ip)
            elif t == "TICTACTOE_INVITE":
                handle_invite(raw, ip)
            elif t == "TICTACTOE_MOVE":
                handle_move(raw, ip)
            elif t in ["POST", "DM", "FOLLOW", "LIKE", "GROUP_CREATE", "GROUP_MESSAGE"]:
                # Just log for now
                log(f"{t}: {extract_field(raw, 'MESSAGE') or '...'}", level="INFO")
            elif t == "ACK":
                msgid = extract_field(raw, "MSGID")
                with message_lock:
                    # Mark as received
                    pass  # We use polling in send_with_retry
        time.sleep(0.01)

# =============================
# CLI & Main
# =============================

def show_help():
    print("""
Commands:
  post <msg>               - Send public post
  dm <user> <msg>          - Send DM
  follow <user>            - Follow user
  like <postid>            - Like a post
  file <user> <path>       - Send file
  game <user>              - Invite to TTT
  move <gameid> <0-8>      - Make TTT move
  group create <id> <mems> - Create group (comma list)
  group send <id> <msg>    - Send group message
  peers                    - List peers
  help                     - Show this
  exit                     - Quit
""")

def main():
    global sock, USER_ID, DISPLAY_NAME, VERBOSE, TEST_LOSS, running

    parser = argparse.ArgumentParser()
    parser.add_argument("--userid", type=str, default=USER_ID)
    parser.add_argument("--name", type=str, default="Anonymous")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--loss", action="store_true")
    args = parser.parse_args()

    USER_ID = args.userid
    DISPLAY_NAME = args.name
    VERBOSE = args.verbose
    TEST_LOSS = args.loss

    log(f"Starting LSNP node: {USER_ID} ({DISPLAY_NAME})")

    # Setup socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", UDP_PORT))
        sock.settimeout(1.0)
    except Exception as e:
        log(f"Bind failed: {e}", level="ERROR")
        return

    # Start threads
    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=process_messages, daemon=True).start()
    threading.Thread(target=broadcast_profile, daemon=True).start()

    log("System ready. Type 'help' for commands.")

    show_help()
    try:
        while True:
            try:
                cmd = input("> ").strip()
                if not cmd:
                    continue
                parts = cmd.split(" ", 2)
                c = parts[0].lower()

                if c == "exit":
                    break
                elif c == "help":
                    show_help()
                elif c == "post" and len(parts) > 1:
                    send_post(parts[1])
                elif c == "dm" and len(parts) > 2:
                    send_dm(parts[1], parts[2])
                elif c == "follow" and len(parts) > 1:
                    send_follow(parts[1])
                elif c == "like" and len(parts) > 1:
                    toggle_like(parts[1])
                elif c == "file" and len(parts) > 2:
                    send_file(parts[1], parts[2])
                elif c == "game" and len(parts) > 1:
                    start_game(parts[1])
                elif c == "move" and len(parts) > 2:
                    make_move(parts[1], int(parts[2]))
                elif c == "peers":
                    for uid, p in peers.items():
                        print(f"{uid} @ {p['ip']} ({p['display_name']})")
                elif c == "group" and len(parts) > 1:
                    sub = parts[1]
                    if sub == "create" and len(parts) > 2:
                        rest = parts[2].split(" ", 1)
                        gid = rest[0]
                        mems = rest[1].split(",") if len(rest) > 1 else []
                        create_group(gid, mems, f"Group {gid}")
                    elif sub == "send" and len(parts) > 2:
                        rest = parts[2].split(" ", 1)
                        gid = rest[0]
                        msg = rest[1] if len(rest) > 1 else ""
                        send_group_message(gid, msg)
                else:
                    print("Unknown command. Type 'help'.")
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"Command error: {e}", level="ERROR")
    except EOFError:
        pass
    finally:
        running = False
        log("Shutting down...")
        log_file.close()
        sock.close()

if __name__ == "__main__":
    main()