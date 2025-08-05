#!/usr/bin/env python3
"""
Decentralized UDP-based Social Network
Supports: Messaging, File Transfer, Tic-Tac-Toe, Groups, Tokens, and more
No central server — fully peer-to-peer using UDP broadcast/unicast
"""

import socket
import threading
import time
import sys
import base64
import os
import random
import string
from datetime import datetime
from typing import Dict, List, Optional

# -------------------------------
# CONFIGURATION
# -------------------------------
PORT = 50999
BROADCAST_ADDR = '255.255.255.255'
BUFFER_SIZE = 65536  # Max UDP packet size
TTL_PING = 300
TTL_DEFAULT = 3600

# Global state
user_id: str = ""
display_name: str = ""
status: str = "Online"
avatar_data: Optional[str] = None  # Base64 string
verbose = False

# Local storage
profiles: Dict[str, dict] = {}  # USER_ID -> profile
followers: set = set()
following: set = set()
posts: Dict[str, dict] = {}  # MESSAGE_ID -> post
revocation_list: set = set()  # Set of revoked tokens
pending_acks: Dict[str, dict] = {}  # MESSAGE_ID -> {msg, dst, retries}
file_metadata: Dict[str, dict] = {}  # FILEID -> info about file chunks
groups: Dict[str, dict] = {}  # GROUP_ID -> {name, members}
games: Dict[str, dict] = {}  # GAMEID -> game state

# Setup UDP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("", PORT))
except Exception as e:
    print(f"[ERROR] Could not bind to port {PORT}: {e}")
    sys.exit(1)


# -------------------------------
# VERBOSE PRINTING
# -------------------------------
def printv(*args):
    if verbose:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{timestamp}] {' '.join(map(str, args))}")


# -------------------------------
# MESSAGE PARSING & BUILDING
# -------------------------------
def parse_message(raw: str, sender_ip: str) -> Optional[Dict[str, str]]:
    lines = [line.strip() for line in raw.strip().split('\n') if line.strip()]
    msg = {}
    for line in lines:
        if ': ' not in line:
            continue
        key, value = line.split(': ', 1)
        msg[key.strip()] = value.strip()

    if not msg:
        return None

    # Security: Verify sender IP matches FROM or USER_ID
    identity_field = msg.get('FROM') or msg.get('USER_ID')
    if identity_field:
        try:
            expected_ip = identity_field.split('@')[1]
            if expected_ip != sender_ip:
                printv(f"[SECURITY] IP mismatch: {expected_ip} != {sender_ip} — Dropping message")
                return None
        except Exception:
            return None

    return msg


def build_message(fields: dict) -> str:
    lines = [f"{k}: {v}" for k, v in fields.items()]
    return '\n'.join(lines) + '\n\n'


# -------------------------------
# UDP SEND
# -------------------------------
def send_udp(msg: str, ip: str):
    try:
        sock.sendto(msg.encode('utf-8'), (ip, PORT))
        printv("SEND >", ip, msg.split('\n')[0])
    except Exception as e:
        printv("[SEND FAIL]", e)


# -------------------------------
# RELIABLE SEND WITH RETRY (for DM, FILE_CHUNK, etc.)
# -------------------------------
def send_reliable(msg: str, ip: str, msg_id: str):
    for attempt in range(3):
        send_udp(msg, ip)
        pending_acks[msg_id] = {'msg': msg, 'ip': ip, 'retries': attempt}
        time.sleep(2)
        if msg_id not in pending_acks:
            return
    if msg_id in pending_acks:
        printv(f"[RETRY] Failed after 3 attempts: {msg_id}")
        del pending_acks[msg_id]


# -------------------------------
# BROADCAST PROFILE & PING
# -------------------------------
def send_ping():
    while True:
        msg = build_message({
            "TYPE": "PING",
            "USER_ID": user_id
        })
        send_udp(msg, BROADCAST_ADDR)
        time.sleep(TTL_PING)


def send_profile():
    while True:
        fields = {
            "TYPE": "PROFILE",
            "USER_ID": user_id,
            "DISPLAY_NAME": display_name,
            "STATUS": status,
        }
        if avatar_data:
            fields.update({
                "AVATAR_TYPE": "image/png",
                "AVATAR_ENCODING": "base64",
                "AVATAR_DATA": avatar_data
            })
        msg = build_message(fields)
        send_udp(msg, BROADCAST_ADDR)
        time.sleep(TTL_PING)


