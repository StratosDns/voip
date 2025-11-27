import socket
import threading
import json
import argparse
import time

# extension -> {conn, addr, state, peer, remote}
clients = {}
lock = threading.Lock()

# extensions currently in an IVR session
ivr_sessions = set()

# trunk sockets
trunk_outbound = None      # socket we use to SEND trunk messages
trunk_outbound_lock = threading.Lock()


def send_json(conn, obj):
    """Send a JSON object terminated by newline."""
    try:
        data = json.dumps(obj).encode("utf-8") + b"\n"
        conn.sendall(data)
    except Exception:
        pass


def get_client(ext):
    with lock:
        return clients.get(ext)


def set_state(ext, state, peer=None, remote=False):
    with lock:
        if ext in clients:
            clients[ext]["state"] = state
            clients[ext]["peer"] = peer
            clients[ext]["remote"] = remote


def trunk_send(obj):
    """Send a JSON message on the outbound trunk connection (if available)."""
    global trunk_outbound
    with trunk_outbound_lock:
        conn = trunk_outbound
    if conn is not None:
        send_json(conn, obj)


# ============================================================
#  LOCAL CALL HANDLING
# ============================================================

def handle_local_call(caller_ext, callee_ext):
    caller = get_client(caller_ext)
    callee = get_client(callee_ext)

    if caller is None:
        return

    if caller["state"] != "idle":
        send_json(caller["conn"], {
            "type": "error",
            "reason": "Δεν μπορείς να ξεκινήσεις νέα κλήση ενώ είσαι σε κλήση."
        })
        return

    if callee is None:
        send_json(caller["conn"], {
            "type": "error",
            "reason": f"Το extension {callee_ext} δεν είναι καταχωρημένο."
        })
        return

    if callee["state"] == "idle":
        set_state(caller_ext, "in_call", peer=callee_ext, remote=False)
        set_state(callee_ext, "in_call", peer=caller_ext, remote=False)

        send_json(caller["conn"], {
            "type": "call_proceeding",
            "to": callee_ext
        })
        send_json(callee["conn"], {
            "type": "incoming_call",
            "from": caller_ext
        })
    else:
        # Call waiting behaviour
        send_json(caller["conn"], {
            "type": "busy",
            "to": callee_ext
        })
        send_json(callee["conn"], {
            "type": "incoming_call_waiting",
            "from": caller_ext
        })


# ============================================================
#  TRUNK CALL HANDLING
# ============================================================

def handle_outgoing_trunk_call(caller_ext, callee_ext):
    """Caller on this PBX wants to call a remote extension via trunk."""
    caller = get_client(caller_ext)
    if caller is None:
        return

    if caller["state"] != "idle":
        send_json(caller["conn"], {
            "type": "error",
            "reason": "Δεν μπορείς να ξεκινήσεις νέα κλήση ενώ είσαι σε κλήση."
        })
        return

    set_state(caller_ext, "in_call", peer=callee_ext, remote=True)

    send_json(caller["conn"], {
        "type": "call_proceeding",
        "to": callee_ext
    })

    trunk_send({
        "type": "trunk_call",
        "from": caller_ext,
        "to": callee_ext
    })


def handle_incoming_trunk_call(data):
    """Received trunk_call from remote PBX."""
    from_ext = data["from"]
    to_ext = data["to"]

    callee = get_client(to_ext)
    if callee is None:
        # Destination not registered -> remote caller should see busy and reset state
        trunk_send({
            "type": "trunk_busy",
            "from": to_ext,
            "to": from_ext
        })
        return

    if callee["state"] == "idle":
        set_state(to_ext, "in_call", peer=from_ext, remote=True)
        send_json(callee["conn"], {
            "type": "incoming_call",
            "from": from_ext
        })
    else:
        send_json(callee["conn"], {
            "type": "incoming_call_waiting",
            "from": from_ext
        })
        trunk_send({
            "type": "trunk_busy",
            "from": to_ext,
            "to": from_ext
        })


