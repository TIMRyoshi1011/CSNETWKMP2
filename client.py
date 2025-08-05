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
import base64

# =============================
# Configuration
# =============================
SERVER_IP = "127.0.0.1"
UDP_PORT = 50999
BUFFER_SIZE = 8192
HEARTBEAT_INTERVAL = 5
FILE_CHUNK_SIZE = 1024  # Max bytes per chunk (before base64 encoding)
FILE_TRANSFER_TIMEOUT = 300 # 5 minutes to complete a file transfer

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
groups = {}  # group_id -> list of user_ids (not display names)
STATUS = "Online" # default
peers_status = {}  # user_id -> {"name": ..., "status": ...}
AVATAR_PATH = None 
AVATAR_TYPE = None
AVATAR_ENCODING = None
AVATAR_DATA = None

# For receiving files
incoming_files: Dict[str, Dict] = {} # FILEID -> {filename, filesize, filetype, total_chunks, received_chunks: {index: data}, last_chunk_time, status: 'pending'/'receiving'/'complete'/'ignored'}
# For sending files (optional, for tracking)
outgoing_files: Dict[str, Dict] = {} # FILEID -> {filename, to_user, total_chunks, sent_chunks, status: 'offered'/'sending'/'complete'}
file_transfer_lock = threading.Lock()


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

# avatar
def load_avatar(path):
    
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) > 20480:
            print(f"⚠️ Avatar file too large: {len(data)} bytes (max 20KB)")
            return None, None, None

        ext = os.path.splitext(path)[1].lower()
        mime_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.bmp': 'image/bmp'
        }

        mime_type = mime_types.get(ext, 'image/jpeg')
        encoded_data = base64.b64encode(data).decode('ascii').replace('\n', '')

        return mime_type, 'base64', encoded_data
    except Exception as e:
        print(f"❌ Error loading avatar: {e}")
        return None, None, None

def heartbeat():
    while running:
        msg = f"TYPE: HEARTBEAT\nUSER_ID: {USER_ID}"
        send_udp(msg)
        time.sleep(HEARTBEAT_INTERVAL)

