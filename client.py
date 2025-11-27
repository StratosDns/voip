import socket
import threading
import json
import argparse
import sys


def send_msg(conn, msg: dict):
    data = json.dumps(msg).encode("utf-8") + b"\n"
    try:
        conn.sendall(data)
    except OSError:
        print("[CLIENT] Αποτυχία αποστολής (η σύνδεση ίσως έκλεισε).")


def receiver_thread(conn):
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

            mtype = msg.get("type")
            if mtype == "register_ok":
                print(f"[SERVER] Καταχωρήθηκες ως extension {msg.get('extension')}")
            elif mtype == "incoming_call":
                print(f"[SERVER] Νέα κλήση από {msg.get('from')}. Πληκτρολόγησε 'answer' για απάντηση.")
            elif mtype == "incoming_call_waiting":
                print(f"[SERVER] CALL WAITING: Νέα κλήση από {msg.get('from')} (ήδη σε κλήση).")
            elif mtype == "call_proceeding":
                print(f"[SERVER] Γίνεται κλήση προς {msg.get('to')}...")
            elif mtype == "call_answered":
                print(f"[SERVER] Η κλήση απαντήθηκε από {msg.get('by')}.")
            elif mtype == "hangup":
                print(f"[SERVER] Η κλήση τερματίστηκε από {msg.get('by')}.")
            elif mtype == "error":
                print(f"[SERVER][ERROR] {msg.get('reason')}")
            elif mtype == "ivr_message":
                print("\n--- IVR ---")
                print(msg.get("text", ""))
                print("Γράψε 'digit <n>' για να επιλέξεις (π.χ. digit 1)\n")
            elif mtype == "ivr_info":
                print(f"[IVR] {msg.get('text')}")
            elif mtype == "chat":
                print(f"[CHAT] {msg.get('from')}: {msg.get('text')}")
            elif mtype == "chat_sent":
                print(f"[CHAT] Μήνυμα στάλθηκε προς {msg.get('to')}")
            else:
                print(f"[SERVER] Μήνυμα: {msg}")
    except Exception as e:
        print(f"[CLIENT] Receiver error: {e}")
    finally:
        print("[CLIENT] Η σύνδεση με το PBX τερματίστηκε (receiver).")


def main():
    parser = argparse.ArgumentParser(description="Simple VoIP client")
    parser.add_argument("--server-ip", default="127.0.0.1")
    parser.add_argument("--server-port", type=int, default=5000)
    parser.add_argument("--extension", required=True)
    args = parser.parse_args()

    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn.connect((args.server_ip, args.server_port))
    print(f"[CLIENT] Συνδέθηκες στο PBX {args.server_ip}:{args.server_port}")

    # Register
    send_msg(conn, {"type": "register", "extension": args.extension})

    # Thread for incoming messages
    t = threading.Thread(target=receiver_thread, args=(conn,), daemon=True)
    t.start()

    # Command loop
    try:
        while True:
            cmd = input("> ").strip()
            if not cmd:
                continue
            if cmd.lower() == "quit":
                break
            parts = cmd.split()
            if parts[0].lower() == "call" and len(parts) == 2:
                to_ext = parts[1]
                send_msg(conn, {"type": "call", "from": args.extension, "to": to_ext})
            elif parts[0].lower() == "answer":
                send_msg(conn, {"type": "answer"})
            elif parts[0].lower() == "hangup":
                send_msg(conn, {"type": "hangup"})
            elif parts[0].lower() == "digit" and len(parts) == 2:
                digit = parts[1]
                send_msg(conn, {"type": "ivr_choice", "digit": digit})
            elif parts[0].lower() == "ivr" and len(parts) == 2:
                to_ext = parts[1]
                send_msg(conn, {"type": "call", "from": args.extension, "to": to_ext})
            elif parts[0].lower() == "msg" and len(parts) >= 2:
                text = " ".join(parts[1:])
                send_msg(conn, {"type": "chat", "text": text})
            else:
                print("Εντολές: call <ext>, ivr <ext>, answer, hangup, digit <n>, msg <text>, quit")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        conn.close()
        sys.exit(0)


if __name__ == "__main__":
    main()
