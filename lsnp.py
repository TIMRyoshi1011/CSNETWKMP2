#!/usr/bin/env python3
"""
Lightweight Social Networking Protocol (LSNP)
Fully Fixed Version: Terminal-to-Terminal Chat, Discovery, DM, Games
"""

import socket
import threading
import time
import os
import base64
import sys
import argparse
import random
from datetime import datetime
from typing import Dict, List, Optional

# =============================
# Configuration
# =============================
UDP_PORT = 50999
BROADCAST_INTERVAL = 60       # Faster for testing
MAX_RETRIES = 3
ACK_TIMEOUT = 2
LOSS_RATE = 0.0  # Set to 0.1–0.3 to test loss
BUFFER_SIZE = 8192
VERBOSE = False
TEST_LOSS = False

# =============================
# Global State
# =============================
running = True
sock = None
message_queue = []
message_lock = threading.Lock()

# Local user config
USER_ID = "user_" + str(random.randint(1000, 9999))
DISPLAY_NAME = "Anonymous"
AVATAR = "https://example.com/avatar.png"

# Data stores
peers = {}  # user_id -> {ip, display_name, avatar, last_seen}
tokens = {}  # token -> {scope, expiry, revoked}
games = {}  # game_id -> game state
groups = {}  # group_id -> {name, members}
received_chunks = {}  # fileid -> {total, chunks, sender}
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

# ✅ FIXED: send_udp function
def send_udp(data: str, ip: str, port: int = UDP_PORT):
    """Send UDP packet with optional simulated loss."""
    if not data.endswith("\n\n"):
        data += "\n\n"  # Ensure proper termination

    if TEST_LOSS and random.random() < LOSS_RATE:
        log(f"[LOSS] Dropped packet to {ip}: {data[:50]}...", level="DEBUG")
        return

    try:
        sock.sendto(data.encode('utf-8'), (ip, port))
        if VERBOSE:
            log(f"Sent to {ip}: {data.strip()}", level="DEBUG")
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
# Peer Discovery
# =============================
def broadcast_profile():
    """
    Broadcasts PROFILE message.
    First two broadcasts every 10 seconds, then every BROADCAST_INTERVAL.
    """
    count = 0
    fast_interval = 10  # seconds

    while running:
        msg = (
            f"TYPE: PROFILE\n"
            f"USER_ID: {USER_ID}\n"
            f"DISPLAY_NAME: {DISPLAY_NAME}\n"
            f"AVATAR: {AVATAR}\n\n"
        )
        send_udp(msg, '<broadcast>')
        log(f"Broadcasted PROFILE", level="DEBUG")

        if count < 3:
            # Fast broadcast twice
            time.sleep(fast_interval)
            count += 1
        else:
            # Then go back to normal interval
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
        log(f"Discovered peer: {user_id} ({display_name}) @ {sender_ip}", level="INFO")

def handle_ping(msg: str, sender_ip: str, sender_port: int):
    pong = f"TYPE: PONG\nUSER_ID: {USER_ID}\n\n"
    send_udp(pong, sender_ip, sender_port)

# =============================
# ACK & Retry System
# =============================
def send_with_retry(msg: str, dest_ip: str, msg_id: str, scope: str):
    token = create_token(scope)
    # Ensure message ends with exactly one \n before adding TOKEN/MSGID
    if not msg.endswith("\n"):
        msg += "\n"
    msg += f"TOKEN: {token}\nMSGID: {msg_id}\n\n"  # Double \n\n at end

    retries = 0
    while retries < MAX_RETRIES and running:
        send_udp(msg, dest_ip)
        log(f"Sent {msg_id} to {dest_ip} (retry {retries})", level="DEBUG")
        start = time.time()
        acked = False
        while time.time() - start < ACK_TIMEOUT:
            with message_lock:
                for m in message_queue:
                    if m["type"] == "ACK" and extract_field(m["raw"], "MSGID") == msg_id:
                        message_queue.remove(m)
                        acked = True
                        break
            if acked:
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
    msg_id = generate_msgid()
    for peer_id, info in peers.items():
        send_with_retry(msg, info["ip"], msg_id, "chat")