# --- New File Transfer Cleanup Loop ---
def file_transfer_cleanup_loop():
    while running:
        time.sleep(60) # Check every minute
        now = time.time()
        with file_transfer_lock:
            files_to_remove = []
            for file_id, info in incoming_files.items():
                if info['status'] == 'receiving' and (now - info['last_chunk_time']) > FILE_TRANSFER_TIMEOUT:
                    log(f"File transfer for {info['filename']} (ID: {file_id}) timed out.", level="WARNING")
                    files_to_remove.append(file_id)
                elif info['status'] in ['complete', 'ignored'] and (now - info['last_chunk_time']) > 3600: # Keep completed/ignored for an hour
                    files_to_remove.append(file_id)
            
            for file_id in files_to_remove:
                del incoming_files[file_id]
                log(f"Cleaned up file transfer context for {file_id}")

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
            elif msg_type == "PROFILE":
                uid_with_ip = headers.get("USER_ID", "").split("@")[0]
                uid = uid_with_ip
                name = headers.get("DISPLAY_NAME", uid)
                status = headers.get("STATUS", "")
                avatar_type = headers.get("AVATAR_TYPE")
                avatar_data = headers.get("AVATAR_DATA")

                peers[uid] = name
                peers_status[uid] = {
                    "name": name, 
                    "status": status,
                    "avatar_type": avatar_type,
                    "avatar_data": avatar_data
                }
                
            elif msg_type == "DM":
                frm = headers.get("FROM", "").split("@")[0]
                frm_name = peers.get(frm, frm)
                content = headers.get("CONTENT", "")
                clear_input()
                print(f"[← {frm_name}] {content}") 
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
                
            elif msg_type == "UNFOLLOW_NOTIFY":
                uid = headers.get("USER_ID", "")
                name = headers.get("DISPLAY_NAME", uid)
                if uid in followers:
                    del followers[uid]
                clear_input()
                print(f"👤 {name} unfollowed you!")
                print_prompt()

            elif msg_type == "TICTACTOE_INVITE":
                from_user = headers.get("FROM", "").split("@")[0]
                gameid = headers.get("GAMEID", "")
                symbol = headers.get("SYMBOL", "X")
                clear_input()
                print(f"{from_user} is inviting you to play tic-tac-toe (Game ID: {gameid}, They are {symbol})")
                print_prompt()

            elif msg_type == "TICTACTOE_MOVE":
                gameid = headers.get("GAMEID")
                pos = headers.get("POS")
                clear_input()
                print(f"🎮 Move in {gameid}: position {pos}")
                print_prompt()

            # --- New File Transfer Message Handling ---
            elif msg_type == "FILE_OFFER":
                file_id = headers.get("FILEID")
                from_user = headers.get("FROM", "").split("@")[0]
                filename = headers.get("FILENAME")
                filesize = int(headers.get("FILESIZE", 0))
                filetype = headers.get("FILETYPE", "application/octet-stream")
                description = headers.get("DESCRIPTION", "No description")
                total_chunks = int(headers.get("TOTAL_CHUNKS", 0)) # Added TOTAL_CHUNKS to offer for convenience

                clear_input()
                print(f"User {peers.get(from_user, from_user)} is sending you a file:")
                print(f"  Filename: {filename}")
                print(f"  Size: {filesize} bytes")
                print(f"  Type: {filetype}")
                print(f"  Description: {description}")
                print(f"  (File ID: {file_id})")
                print("Do you accept? (yes/no)")
                print_prompt() # Re-prompt for user input

                with file_transfer_lock:
                    if file_id in incoming_files and incoming_files[file_id]['status'] != 'pending':
                        log(f"Already handling file offer for {file_id}. Ignoring duplicate.", level="WARNING")
                        return

                    incoming_files[file_id] = {
                        "from_user": from_user,
                        "filename": filename,
                        "filesize": filesize,
                        "filetype": filetype,
                        "total_chunks": total_chunks,
                        "received_chunks": {}, # Store {index: data}
                        "last_chunk_time": time.time(),
                        "status": "pending", # 'pending', 'receiving', 'complete', 'ignored'
                        "temp_data": [] # To store chunks in order
                    }
                
                # The user will type 'yes' or 'no' in the next input loop iteration
                # We don't automatically accept here.

            elif msg_type == "FILE_CHUNK":
                file_id = headers.get("FILEID")
                chunk_index = int(headers.get("CHUNK_INDEX", -1))
                total_chunks = int(headers.get("TOTAL_CHUNKS", -1))
                chunk_data_b64 = headers.get("DATA", "")
                from_user = headers.get("FROM", "").split("@")[0]

                with file_transfer_lock:
                    if file_id not in incoming_files:
                        log(f"Received chunk for unknown file ID {file_id}. Ignoring.", level="WARNING")
                        return
                    
                    file_info = incoming_files[file_id]

                    if file_info['status'] == 'ignored':
                        log(f"Received chunk for ignored file {file_id}. Discarding.", level="INFO")
                        return
                    
                    if file_info['status'] == 'pending':
                        # This means the user hasn't responded to the offer yet.
                        # For simplicity, we'll auto-ignore if chunks arrive before acceptance.
                        # A more robust system would queue or prompt again.
                        log(f"Received chunk for pending file {file_id}. Auto-ignoring.", level="WARNING")
                        file_info['status'] = 'ignored'
                        return

                    if file_info['status'] == 'receiving':
                        if chunk_index in file_info['received_chunks']:
                            log(f"Duplicate chunk {chunk_index} for file {file_id}. Ignoring.", level="INFO")
                            return
                        
                        try:
                            decoded_data = base64.b64decode(chunk_data_b64)
                            file_info['received_chunks'][chunk_index] = decoded_data
                            file_info['last_chunk_time'] = time.time()
                            # log(f"Received chunk {chunk_index}/{total_chunks} for {file_info['filename']}")

                            if len(file_info['received_chunks']) == file_info['total_chunks']:
                                # All chunks received! Reassemble the file.
                                log(f"All chunks received for {file_info['filename']} (ID: {file_id}). Reassembling...")
                                
                                # Sort chunks by index and concatenate
                                full_data = b''
                                for i in range(file_info['total_chunks']):
                                    if i in file_info['received_chunks']:
                                        full_data += file_info['received_chunks'][i]
                                    else:
                                        log(f"Missing chunk {i} for file {file_id}! Cannot reassemble.", level="ERROR")
                                        file_info['status'] = 'failed' # Mark as failed
                                        return # Exit, cannot complete

                                # Save the file
                                try:
                                    # Ensure a 'downloads' directory exists
                                    download_dir = "downloads"
                                    os.makedirs(download_dir, exist_ok=True)
                                    save_path = os.path.join(download_dir, file_info['filename'])
                                    with open(save_path, 'wb') as f:
                                        f.write(full_data)
                                    file_info['status'] = 'complete'
                                    clear_input()
                                    print(f"File transfer of '{file_info['filename']}' is complete. Saved to '{save_path}'")
                                    print_prompt()

                                    # Notify sender
                                    send_file_received(from_user, file_id, "COMPLETE")

                                except Exception as e:
                                    log(f"Error saving file {file_info['filename']}: {e}", level="ERROR")
                                    file_info['status'] = 'failed'
                            # else:
                                # clear_input()
                                # print(f"Receiving {file_info['filename']}: {len(file_info['received_chunks'])}/{file_info['total_chunks']} chunks")
                                # print_prompt()

                        except Exception as e:
                            log(f"Error decoding or processing chunk for file {file_id}: {e}", level="ERROR")
                            file_info['status'] = 'failed'

            elif msg_type == "FILE_RECEIVED":
                file_id = headers.get("FILEID")
                status = headers.get("STATUS")
                from_user = headers.get("FROM", "").split("@")[0] # The receiver of the file

                with file_transfer_lock:
                    if file_id in outgoing_files:
                        outgoing_files[file_id]['status'] = status
                        clear_input()
                        print(f"✅ File '{outgoing_files[file_id]['filename']}' (ID: {file_id}) was {status} by {peers.get(from_user, from_user)}.")
                        print_prompt()
                    else:
                        log(f"Received FILE_RECEIVED for unknown outgoing file ID {file_id}.", level="WARNING")
            # --- End New File Transfer Message Handling ---

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
    
