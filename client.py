# client.py
"""
LSNP Client
Connects to central server for chat, DMs, games
"""
import socket
import threading
import time
import sys
import argparse
import random
import os
import json
from datetime import datetime
from typing import Dict

# =============================
# Configuration
# =============================
SERVER_IP = "127.0.0.1"
UDP_PORT = 50999
BUFFER_SIZE = 8192
HEARTBEAT_INTERVAL = 5

# =============================
# Global State
# =============================
running = True
sock = None
USER_ID = ""
DISPLAY_NAME = ""
server_addr = (SERVER_IP, UDP_PORT)
peers = {}  # user_id -> display_name
followers = {}
following = {}

def log(msg, level="INFO"):
    print(f"[{level}] {msg}")

def send_udp(data: str):
    try:
        if not data.endswith("\n\n"):
            data += "\n\n"
        sock.sendto(data.encode('utf-8'), server_addr)
    except Exception as e:
        log(f"Send failed: {e}", level="ERROR")

def register_with_server():
    msg = (
        f"TYPE: REGISTER\n"
        f"USER_ID: {USER_ID}\n"
        f"DISPLAY_NAME: {DISPLAY_NAME}"
    )
    send_udp(msg)
    log("👤 Registered with server")

def heartbeat():
    while running:
        msg = f"TYPE: HEARTBEAT\nUSER_ID: {USER_ID}"
        send_udp(msg)
        time.sleep(HEARTBEAT_INTERVAL)

def listen_loop():
    global peers
    sock.settimeout(1)
    while running:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
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

            if msg_type == "WELCOME":
                try:
                    peer_json = headers.get("PEERS", "{}")
                    peers = json.loads(peer_json)
                    clear_input()
                    print("🌐 Peers online:")
                    for uid, name in peers.items():
                        print(f"  🧑 {name} ({uid})")
                    print_prompt()
                except:
                    peers = {}

            elif msg_type == "PEER_JOINED":
                uid = headers.get("USER_ID")
                name = headers.get("DISPLAY_NAME")
                if uid and name:
                    peers[uid] = name
                    clear_input()
                    print(f"🎉 {name} joined the chat!")
                    print_prompt()

            elif msg_type == "DM":
                frm = headers.get("FROM", "").split("@")[0]
                frm_name = peers.get(frm, frm)
                msg = headers.get("MESSAGE", "")
                clear_input()
                print(f"[← {frm_name}] {msg}")
                print_prompt()

            elif msg_type == "POST":
                frm = headers.get("FROM", "").split("@")[0]
                frm_name = peers.get(frm, frm)
                msg = headers.get("MESSAGE", "")
                clear_input()
                print(f"[POST][{frm_name}] {msg}")
                print_prompt()
                
            elif msg_type == "FOLLOW_NOTIFY":
                uid = headers.get("USER_ID", "")
                name = headers.get("DISPLAY_NAME", uid)
                followers[uid] = name 
                clear_input()
                print(f"👤 {name} followed you!")
                print_prompt()

            elif msg_type == "TICTACTOE_INVITE":
                gameid = headers.get("GAMEID")
                player_x = headers.get("PLAYER_X")
                player_o = headers.get("PLAYER_O")
                x_name = peers.get(player_x, player_x)
                o_name = peers.get(player_o, o_name)
                clear_input()
                print(f"🎮 Game Invite: {gameid}")
                print(f"   X: {x_name} | O: {o_name}")
                print_prompt()

            elif msg_type == "TICTACTOE_MOVE":
                gameid = headers.get("GAMEID")
                pos = headers.get("POS")
                clear_input()
                print(f"🎮 Move in {gameid}: position {pos}")
                print_prompt()

        except socket.timeout:
            continue
        except Exception as e:
            if running:
                log(f"Recv error: {e}", level="ERROR")

def clear_input():
    sys.stdout.write('\r' + ' ' * 60 + '\r')

def print_prompt():
    sys.stdout.write("> ")
    sys.stdout.flush()

def send_dm(to_user: str, message: str):
    if to_user not in peers:
        print(f"❌ User '{to_user}' not found.")
        return
    msg = (
        f"TYPE: DM\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {to_user}\n"
        f"MESSAGE: {message}"
    )
    send_udp(msg)
    target_name = peers.get(to_user, to_user)
    clear_input()
    print(f"[→ {target_name}] {message}")
    print_prompt()

def send_post(message: str):
    timestamp = int(time.time())
    ttl = 3600
    token = f"{USER_ID}|{timestamp+ttl}|broadcast"
    msg_id = hex(random.getrandbits(64))[2:]
    
    msg = (
        f"TYPE: POST\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"MESSAGE_ID: {msg_id}\n"
        f"TOKEN: {token}\n"
        f"TTL: {ttl}\n"
        f"MESSAGE: {message}"
    )
    send_udp(msg)
    clear_input()
    print(f"[You] {message}")
    print_prompt()