def handle_trunk_busy(data):
    """Remote PBX reports that the call could not be established."""
    to_ext = data["to"]
    frm = data["from"]
    caller = get_client(to_ext)
    if caller:
        # Reset local state to idle and notify busy
        set_state(to_ext, "idle", peer=None, remote=False)
        send_json(caller["conn"], {
            "type": "busy",
            "to": frm
        })


def handle_answer(ext):
    me = get_client(ext)
    if me is None:
        return

    if me["state"] != "in_call":
        send_json(me["conn"], {
            "type": "error",
            "reason": "Δεν υπάρχει κλήση για απάντηση."
        })
        return

    peer_ext = me["peer"]
    remote = me["remote"]

    if not remote:
        peer = get_client(peer_ext)
        if peer:
            send_json(peer["conn"], {
                "type": "call_answered",
                "by": ext
            })
        send_json(me["conn"], {
            "type": "call_answered",
            "by": peer_ext
        })
    else:
        # Inform remote PBX that the local callee has answered
        trunk_send({
            "type": "trunk_call_answered",
            "from": ext,
            "to": peer_ext
        })
        send_json(me["conn"], {
            "type": "call_answered",
            "by": peer_ext
        })


def handle_trunk_answer(data):
    """Remote PBX informs that a trunk call has been answered."""
    caller_ext = data["to"]   # local caller
    receiver_ext = data["from"]  # remote party

    caller = get_client(caller_ext)
    if caller:
        send_json(caller["conn"], {
            "type": "call_answered",
            "by": receiver_ext
        })


def handle_hangup(ext):
    me = get_client(ext)
    if me is None or me["state"] != "in_call":
        return

    peer_ext = me["peer"]
    remote = me["remote"]

    # Reset local
    set_state(ext, "idle", peer=None, remote=False)
    send_json(me["conn"], {
        "type": "hangup",
        "by": ext
    })

    if not remote:
        peer = get_client(peer_ext)
        if peer:
            set_state(peer_ext, "idle", peer=None, remote=False)
            send_json(peer["conn"], {
                "type": "hangup",
                "by": ext
            })
    else:
        # Remote peer is on the other PBX
        trunk_send({
            "type": "trunk_hangup",
            "from": ext,
            "to": peer_ext
        })


def handle_trunk_hangup(data):
    to_ext = data["to"]
    frm = data["from"]
    local = get_client(to_ext)
    if local:
        set_state(to_ext, "idle", peer=None, remote=False)
        send_json(local["conn"], {
            "type": "hangup",
            "by": frm
        })


# ============================================================
#  CHAT HANDLING
# ============================================================

def handle_chat(ext, text):
    me = get_client(ext)
    if me is None:
        return

    if me["state"] != "in_call":
        send_json(me["conn"], {
            "type": "error",
            "reason": "Δεν υπάρχει ενεργή κλήση για chat."
        })
        return

    peer_ext = me["peer"]
    remote = me["remote"]

    if not remote:
        peer = get_client(peer_ext)
        if peer:
            send_json(peer["conn"], {
                "type": "chat",
                "from": ext,
                "text": text
            })
            send_json(me["conn"], {
                "type": "chat_sent",
                "to": peer_ext
            })
    else:
        trunk_send({
            "type": "trunk_chat",
            "from": ext,
            "to": peer_ext,
            "text": text
        })
        send_json(me["conn"], {
            "type": "chat_sent",
            "to": peer_ext
        })


def handle_trunk_chat(data):
    to_ext = data["to"]
    frm = data["from"]
    text = data["text"]

    peer = get_client(to_ext)
    if peer:
        send_json(peer["conn"], {
            "type": "chat",
            "from": frm,
            "text": text
        })


# ============================================================
#  IVR HANDLING
# ============================================================

def ivr_start(ext, ivr_ext, local_prefix):
    """Start an IVR session for extension ext."""
    me = get_client(ext)
    if me is None:
        return

    if me["state"] != "idle":
        send_json(me["conn"], {
            "type": "error",
            "reason": "Δεν μπορείς να καλέσεις IVR ενώ είσαι σε κλήση."
        })
        return

    ivr_sessions.add(ext)

    center_label = "A" if ivr_ext == "5000" else "B" if ivr_ext == "7000" else "Local"
    menu_text = (
        f"--- IVR Center {center_label} ({ivr_ext}) ---\n"
        "0 → Πληροφορίες για το τηλεφωνικό κέντρο\n"
        f"1–9 → Κλήση στα {local_prefix}001–{local_prefix}009 (αν είναι καταχωρημένα)\n"
    )

    send_json(me["conn"], {
        "type": "ivr_message",
        "text": menu_text
    })