def send_profile():
    global STATUS, AVATAR_TYPE, AVATAR_ENCODING, AVATAR_DATA
    
    msg_parts = [
        f"TYPE: PROFILE",
        f"USER_ID: {USER_ID}@{get_my_ip()}",
        f"DISPLAY_NAME: {DISPLAY_NAME}",
        f"STATUS: {STATUS}"
    ]
    
    # Add avatar fields if available
    if AVATAR_TYPE and AVATAR_ENCODING and AVATAR_DATA:
        msg_parts.extend([
            f"AVATAR_TYPE: {AVATAR_TYPE}",
            f"AVATAR_ENCODING: {AVATAR_ENCODING}",
            f"AVATAR_DATA: {AVATAR_DATA}"
        ])
       
    msg = "\n".join(msg_parts)
    send_udp(msg)
    
    peers_status[USER_ID] = {
        "name": DISPLAY_NAME,
        "status": STATUS,
        "avatar_type": AVATAR_TYPE,
        "avatar_data": AVATAR_DATA
    }


def profile_broadcast_loop():
    while running:
        send_profile()
        time.sleep(300)
        clear_input()
        print("\n> 🟢 Status:")
        for uid, info in peers_status.items():
            name = info.get("name", uid)
            status = info.get("status", "")
            
            avatar_type = info.get("avatar_type")
            if avatar_type:
                avatar_indicator = f" 🖼️({avatar_type})"
            else:
                avatar_indicator = ""
            
            print(f" 👤 {name} ({uid}) is now: '{status}'{avatar_indicator}")
        print_prompt()
        