def get_my_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"
    
def follow(name: str):
    name = name.strip().lower()
    match_uid = None
    for uid, display in peers.items():
        if display.lower() == name:
            match_uid = uid
            break
    if not match_uid:
        print(f"❌ No peer found with display name '{name}'")
        return
    if match_uid in following:
        print(f"❌ Already following {peers[match_uid]}")
        return

    timestamp = int(time.time())
    ttl = 3600
    token = f"{USER_ID}|{timestamp+ttl}|follow"
    msg_id = hex(random.getrandbits(64))[2:]
    msg = (
        f"TYPE: FOLLOW\n"
        f"MESSAGE_ID: {msg_id}\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {match_uid}\n"
        f"TIMESTAMP: {timestamp}\n"
        f"TOKEN: {token}"
    )
    send_udp(msg)
    
    following[match_uid] = peers[match_uid]
    print(f"✅ Following {peers[match_uid]}")
    
def show_help():
    print("""
Commands:
  post <msg>               - Send public post
  dm <user> <msg>          - Send DM
  follow <user>            - Follow user (server logs)
  like <postid>            - Like a post (server logs)
  file <user> <path>       - Send file (not implemented yet)
  game <user>              - Invite to TTT (not implemented yet)
  move <gameid> <0-8>      - Make TTT move
  group create <id> <mems> - Create group (comma list)
  group send <id> <msg>    - Send group message
  peers                    - List peers
  help                     - Show this
  exit                     - Quit
""")

def main():
    global sock, USER_ID, DISPLAY_NAME, running

    parser = argparse.ArgumentParser()
    parser.add_argument("--userid", type=str, default=f"user_{random.randint(1000,9999)}")
    parser.add_argument("--name", type=str, default="Anonymous")
    args = parser.parse_args()

    USER_ID = args.userid
    DISPLAY_NAME = args.name

    print(f"💬 Starting LSNP Client: {DISPLAY_NAME} ({USER_ID})")
    print(f"📍 Connecting to server at {SERVER_IP}:{UDP_PORT}")

    global sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1)

    register_with_server()

    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

    time.sleep(1)
    show_help()
    print_prompt()

    buffer = ""
    try:
        while running:
            char = None
            if os.name == "nt":
                import msvcrt
                if msvcrt.kbhit():
                    char = msvcrt.getch().decode('utf-8', errors='ignore')
            else:
                import select
                if select.select([sys.stdin], [], [], 0.1) == ([sys.stdin], [], []):
                    char = sys.stdin.read(1)

            if char in ("\r", "\n"):
                cmd = buffer.strip()
                buffer = ""
                if cmd:
                    parts = cmd.split(" ", 1) #changed to splut 1x only
                    
                    c = parts[0].lower()
                    if c == "exit":
                        break
                    elif c == "help":
                        show_help()
                    elif c == "peers":
                        print("👥 Online Peers:")
                        for uid, name in peers.items():
                            print(f"  {uid} ({name})")
                    elif c == "post" and len(parts) > 1:
                        send_post(parts[1])
                    elif c == "dm" and len(parts) >= 3:
                        send_dm(parts[1], parts[2])
                    elif c == "follow" and len(parts) > 1:
                        follow(parts[1])
                    elif c == "like" and len(parts) > 1:
                        print(f"❤️ Liked post {parts[1]}")
                    elif c == "game" and len(parts) > 1:
                        print("🎮 Game invite sent (demo only)")
                    elif c == "move" and len(parts) > 2:
                        gameid, pos = parts[1], parts[2]
                        print(f"➡️ Move {pos} in game {gameid}")
                    elif c == "group" and len(parts) > 1:
                        if parts[1] == "create" and len(parts) > 2:
                            subparts = parts[2].split(" ", 1)
                            gid = subparts[0]
                            mems = subparts[1].split(",") if len(subparts) > 1 else []
                            print(f"👥 Created group {gid} with {len(mems)} members")
                        elif parts[1] == "send" and len(parts) > 2:
                            gid, msg = parts[2].split(" ", 1)
                            print(f"[togroup {gid}] {msg}")
                    elif c == "debug":
                        print("🔍 Debug Info:")
                        print(f"Following: {following}")
                        print(f"Followers: {followers}")
                    else:
                        print("❌ Unknown command. Type 'help'.")
                print_prompt()
            elif char == "\b":
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
        sock.close()
        print("\n👋 Goodbye!")

if __name__ == "__main__":
    main()