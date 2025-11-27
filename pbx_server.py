import socket
import threading
import json
import argparse

# extension -> {"conn": conn, "addr": addr, "state": "idle"/"in_call", "peer": None}
clients = {}
lock = threading.Lock()

# Extensions with an active IVR "session"
ivr_sessions = set()

MENU_TEXT = (
    "Καλώς ήρθατε στο τηλεφωνικό κέντρο A.\n"
    "Πατήστε 1 για σύνδεση με 5001.\n"
    "Πατήστε 2 για σύνδεση με 5002.\n"
    "Πατήστε 3 για πληροφορίες.\n"
)


def send_msg(conn, msg: dict):
    """Send JSON message with newline terminator."""
    data = json.dumps(msg).encode("utf-8") + b"\n"
    try:
        conn.sendall(data)
    except OSError:
        pass


def get_client(ext):
    with lock:
        return clients.get(ext)


def set_state(ext, state, peer=None):
    with lock:
        if ext in clients:
            clients[ext]["state"] = state
            clients[ext]["peer"] = peer


def handle_call(msg):
    """Handle call request from caller to callee."""
    from_ext = msg.get("from")
    to_ext = msg.get("to")

    caller = get_client(from_ext)
    if not caller:
        return

    # Dial plan: only allow 5XXX
    if not (isinstance(to_ext, str) and len(to_ext) == 4 and to_ext.startswith("5")):
        send_msg(caller["conn"], {"type": "error", "reason": "Dial plan: μόνο 5XXX επιτρέπονται"})
        return

    # Caller must be idle
    if caller["state"] != "idle":
        send_msg(
            caller["conn"],
            {"type": "error", "reason": "Δεν μπορείς να ξεκινήσεις νέα κλήση ενώ είσαι σε κλήση."},
        )
        return

    # IVR on 5000
    if to_ext == "5000":
        start_ivr(from_ext)
        return

    callee = get_client(to_ext)
    if not callee:
        send_msg(caller["conn"], {"type": "error", "reason": f"Ο αριθμός {to_ext} δεν είναι καταχωρημένος"})
        return

    if callee["state"] == "idle":
        # Normal incoming call
        set_state(from_ext, "in_call", peer=to_ext)
        set_state(to_ext, "in_call", peer=from_ext)
        send_msg(callee["conn"], {"type": "incoming_call", "from": from_ext})
        send_msg(caller["conn"], {"type": "call_proceeding", "to": to_ext})
    else:
        # Call waiting: callee already in call
        send_msg(callee["conn"], {"type": "incoming_call_waiting", "from": from_ext})
        send_msg(caller["conn"], {"type": "busy", "to": to_ext})


def handle_answer(ext):
    """Client with extension ext answers an incoming call."""
    me = get_client(ext)
    if not me:
        return
    peer_ext = me.get("peer")
    if not peer_ext:
        send_msg(me["conn"], {"type": "error", "reason": "Δεν υπάρχει ενεργή κλήση για απάντηση"})
        return

    peer = get_client(peer_ext)
    if peer:
        send_msg(peer["conn"], {"type": "call_answered", "by": ext})
    send_msg(me["conn"], {"type": "call_answered", "by": ext})


def handle_hangup(ext):
    """Hang up an active call."""
    me = get_client(ext)
    if not me:
        return
    peer_ext = me.get("peer")
    set_state(ext, "idle", peer=None)

    # End any IVR session too
    with lock:
        ivr_sessions.discard(ext)

    if peer_ext:
        peer = get_client(peer_ext)
        if peer:
            set_state(peer_ext, "idle", peer=None)
            send_msg(peer["conn"], {"type": "hangup", "by": ext})

    send_msg(me["conn"], {"type": "hangup", "by": ext})


def start_ivr(caller_ext):
    """Start IVR for caller."""
    caller = get_client(caller_ext)
    if not caller:
        return

    # IVR not allowed if not idle
    if caller["state"] != "idle":
        send_msg(
            caller["conn"],
            {"type": "error", "reason": "Δεν μπορείς να καλέσεις το IVR ενώ είσαι σε κλήση."},
        )
        return

    with lock:
        ivr_sessions.add(caller_ext)

    send_msg(
        caller["conn"],
        {"type": "ivr_message", "text": MENU_TEXT},
    )