def send_dm(to_user: str, message: str):
    if to_user not in peers:
        print(f"❌ User '{to_user}' not found.")
        return
    
    current_timestamp = int(time.time())
    message_id = f"{USER_ID}-{current_timestamp}-{random.randint(1000,9999)}" # Simple unique ID
    token_expiry = current_timestamp + 3600
    token = f"{USER_ID}|{token_expiry}|chat"

    msg = (
        f"TYPE: DM\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {to_user}\n"
        f"CONTENT: {message}\n" # Use CONTENT field
        f"TIMESTAMP: {current_timestamp}\n"
        f"MESSAGE_ID: {message_id}\n"
        f"TOKEN: {token}"
    )
    send_udp(msg)
    target_name = peers.get(to_user, to_user)
    clear_input()
    print(f"[→ {target_name}] {message}") # Print original message for sent confirmation
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

def resolve_user_id(name: str) -> str:
    """Resolve display name to USER_ID (case-insensitive)"""
    name = name.strip().lower()
    for uid, display in peers.items():
        if display.lower() == name:
            return uid
    return None

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

def unfollow(name: str):
    name = name.strip().lower()
    match_uid = None
    for uid, display in peers.items():
        if display.lower() == name:
            match_uid = uid
            break

    if not match_uid:
        print(f"❌ No peer found with display name '{name}'")
        return
    if match_uid not in following:
        print(f"❌ Not currently following {peers[match_uid]}")
        return

    msg = (
        f"TYPE: UNFOLLOW\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {match_uid}"
    )
    send_udp(msg)
    
    del following[match_uid]
    print(f"✅ Unfollowed {peers[match_uid]}")

def tictactoe_invite(to_user: str):
    if to_user not in peers:
        print(f"❌ User '{to_user}' not found.")
        return
    
    gameid = f"game-{random.randint(1000, 9999)}"
    player_x = USER_ID
    player_o = to_user
    symbol = "X"  # inviter always X

    msg = (
        f"TYPE: TICTACTOE_INVITE\n"
        f"GAMEID: {gameid}\n"
        f"PLAYER_X: {player_x}\n"
        f"PLAYER_O: {player_o}\n"
        f"SYMBOL: {symbol}\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {to_user}"
    )
    send_udp(msg)

    clear_input()
    print(f"🎮 Game invite sent to {peers[to_user]} ({to_user})")
    print_prompt()