# -------------------------------
# TOKEN SYSTEM
# -------------------------------
def create_token(user_id: str, scope: str, ttl: int = TTL_DEFAULT) -> str:
    timestamp = int(time.time())
    return f"{user_id}|{timestamp + ttl}|{scope}"


def validate_token(token: str, scope: str) -> bool:
    if token in revocation_list:
        return False
    try:
        parts = token.split('|')
        if len(parts) != 3:
            return False
        _, expiry_str, tok_scope = parts
        expiry = int(expiry_str)
        if time.time() > expiry:
            return False
        if tok_scope != scope:
            return False
        return True
    except:
        return False


# -------------------------------
# HANDLE INCOMING MESSAGES
# -------------------------------
def parse_and_handle_message(raw: str, sender_ip: str):
    msg = parse_message(raw, sender_ip)
    if not msg:
        return

    msg_type = msg.get("TYPE")
    printv("RECV <", sender_ip, msg_type or "(unknown)")

    # Validate token if required
    token = msg.get("TOKEN")
    scope_map = {
        "POST": "broadcast",
        "DM": "chat",
        "FOLLOW": "follow",
        "UNFOLLOW": "follow",
        "LIKE": "broadcast",
        "FILE_OFFER": "file",
        "FILE_CHUNK": "file",
        "TICTACTOE_INVITE": "game",
        "TICTACTOE_MOVE": "game",
        "GROUP_CREATE": "group",
        "GROUP_UPDATE": "group",
        "GROUP_MESSAGE": "group"
    }
    required_scope = scope_map.get(msg_type)
    if required_scope and token:
        if not validate_token(token, required_scope):
            printv(f"[TOKEN] Invalid/revoked/expired token from {sender_ip}")
            return

    # Route message
    handlers = {
        "PING": handle_ping,
        "PROFILE": handle_profile,
        "POST": handle_post,
        "DM": handle_dm,
        "FOLLOW": handle_follow,
        "UNFOLLOW": handle_unfollow,
        "LIKE": handle_like,
        "REVOKE": handle_revoke,
        "ACK": handle_ack,
        "FILE_OFFER": handle_file_offer,
        "FILE_CHUNK": handle_file_chunk,
        "FILE_RECEIVED": lambda m: printv("File receipt confirmed"),
        "TICTACTOE_INVITE": handle_tictactoe_invite,
        "TICTACTOE_MOVE": handle_tictactoe_move,
        "TICTACTOE_RESULT": handle_tictactoe_result,
        "GROUP_CREATE": handle_group_create,
        "GROUP_UPDATE": handle_group_update,
        "GROUP_MESSAGE": handle_group_message,
    }
    if msg_type in handlers:
        handlers[msg_type](msg)
    else:
        printv(f"[UNKNOWN] {msg_type}")


# -------------------------------
# MESSAGE HANDLERS
# -------------------------------
def handle_ping(msg):
    # Silent presence update
    pass


def handle_profile(msg):
    uid = msg['USER_ID']
    profiles[uid] = {
        'DISPLAY_NAME': msg.get('DISPLAY_NAME'),
        'STATUS': msg.get('STATUS'),
        'AVATAR_DATA': msg.get('AVATAR_DATA'),
        'AVATAR_TYPE': msg.get('AVATAR_TYPE'),
        'LAST_SEEN': time.time()
    }


def handle_post(msg):
    msg_id = msg['MESSAGE_ID']
    if msg_id not in posts:
        print(f"[POST] {msg['USER_ID']}: {msg['CONTENT']}")
        posts[msg_id] = {
            'user_id': msg['USER_ID'],
            'content': msg['CONTENT'],
            'timestamp': time.time()
        }


def handle_dm(msg):
    frm = msg['FROM']
    content = msg['CONTENT']
    print(f"[DM] {frm}: {content}")
    send_ack(msg['MESSAGE_ID'], frm.split('@')[1])


def handle_follow(msg):
    frm = msg['FROM']
    action = "followed" if msg['TYPE'] == "FOLLOW" else "unfollowed"
    print(f"User {frm} has {action} you")


def handle_unfollow(msg):
    handle_follow(msg)


def handle_like(msg):
    frm = msg['FROM'].split('@')[0]
    post_ts = msg['POST_TIMESTAMP']
    print(f"{frm} likes your post [{post_ts}]")


def handle_revoke(msg):
    token = msg['TOKEN']
    revocation_list.add(token)
    printv(f"[TOKEN] Revoked: {token}")


def handle_ack(msg):
    msg_id = msg['MESSAGE_ID']
    if msg_id in pending_acks:
        del pending_acks[msg_id]