def handle_ivr_choice(ext, digit):
    """Process IVR digit selected by client ext."""
    with lock:
        has_session = ext in ivr_sessions

    if not has_session:
        client = get_client(ext)
        if client:
            send_msg(client["conn"], {"type": "error", "reason": "Δεν υπάρχει ενεργό IVR για αυτό το extension."})
        return

    # Single-use IVR session
    with lock:
        ivr_sessions.discard(ext)

    me = get_client(ext)
    if not me:
        return

    # IVR cannot start new call if not idle
    if me["state"] != "idle":
        send_msg(
            me["conn"],
            {"type": "error", "reason": "Δεν μπορείς να ξεκινήσεις νέα κλήση από το IVR ενώ είσαι σε κλήση."},
        )
        return

    if digit == "1":
        handle_call({"from": ext, "to": "5001"})
    elif digit == "2":
        handle_call({"from": ext, "to": "5002"})
    elif digit == "3":
        client = get_client(ext)
        if client:
            send_msg(
                client["conn"],
                {
                    "type": "ivr_info",
                    "text": "Το κέντρο Α λειτουργεί Δευτέρα-Παρασκευή 09:00-17:00.",
                },
            )
    else:
        client = get_client(ext)
        if client:
            send_msg(client["conn"], {"type": "error", "reason": "Μη έγκυρη επιλογή IVR"})


def handle_chat_message(ext, text):
    """Send chat message from ext to its peer during call."""
    me = get_client(ext)
    if not me:
        return

    if me["state"] != "in_call" or not me.get("peer"):
        send_msg(
            me["conn"],
            {
                "type": "error",
                "reason": "Δεν μπορείς να στείλεις μήνυμα αν δεν είσαι σε ενεργή κλήση.",
            },
        )
        return

    peer_ext = me["peer"]
    peer = get_client(peer_ext)
    if not peer:
        send_msg(
            me["conn"],
            {
                "type": "error",
                "reason": "Ο συνομιλητής δεν είναι πλέον διαθέσιμος.",
            },
        )
        return

    send_msg(
        peer["conn"],
        {"type": "chat", "from": ext, "text": text},
    )
    send_msg(me["conn"], {"type": "chat_sent", "to": peer_ext})


def handle_client(conn, addr):
    """Per-client thread."""
    ext = None
    print(f"[PBX] Νέα σύνδεση από {addr}")
    f = conn.makefile("r")
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            try:
                mtype = msg.get("type")

                if mtype == "register":
                    ext = msg.get("extension")
                    if not ext:
                        send_msg(conn, {"type": "error", "reason": "Λείπει το extension"})
                        continue
                    with lock:
                        clients[ext] = {"conn": conn, "addr": addr, "state": "idle", "peer": None}
                        ivr_sessions.discard(ext)
                    print(f"[PBX] Extension {ext} registered από {addr}")
                    send_msg(conn, {"type": "register_ok", "extension": ext})

                elif mtype == "call":
                    handle_call(msg)

                elif mtype == "answer":
                    if ext:
                        handle_answer(ext)

                elif mtype == "hangup":
                    if ext:
                        handle_hangup(ext)

                elif mtype == "ivr_choice":
                    digit = str(msg.get("digit", ""))
                    if ext:
                        handle_ivr_choice(ext, digit)

                elif mtype == "chat":
                    text = str(msg.get("text", ""))
                    if ext and text:
                        handle_chat_message(ext, text)

            except Exception as e:
                print(f"[PBX] Σφάλμα στο client {ext} ({addr}): {e}")

    finally:
        if ext:
            print(f"[PBX] Extension {ext} αποσυνδέθηκε")
            with lock:
                clients.pop(ext, None)
                ivr_sessions.discard(ext)
        conn.close()


def run_server(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(10)
    print(f"[PBX] Server ακούει στο {host}:{port}")

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


def main():
    parser = argparse.ArgumentParser(description="Απλό PBX server σε Python")
    parser.add_argument("--host", default="0.0.0.0", help="Διεύθυνση ακρόασης (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port ακρόασης (default 5000)")
    args = parser.parse_args()

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