def ivr_choice(ext, digit, local_prefix, remote_prefix):
    if ext not in ivr_sessions:
        c = get_client(ext)
        if c:
            send_json(c["conn"], {
                "type": "error",
                "reason": "Δεν υπάρχει ενεργό IVR."
            })
        return

    ivr_sessions.discard(ext)

    me = get_client(ext)
    if me is None:
        return

    if me["state"] != "idle":
        send_json(me["conn"], {
            "type": "error",
            "reason": "Δεν μπορείς να ξεκινήσεις νέα κλήση από το IVR ενώ είσαι σε κλήση."
        })
        return

    # Digit 0 -> info
    if digit == "0":
        send_json(me["conn"], {
            "type": "ivr_info",
            "text": (
                "Το τηλεφωνικό κέντρο λειτουργεί Δευτέρα–Παρασκευή 09:00–17:00.\n"
                "Για τεχνική υποστήριξη επικοινωνήστε με τον διαχειριστή."
            )
        })
        return

    # Digits 1-9 -> local extensions prefix00d (e.g. 5003, 7003)
    if not digit.isdigit() or not (1 <= int(digit) <= 9):
        send_json(me["conn"], {
            "type": "error",
            "reason": "Μη έγκυρη επιλογή IVR."
        })
        return

    target_ext = f"{local_prefix}00{digit}"
    # Use normal call routing, but we know this is a local extension by design
    handle_call(ext, target_ext, local_prefix, remote_prefix, ivr_ext=None)


# ============================================================
#  GENERIC CALL ROUTER
# ============================================================

def handle_call(caller_ext, target_ext, local_prefix, remote_prefix, ivr_ext):
    """Route a call depending on prefix and IVR rules."""
    caller = get_client(caller_ext)
    if caller is None:
        return

    # Local IVR: allow both 'ivr' command and 'call 5000/7000' to start IVR
    if ivr_ext is not None and target_ext == ivr_ext:
        ivr_start(caller_ext, ivr_ext, local_prefix)
        return

    # Do not allow calling the remote IVR
    remote_ivr = None
    if remote_prefix:
        remote_ivr = f"{remote_prefix}000"

    if remote_ivr and target_ext == remote_ivr:
        send_json(caller["conn"], {
            "type": "error",
            "reason": "Δεν επιτρέπεται κλήση προς το IVR του άλλου τηλεφωνικού κέντρου."
        })
        return

    # Normal local / remote routing
    if target_ext.startswith(local_prefix):
        handle_local_call(caller_ext, target_ext)
    elif remote_prefix and target_ext.startswith(remote_prefix):
        handle_outgoing_trunk_call(caller_ext, target_ext)
    else:
        send_json(caller["conn"], {
            "type": "error",
            "reason": "Dial plan violation."
        })


# ============================================================
#  CLIENT THREAD
# ============================================================