def send_ack(msg_id: str, dst_ip: str):
    msg = build_message({
        "TYPE": "ACK",
        "MESSAGE_ID": msg_id,
        "STATUS": "RECEIVED"
    })
    send_udp(msg, dst_ip)


# -------------------------------
# FILE TRANSFER
# -------------------------------
def handle_file_offer(msg):
    file_id = msg['FILEID']
    filename = msg['FILENAME']
    frm = msg['FROM']
    from_ip = frm.split('@')[1]

    print(f"[FILE] {frm} is sending you '{filename}'. Accept? (y/n)")
    if input("> ").lower() == 'y':
        total_chunks = int(msg['TOTAL_CHUNKS'])
        file_metadata[file_id] = {
            'filename': filename,
            'chunks': total_chunks,
            'received': 0,
            'data': [None] * total_chunks
        }
        printv(f"[FILE] Ready to receive {file_id}")


def handle_file_chunk(msg):
    file_id = msg['FILEID']
    idx = int(msg['CHUNK_INDEX'])
    data_b64 = msg['DATA']
    from_ip = msg['FROM'].split('@')[1]

    if file_id not in file_metadata:
        send_ack(msg['MESSAGE_ID'], from_ip)
        return

    meta = file_metadata[file_id]
    if idx < 0 or idx >= len(meta['data']) or meta['data'][idx] is not None:
        send_ack(msg['MESSAGE_ID'], from_ip)
        return

    try:
        chunk_data = base64.b64decode(data_b64)
        meta['data'][idx] = chunk_data
        meta['received'] += 1
        send_ack(msg['MESSAGE_ID'], from_ip)

        if meta['received'] == meta['chunks']:
            filepath = f"received_{meta['filename']}"
            with open(filepath, 'wb') as f:
                for piece in meta['data']:
                    f.write(piece)
            print(f"File transfer of '{meta['filename']}' is complete")
            del file_metadata[file_id]
    except Exception as e:
        print(f"[ERROR] Failed to decode file chunk: {e}")


# -------------------------------
# TIC-TAC-TOE
# -------------------------------
def draw_board(board):
    print("\n".join([
        f" {board[0]} | {board[1]} | {board[2]} ",
        "---+---+---",
        f" {board[3]} | {board[4]} | {board[5]} ",
        "---+---+---",
        f" {board[6]} | {board[7]} | {board[8]} "
    ]))


def handle_tictactoe_invite(msg):
    game_id = msg['GAMEID']
    frm = msg['FROM']
    symbol = msg['SYMBOL']
    print(f"[GAME] {frm} is inviting you to play tic-tac-toe! Accept? (y/n)")
    if input("> ").lower() == 'y':
        games[game_id] = {'board': [' ']*9, 'turn': 1}
        # Example: auto-respond with move
        send_tictactoe_move(game_id, 4, 'O', frm.split('@')[1])


def send_tictactoe_move(game_id: str, pos: int, symbol: str, to_ip: str):
    current_turn = games.get(game_id, {}).get('turn', 1) + 1
    msg = build_message({
        "TYPE": "TICTACTOE_MOVE",
        "FROM": user_id,
        "TO": f"dummy@{to_ip}",
        "GAMEID": game_id,
        "POSITION": str(pos),
        "SYMBOL": symbol,
        "TURN": str(current_turn),
        "TOKEN": create_token(user_id, 'game')
    })
    send_reliable(msg, to_ip, ''.join(random.choices(string.hexdigits.lower(), k=16)))


def handle_tictactoe_move(msg):
    game_id = msg['GAMEID']
    pos = int(msg['POSITION'])
    symbol = msg['SYMBOL']
    turn = int(msg['TURN'])
    from_ip = msg['FROM'].split('@')[1]

    if game_id not in games:
        games[game_id] = {'board': [' ']*9, 'turn': 0}

    game = games[game_id]
    if turn != game['turn'] + 1:
        send_ack(msg['MESSAGE_ID'], from_ip)
        return

    if 0 <= pos < 9 and game['board'][pos] == ' ':
        game['board'][pos] = symbol
        game['turn'] = turn
        draw_board(game['board'])
        send_ack(msg['MESSAGE_ID'], from_ip)
    else:
        send_ack(msg['MESSAGE_ID'], from_ip)


def handle_tictactoe_result(msg):
    game_id = msg['GAMEID']
    result = msg['RESULT']
    symbol = msg['SYMBOL']
    board = games.get(game_id, {}).get('board', [' ']*9)
    draw_board(board)
    print(f"🎮 Game over: {result.upper()} by {symbol}")