def send_file_offer(to_user_id: str, file_path: str, description: str = ""):
    if to_user_id not in peers:
        print(f"❌ User '{to_user_id}' not found.")
        return

    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return
    
    if not os.path.isfile(file_path):
        print(f"❌ Path is not a file: {file_path}")
        return

    filename = os.path.basename(file_path)
    filesize = os.path.getsize(file_path)
    file_id = f"{USER_ID}-{int(time.time())}-{random.randint(1000,9999)}"
    
    # Determine file type (simple guess based on extension)
    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.txt': 'text/plain',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.pdf': 'application/pdf',
        '.zip': 'application/zip',
        '.mp3': 'audio/mpeg',
        '.mp4': 'video/mp4',
    }
    filetype = mime_types.get(ext, 'application/octet-stream')

    # Calculate total chunks
    total_chunks = (filesize + FILE_CHUNK_SIZE - 1) // FILE_CHUNK_SIZE

    current_timestamp = int(time.time())
    token_expiry = current_timestamp + 3600 # Token valid for 1 hour
    token = f"{USER_ID}|{token_expiry}|file"

    msg = (
        f"TYPE: FILE_OFFER\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {to_user_id}\n"
        f"FILENAME: {filename}\n"
        f"FILESIZE: {filesize}\n"
        f"FILETYPE: {filetype}\n"
        f"FILEID: {file_id}\n"
        f"TOTAL_CHUNKS: {total_chunks}\n" # Include total chunks in offer
        f"DESCRIPTION: {description}\n"
        f"TIMESTAMP: {current_timestamp}\n"
        f"TOKEN: {token}"
    )
    send_udp(msg)

    with file_transfer_lock:
        outgoing_files[file_id] = {
            "filename": filename,
            "file_path": file_path,
            "to_user": to_user_id,
            "total_chunks": total_chunks,
            "sent_chunks": 0,
            "status": "offered", # 'offered', 'sending', 'complete', 'failed'
            "token": token # Store token for sending chunks
        }
    
    clear_input()
    print(f"📤 File offer sent for '{filename}' to {peers.get(to_user_id, to_user_id)}. Waiting for acceptance...")
    print_prompt()

def send_file_chunks(file_id: str):
    with file_transfer_lock:
        if file_id not in outgoing_files or outgoing_files[file_id]['status'] != 'sending':
            log(f"Not ready to send chunks for file ID {file_id}.", level="WARNING")
            return
        
        file_info = outgoing_files[file_id]
        file_path = file_info['file_path']
        to_user_id = file_info['to_user']
        total_chunks = file_info['total_chunks']
        token = file_info['token']

    try:
        with open(file_path, 'rb') as f:
            for i in range(total_chunks):
                f.seek(i * FILE_CHUNK_SIZE)
                chunk_data = f.read(FILE_CHUNK_SIZE)
                if not chunk_data:
                    break 

                encoded_chunk = base64.b64encode(chunk_data).decode('ascii')

                msg = (
                    f"TYPE: FILE_CHUNK\n"
                    f"FROM: {USER_ID}@{get_my_ip()}\n"
                    f"TO: {to_user_id}\n"
                    f"FILEID: {file_id}\n"
                    f"CHUNK_INDEX: {i}\n"
                    f"TOTAL_CHUNKS: {total_chunks}\n"
                    f"CHUNK_SIZE: {len(chunk_data)}\n" 
                    f"TOKEN: {token}\n"
                    f"DATA: {encoded_chunk}"
                )
                send_udp(msg)
                
                with file_transfer_lock:
                    outgoing_files[file_id]['sent_chunks'] = i + 1
                
                # Add a small delay to avoid overwhelming the network/server
                time.sleep(0.01) 
                
                clear_input()
                print(f"Sending '{file_info['filename']}': {i+1}/{total_chunks} chunks sent.")
                print_prompt()

        with file_transfer_lock:
            outgoing_files[file_id]['status'] = 'complete'
        clear_input()
        print(f"✅ All chunks for '{file_info['filename']}' (ID: {file_id}) sent.")
        print_prompt()

    except Exception as e:
        log(f"Error sending file chunks for {file_id}: {e}", level="ERROR")
        with file_transfer_lock:
            outgoing_files[file_id]['status'] = 'failed'

def send_file_received(to_user_id: str, file_id: str, status: str):
    msg = (
        f"TYPE: FILE_RECEIVED\n"
        f"FROM: {USER_ID}@{get_my_ip()}\n"
        f"TO: {to_user_id}\n"
        f"FILEID: {file_id}\n"
        f"STATUS: {status}\n"
        f"TIMESTAMP: {int(time.time())}"
    )
    send_udp(msg)