def client_thread(conn, addr, local_prefix, remote_prefix, ivr_ext):
    ext = None
    print(f"[PBX] Σύνδεση από {addr}")
    f = conn.makefile("r", encoding="utf-8")

    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # Registration
            if mtype == "register":
                ext = msg.get("extension")
                if not ext:
                    continue
                with lock:
                    clients[ext] = {
                        "conn": conn,
                        "addr": addr,
                        "state": "idle",
                        "peer": None,
                        "remote": False
                    }
                print(f"[PBX] Extension {ext} registered από {addr}")
                send_json(conn, {
                    "type": "register_ok",
                    "extension": ext
                })
                continue

            if ext is None:
                # Ignore messages from unregistered clients
                continue

            if mtype == "call":
                dest = msg.get("to")
                if not dest:
                    continue
                handle_call(ext, dest, local_prefix, remote_prefix, ivr_ext)

            elif mtype == "answer":
                handle_answer(ext)

            elif mtype == "hangup":
                handle_hangup(ext)

            elif mtype == "ivr":
                dest = msg.get("to")
                # Only allow IVR calls to the local IVR number
                if dest == ivr_ext:
                    ivr_start(ext, ivr_ext, local_prefix)
                else:
                    c = get_client(ext)
                    if c:
                        send_json(c["conn"], {
                            "type": "error",
                            "reason": "Δεν επιτρέπεται κλήση IVR σε αυτόν τον αριθμό."
                        })

            elif mtype == "ivr_choice":
                digit = msg.get("digit")
                if digit is not None:
                    ivr_choice(ext, str(digit), local_prefix, remote_prefix)

            elif mtype == "chat":
                text = msg.get("text", "")
                handle_chat(ext, text)

    except Exception as e:
        print(f"[PBX] Σφάλμα client {addr}: {e}")

    finally:
        if ext:
            with lock:
                if ext in clients:
                    clients.pop(ext)
        conn.close()
        print(f"[PBX] Αποσύνδεση {ext}")


# ============================================================
#  TRUNK HANDLERS
# ============================================================

def trunk_inbound_thread(conn):
    """Handle messages coming FROM the remote PBX."""
    print("[PBX] TRUNK inbound συνδέθηκε.")
    f = conn.makefile("r", encoding="utf-8")

    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            if mtype == "trunk_call":
                handle_incoming_trunk_call(msg)
            elif mtype == "trunk_call_answered":
                handle_trunk_answer(msg)
            elif mtype == "trunk_hangup":
                handle_trunk_hangup(msg)
            elif mtype == "trunk_busy":
                handle_trunk_busy(msg)
            elif mtype == "trunk_chat":
                handle_trunk_chat(msg)

    except Exception as e:
        print(f"[PBX] Σφάλμα TRUNK inbound: {e}")
    finally:
        print("[PBX] TRUNK inbound έκλεισε.")


def trunk_outbound_connector(host, port):
    """Continuously try to connect outbound trunk to the remote PBX."""
    global trunk_outbound
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            with trunk_outbound_lock:
                trunk_outbound = s
            print("[PBX] TRUNK outbound συνδέθηκε.")
            f = s.makefile("r", encoding="utf-8")
            for _ in f:
                # We don't expect messages on the outbound side; inbound thread handles them.
                pass
        except Exception as e:
            print(f"[PBX] TRUNK outbound απέτυχε ({e}), retry σε 1sec")
            time.sleep(1)


# ============================================================
#  MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=["A", "B"], required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--remote-prefix", required=True)
    parser.add_argument("--ivr-ext", required=True)
    parser.add_argument("--trunk-remote-host", required=True)
    parser.add_argument("--trunk-remote-port", type=int, required=True)
    parser.add_argument("--trunk-listen-port", type=int, required=True)
    args = parser.parse_args()

    local_prefix = args.prefix
    remote_prefix = args.remote_prefix
    ivr_ext = args.ivr_ext

    # Start trunk listener
    def start_trunk_listener():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", args.trunk_listen_port))
        srv.listen(1)
        print(f"[PBX] TRUNK listener στο 0.0.0.0:{args.trunk_listen_port}")
        while True:
            conn, _ = srv.accept()
            threading.Thread(
                target=trunk_inbound_thread,
                args=(conn,),
                daemon=True
            ).start()

    threading.Thread(target=start_trunk_listener, daemon=True).start()

    # Start trunk outbound connector
    threading.Thread(
        target=trunk_outbound_connector,
        args=(args.trunk_remote_host, args.trunk_remote_port),
        daemon=True
    ).start()

    # Start PBX listener for clients
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(100)
    print(f"[PBX] {args.mode} listening on {args.host}:{args.port}")

    while True:
        conn, addr = srv.accept()
        threading.Thread(
            target=client_thread,
            args=(conn, addr, local_prefix, remote_prefix, ivr_ext),
            daemon=True
        ).start()


if __name__ == "__main__":
    main()