# -------------------------------
# GROUPS
# -------------------------------
def handle_group_create(msg):
    gid = msg['GROUP_ID']
    name = msg['GROUP_NAME']
    members = msg['MEMBERS'].split(',')
    groups[gid] = {'name': name, 'members': set(members)}
    if user_id in members:
        print(f"You’ve been added to '{name}'")


def handle_group_update(msg):
    gid = msg['GROUP_ID']
    if gid not in groups:
        return
    add = msg.get('ADD')
    remove = msg.get('REMOVE')
    if add:
        groups[gid]['members'].add(add)
    if remove:
        groups[gid]['members'].discard(remove)
    name = groups[gid]['name']
    print(f"Group '{name}' member list was updated")


def handle_group_message(msg):
    frm = msg['FROM']
    gid = msg['GROUP_ID']
    if gid not in groups or frm not in groups[gid]['members']:
        return
    content = msg['CONTENT']
    name = groups[gid]['name']
    print(f"[GROUP '{name}'] {frm}: {content}")


# -------------------------------
# LISTEN LOOP
# -------------------------------
def listen_loop():
    printv("[NETWORK] Listening on UDP port", PORT)
    while True:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            raw = data.decode('utf-8', errors='replace')
            sender_ip = addr[0]
            parse_and_handle_message(raw, sender_ip)
        except Exception as e:
            print(f"[ERROR] Receive failed: {e}")


# -------------------------------
# MAIN
# -------------------------------
def main():
    global user_id, display_name, verbose, avatar_data

    if '--verbose' in sys.argv:
        verbose = True
        sys.argv.remove('--verbose')

    if len(sys.argv) < 3:
        print("Usage: p2pnet.py <display_name> <your_ip> [--verbose]")
        print("Example: p2pnet.py Alice 192.168.1.10 --verbose")
        sys.exit(1)

    display_name = sys.argv[1]
    my_ip = sys.argv[2]
    user_id = f"{display_name}@{my_ip}"

    # Load avatar (optional)
    avatar_path = "avatar.png"
    if os.path.exists(avatar_path):
        try:
            with open(avatar_path, "rb") as f:
                raw_data = f.read()
                if len(raw_data) <= 20 * 1024:  # <20KB
                    avatar_data = base64.b64encode(raw_data).decode('utf-8')
                else:
                    print("[AVATAR] Too large (>20KB), skipping")
        except Exception as e:
            print(f"[AVATAR] Load failed: {e}")

    # Start background threads
    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=send_ping, daemon=True).start()
    threading.Thread(target=send_profile, daemon=True).start()

    print(f"✅ Node '{display_name}' ({my_ip}) is online.")
    print("Commands: post <text>, dm <name> <ip> <msg>, exit")
    print("-" * 50)

    # CLI Loop
    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue
            cmd = line.split()

            if cmd[0] == 'help':
                print("Available commands:")
                print("  post <message>          — Send public post")
                print("  dm <name> <ip> <msg>    — Send direct message")
                print("  exit                    — Quit")

            elif cmd[0] == 'post' and len(cmd) > 1:
                content = ' '.join(cmd[1:])
                msg_id = ''.join(random.choices(string.hexdigits.lower(), k=16))
                token = create_token(user_id, 'broadcast')
                msg = build_message({
                    "TYPE": "POST",
                    "USER_ID": user_id,
                    "CONTENT": content,
                    "TTL": str(TTL_DEFAULT),
                    "MESSAGE_ID": msg_id,
                    "TOKEN": token
                })
                send_udp(msg, BROADCAST_ADDR)
                posts[msg_id] = {'content': content, 'timestamp': time.time()}

            elif cmd[0] == 'dm' and len(cmd) >= 4:
                target_name, target_ip = cmd[1], cmd[2]
                target_id = f"{target_name}@{target_ip}"
                content = ' '.join(cmd[3:])
                msg_id = ''.join(random.choices(string.hexdigits.lower(), k=16))
                token = create_token(user_id, 'chat')
                msg = build_message({
                    "TYPE": "DM",
                    "FROM": user_id,
                    "TO": target_id,
                    "CONTENT": content,
                    "TIMESTAMP": str(int(time.time())),
                    "MESSAGE_ID": msg_id,
                    "TOKEN": token
                })
                send_reliable(msg, target_ip, msg_id)

            elif cmd[0] == 'exit':
                print("Shutting down...")
                break

            else:
                print("Unknown command. Type 'help'.")

        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"[ERROR] {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