def show_help():
    print("""
Commands:
  post <msg>               - Post a message to followers
  status <msg>             - Profile status
  avatar <path>            - Set avatar image (or 'none' to remove)
  dm <user> <msg>          - Send DM
  follow <user>            - Follow user (server logs)
  unfollow <user>          - Unfollow user
  like <postid>            - Like a post (server logs)
  file <user> <path> [desc]- Send file
  accept <fileid>          - Accept incoming file transfer
  ignore <fileid>          - Ignore incoming file transfer
  game <user>              - Invite to TTT (not implemented yet)
  move <gameid> <0-8>      - Make TTT move
  group create <id> <mems> - Create group (comma list)
  group send <id> <msg>    - Send group message
  peers                    - List peers
  help                     - Show this
  exit                     - Quit
""")

def main():
    global sock, USER_ID, DISPLAY_NAME, running, STATUS

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
    send_profile()  

    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=profile_broadcast_loop, daemon=True).start()
    threading.Thread(target=file_transfer_cleanup_loop, daemon=True).start() # Start file cleanup thread


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
                    parts = cmd.split(" ", 1)
                    
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
                    elif c == "status" and len(parts) > 1:
                        STATUS = parts[1]
                        send_profile() 
                        print(f"✅ Status updated to: '{STATUS}'")
                    elif c == "avatar" and len(parts) > 1:
                        global AVATAR_TYPE, AVATAR_ENCODING, AVATAR_DATA, AVATAR_PATH
                        avatar_path = parts[1].strip()
                        if avatar_path.lower() == "none":
                            AVATAR_TYPE = None
                            AVATAR_ENCODING = None
                            AVATAR_DATA = None
                            AVATAR_PATH = None
                            print("✅ Avatar removed")
                        else:
                            mime_type, encoding, data = load_avatar(avatar_path)
                            if mime_type and encoding and data:
                                AVATAR_TYPE = mime_type
                                AVATAR_ENCODING = encoding
                                AVATAR_DATA = data
                                AVATAR_PATH = avatar_path
                                print(f"✅ Avatar loaded: {avatar_path} ({mime_type})")
                            else:
                                print("❌ Failed to load avatar")
                        send_profile()

                    elif c == "dm" and len(parts) > 1: 
                        dm_parts = parts[1].split(" ", 1)
                        if len(dm_parts) >= 2:
                            user = dm_parts[0]
                            message = dm_parts[1]
                            send_dm(user, message)
                    elif c == "follow" and len(parts) > 1:
                        follow(parts[1])
                    elif c == "unfollow" and len(parts) > 1:
                        unfollow(parts[1])
                    elif c == "like" and len(parts) > 1:
                        print(f"❤️ Liked post {parts[1]}")
                    
                    # --- File Transfer Commands ---
                    elif c == "file" and len(parts) > 1:
                        file_args = parts[1].split(" ", 2) # user, path, [description]
                        if len(file_args) < 2:
                            print("❌ Usage: file <user> <path> [description]")
                        else:
                            to_user = file_args[0]
                            file_path = file_args[1]
                            description = file_args[2] if len(file_args) > 2 else ""
                            send_file_offer(to_user, file_path, description)

                    elif c == "accept" and len(parts) > 1:
                        file_id_to_accept = parts[1].strip()
                        with file_transfer_lock:
                            if file_id_to_accept in incoming_files:
                                file_info = incoming_files[file_id_to_accept]
                                if file_info['status'] == 'pending':
                                    file_info['status'] = 'receiving'
                                    clear_input()
                                    print(f"✅ Accepted file '{file_info['filename']}' (ID: {file_id_to_accept}). Waiting for chunks...")
                                    print_prompt()
                                else:
                                    print(f"❌ File offer for {file_id_to_accept} is not pending (status: {file_info['status']}).")
                            else:
                                print(f"❌ No pending file offer found for ID: {file_id_to_accept}")

                    elif c == "ignore" and len(parts) > 1:
                        file_id_to_ignore = parts[1].strip()
                        with file_transfer_lock:
                            if file_id_to_ignore in incoming_files:
                                file_info = incoming_files[file_id_to_ignore]
                                if file_info['status'] == 'pending' or file_info['status'] == 'receiving':
                                    file_info['status'] = 'ignored'
                                    clear_input()
                                    print(f"🚫 Ignored file '{file_info['filename']}' (ID: {file_id_to_ignore}).")
                                    print_prompt()
                                    # Optionally, notify sender that it was ignored
                                    send_file_received(file_info['from_user'], file_id_to_ignore, "IGNORED")
                                else:
                                    print(f"❌ File offer for {file_id_to_ignore} cannot be ignored (status: {file_info['status']}).")
                            else:
                                print(f"❌ No pending file offer found for ID: {file_id_to_ignore}")
                    # --- End File Transfer Commands ---

                    elif c == "game" and len(parts) > 1:
                        tictactoe_invite(parts[1])
                    elif c == "move" and len(parts) > 2:
                        gameid, pos = parts[1], parts[2]
                        print(f"➡️ Move {pos} in game {gameid}")
                    elif c == "group" and len(parts) > 1:
                        subcmd = parts[1].split(" ", 1)
                        subcommand = subcmd[0].lower()

                        if subcommand == "create" and len(subcmd) > 1:
                            args = subcmd[1].strip().split(" ", 1)
                            if len(args) < 1:
                                print("❌ Usage: group create <id> [members]")
                                continue
                            gid = args[0]
                            member_names = args[1].split(",") if len(args) > 1 else []
                            member_names = [name.strip() for name in member_names if name.strip()]

                            resolved_members = []
                            unresolved = []
                            for name in member_names:
                                uid = resolve_user_id(name)
                                if uid:
                                    resolved_members.append(uid)
                                else:
                                    unresolved.append(name)

                            groups[gid] = resolved_members

                            clear_input()
                            print(f"👥 Created group '{gid}' with {len(resolved_members)} members")
                            if unresolved:
                                print(f"❌ Could not find: {', '.join(unresolved)}")
                            print_prompt()

                        elif subcommand == "send" and len(subcmd) > 1:
                            args = subcmd[1].strip().split(" ", 1)
                            if len(args) < 2:
                                print("❌ Usage: group send <id> <message>")
                                continue
                            gid, message = args[0], args[1]

                            if gid not in groups:
                                print(f"❌ Group '{gid}' does not exist.")
                                continue

                            member_ids = groups[gid]
                            if not member_ids:
                                print(f"📭 Group '{gid}' is empty. No messages sent.")

                            sent_count = 0
                            for uid in member_ids:
                                if uid == USER_ID:
                                    continue
                                if uid in peers:
                                    send_dm(uid, message)
                                    sent_count += 1
                                else:
                                    log(f"Skipping DM to offline user: {uid}", level="WARN")

                            clear_input()
                            print(f"📤 Sent message to {sent_count} members of group '{gid}'")
                            print_prompt()
                        else:
                            print("❌ Invalid group command. Use: group create <id> [mems] OR group send <id> <msg>")
                    elif c == "debug":
                        print("🔍 Debug Info:")
                        print(f"Following: {following}")
                        print(f"Followers: {followers}")
                        print(f"Incoming Files: {incoming_files}")
                        print(f"Outgoing Files: {outgoing_files}")
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

            # Check for accepted file transfers to start sending chunks
            with file_transfer_lock:
                for file_id, file_info in list(outgoing_files.items()): # Use list() to iterate over a copy
                    if file_info['status'] == 'offered' and file_info['to_user'] in incoming_files and incoming_files[file_info['to_user']]['status'] == 'receiving':
                       pass

                    if file_info['status'] == 'offered':
                        file_info['status'] = 'sending' # Assume accepted for now
                        threading.Thread(target=send_file_chunks, args=(file_id,), daemon=True).start()


            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        sock.close()
        print("\n👋 Goodbye!")

if __name__ == "__main__":
    main()