def get_my_ip() -> str:
    """Get local IP address by connecting to a dummy external address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))  # Doesn't actually send data
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
def send_dm(to_user: str, message: str):
    if to_user not in peers:
        log(f"User {to_user} not found", level="ERROR")
        return
    # Add FROM field: USER_ID@IP
    my_ip = get_my_ip()
    msg = (
        f"TYPE: DM\n"
        f"FROM: {USER_ID}@{my_ip}\n"
        f"TO: {to_user}\n"
        f"MESSAGE: {message}"
    )
    msg_id = generate_msgid()
    sent = send_with_retry(msg, peers[to_user]["ip"], msg_id, "dm")
    if sent:
        target_name = peers[to_user]["display_name"]
        print(f"[→ {target_name}] {message}")

def send_follow(target: str):
    if target not in peers:
        log(f"User {target} not found", level="ERROR")
        return
    msg = f"TYPE: FOLLOW\nTARGET_USER: {target}"
    msg_id = generate_msgid()
    send_with_retry(msg, peers[target]["ip"], msg_id, "follow")

def toggle_like(post_id: str):
    msg = f"TYPE: LIKE\nPOST_ID: {post_id}"
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
    if not os.path.exists(filepath):
        log(f"File not found: {filepath}", level="ERROR")
        return

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    fileid = generate_fileid()

    offer = (
        f"TYPE: FILE_OFFER\n"
        f"FILENAME: {filename}\n"
        f"FILESIZE: {filesize}\n"
        f"FILEID: {fileid}\n"
        f"TO: {to_user}"
    )
    msg_id = generate_msgid()
    if send_with_retry(offer, peers[to_user]["ip"], msg_id, "file"):
        log(f"File offer sent: {filename}")
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
            f"DATA: {b64_data}"
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
    except Exception as e:
        log(f"Base64 decode error: {e}", level="ERROR")
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

    if all(received_chunks[fileid]["chunks"]):
        final_data = b''.join(filter(None, received_chunks[fileid]["chunks"]))
        os.makedirs("received", exist_ok=True)
        with open(f"received/received_{fileid}", "wb") as f:
            f.write(final_data)
        log(f"File saved: received/received_{fileid}")
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
    msg = f"TYPE: TICTACTOE_INVITE\nGAMEID: {gameid}\nPLAYER_X: {USER_ID}\nPLAYER_O: {invite_to}"
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

    msg = f"TYPE: TICTACTOE_MOVE\nGAMEID: {gameid}\nPOS: {pos}\nTURN: {g['turn']-1}"
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
        return
    games[gameid] = {
        "player_x": player_x,
        "player_o": player_o,
        "board": [[None]*3 for _ in range(3)],
        "turn": 1,
        "moves": [],
        "status": "active"
    }
    log(f"🎮 Invited to game {gameid} by {player_x}")
    render_grid(gameid)

def handle_move(msg: str, sender_ip: str):
    gameid = extract_field(msg, "GAMEID")
    pos = int(extract_field(msg, "POS"))
    turn = int(extract_field(msg, "TURN"))
    if gameid not in games:
        return
    g = games[gameid]
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
        f"MEMBERS: {','.join(valid_members)}"
    )
    msg_id = generate_msgid()
    for member in valid_members:
        if member != USER_ID and member in peers:
            send_with_retry(msg, peers[member]["ip"], msg_id, "group")

def send_group_message(group_id: str, message: str):
    if group_id not in groups:
        log("Group not found", level="ERROR")
        return
    msg = f"TYPE: GROUP_MESSAGE\nGROUP_ID: {group_id}\nMESSAGE: {message}"
    msg_id = generate_msgid()
    for member in groups[group_id]["members"]:
        if member != USER_ID and member in peers:
            send_with_retry(msg, peers[member]["ip"], msg_id, "group")
    print(f"[togroup {group_id}] {message}")

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
            if claimed_from and "@" in claimed_from:
                claimed_ip = claimed_from.split("@")[1]
                if claimed_ip != ip:
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

            # Queue message
            with message_lock:
                message_queue.append({
                    "raw": raw_msg,
                    "type": msg_type,
                    "ip": ip,
                    "port": port,
                    "time": time.time()
                })

            # Send ACK if message has MSGID
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
        time.sleep(0.01)
        with message_lock:
            queue_copy = message_queue.copy()
            message_queue.clear()
        for msg in queue_copy:
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
            elif t == "DM":
                to_user = extract_field(raw, "TO")
                if to_user == USER_ID:
                    sender = extract_field(raw, "FROM")
                    sender = sender.split("@")[0] if sender and "@" in sender else "someone"
                    sender_name = peers.get(sender, {}).get("display_name", sender)
                    message = extract_field(raw, "MESSAGE") or "No message"
                    sys.stdout.write('\r' + ' ' * 60 + '\r')  # Clear line
                    print(f"[← {sender_name}] {message}")
                    print("> ", end="", flush=True)
            elif t == "POST":
                sender = extract_field(raw, "FROM")
                sender = sender.split("@")[0] if sender and "@" in sender else "someone"
                sender_name = peers.get(sender, {}).get("display_name", sender)
                message = extract_field(raw, "MESSAGE") or ""
                sys.stdout.write('\r' + ' ' * 60 + '\r')
                print(f"[POST][{sender_name}] {message}")
                print("> ", end="", flush=True)
            elif t == "ACK":
                msgid = extract_field(raw, "MSGID")
                # Handled in send_with_retry
            else:
                log(f"Unhandled message type: {t}", level="DEBUG")

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

def clear_input_line():
    sys.stdout.write('\r' + ' ' * 60 + '\r')
    sys.stdout.flush()

def print_prompt():
    sys.stdout.write("> ")
    sys.stdout.flush()

def main():
    global sock, USER_ID, DISPLAY_NAME, VERBOSE, TEST_LOSS, running

    parser = argparse.ArgumentParser()
    parser.add_argument("--userid", type=str, default=f"user_{random.randint(1000,9999)}")
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
        sock.settimeout(0.1)
    except Exception as e:
        log(f"Bind failed: {e}", level="ERROR")
        return

    # Start threads
    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=process_messages, daemon=True).start()
    threading.Thread(target=broadcast_profile, daemon=True).start()

    time.sleep(2)  # Allow discovery

    show_help()
    print("> ", end="", flush=True)

    import os
    if os.name == "nt":
        import msvcrt
        def get_input():
            if msvcrt.kbhit():
                return msvcrt.getch().decode('utf-8', errors='ignore')
            return None
    else:
        import select
        def get_input():
            if select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], []):
                return sys.stdin.read(1)
            return None

    buffer = ""
    try:
        while running:
            char = get_input()
            if char == '\r' or char == '\n':
                cmd = buffer.strip()
                buffer = ""
                if cmd:
                    parts = cmd.split(" ", 2)
                    c = parts[0].lower()
                    if c == "exit":
                        break
                    elif c == "help":
                        show_help()
                    elif c == "peers":
                        print("\n--- Peers ---")
                        for uid, p in peers.items():
                            print(f"{uid} ({p['display_name']}) @ {p['ip']}")
                        print("-------------")
                    elif c == "post" and len(parts) > 1:
                        send_post(parts[1])
                        print(f"[You] {parts[1]}")
                    elif c == "dm" and len(parts) >= 3:
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
                    elif c == "group" and len(parts) > 1:
                        if parts[1] == "create" and len(parts) > 2:
                            gid, *mems = parts[2].split()
                            create_group(gid, mems, f"Group {gid}")
                        elif parts[1] == "send" and len(parts) > 2:
                            gid, msg = parts[2].split(" ", 1)
                            send_group_message(gid, msg)
                    else:
                        print("Unknown command. Type 'help'.")
                print("> ", end="", flush=True)
            elif char == '\b':
                if buffer:
                    buffer = buffer[:-1]
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
            elif char:
                buffer += char
                sys.stdout.write(char)
                sys.stdout.flush()

            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        log("Shutting down...")
        log_file.close()
        sock.close()

if __name__ == "__main__":
    main()
