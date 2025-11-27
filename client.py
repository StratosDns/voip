import socket
import threading
import json
import argparse
import sys


def send_msg(conn, obj):
    try:
        data = json.dumps(obj).encode("utf-8") + b"\n"
        conn.sendall(data)
    except Exception:
        pass


def receiver_thread(conn):
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

            if mtype == "register_ok":
                ext = msg.get("extension")
                print(f"[SERVER] Καταχωρήθηκες ως extension {ext}")

            elif mtype == "call_proceeding":
                to = msg.get("to")
                print(f"[SERVER] Γίνεται κλήση προς {to}...")

            elif mtype == "incoming_call":
                frm = msg.get("from")
                print(f"[SERVER] Νέα κλήση από {frm}. Πληκτρολόγησε 'answer' για απάντηση.")

            elif mtype == "incoming_call_waiting":
                frm = msg.get("from")
                print(f"[SERVER] CALL WAITING: Νέα κλήση από {frm} (ήδη σε κλήση).")

            elif mtype == "call_answered":
                by = msg.get("by")
                print(f"[SERVER] Η κλήση απαντήθηκε από {by}.")

            elif mtype == "hangup":
                by = msg.get("by")
                print(f"[SERVER] Η κλήση τερματίστηκε από {by}.")

            elif mtype == "busy":
                to = msg.get("to")
                print(f"[SERVER] Μήνυμα: {{'type': 'busy', 'to': '{to}'}}")

            elif mtype == "ivr_message":
                text = msg.get("text", "")
                print("\n" + text + "\nΓράψε 'digit <n>' για να επιλέξεις (π.χ. digit 3)\n")

            elif mtype == "ivr_info":
                text = msg.get("text", "")
                print(f"[SERVER][IVR INFO] {text}")

            elif mtype == "chat":
                frm = msg.get("from")
                text = msg.get("text", "")
                print(f"[CHAT] {frm}: {text}")

            elif mtype == "chat_sent":
                to = msg.get("to")
                print(f"[SERVER] Το μήνυμα στάλθηκε προς {to}.")

            elif mtype == "error":
                reason = msg.get("reason", "Άγνωστο σφάλμα.")
                print(f"[SERVER][ERROR] {reason}")

            else:
                # Unknown or debug message
                print(f"[SERVER] Μήνυμα: {msg}")
    except Exception as e:
        print(f"[CLIENT] Σφάλμα receiver: {e}")
    finally:
        print("[CLIENT] Η σύνδεση με τον server τερματίστηκε.")
        try:
            conn.close()
        except Exception:
            pass
        # exit entire process if receiver dies
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--server-port", type=int, required=True)
    parser.add_argument("--extension", required=True)
    args = parser.parse_args()

    # Connect
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn.connect((args.server_ip, args.server_port))
    print(f"[CLIENT] Συνδέθηκες στο PBX {args.server_ip}:{args.server_port}")

    # Register
    send_msg(conn, {"type": "register", "extension": args.extension})

    # Start receiver thread
    t = threading.Thread(target=receiver_thread, args=(conn,), daemon=True)
    t.start()

    # Small help
    print("\nΔιαθέσιμες εντολές:")
    print("  call <ext>   → ξεκινά κλήση")
    print("  answer       → απαντά εισερχόμενη κλήση")
    print("  hangup       → τερματίζει την τρέχουσα κλήση")
    print("  ivr <ext>    → κλήση στο IVR (5000 ή 7000)")
    print("  digit <n>    → επιλογή σε IVR (0–9)")
    print("  msg <text>   → στέλνει μήνυμα chat στον συνομιλητή")
    print("  quit         → έξοδος\n")

    if args.extension.startswith("5"):
        print("Βρίσκεσαι στο Κέντρο A:")
        print("  - Τοπικό range: 5XXX")
        print("  - IVR: 5000 (π.χ. 'ivr 5000')")
        print("  - Μπορείς να καλέσεις remote 7XXX μέσω trunk.\n")
    elif args.extension.startswith("7"):
        print("Βρίσκεσαι στο Κέντρο B:")
        print("  - Τοπικό range: 7XXX")
        print("  - IVR: 7000 (π.χ. 'ivr 7000')")
        print("  - Μπορείς να καλέσεις remote 5XXX μέσω trunk.\n")

    # Command loop
    while True:
        try:
            cmd = input("> ").strip()
        except EOFError:
            break

        if not cmd:
            continue

        parts = cmd.split()
        op = parts[0].lower()

        if op == "call" and len(parts) == 2:
            ext = parts[1]
            send_msg(conn, {"type": "call", "to": ext})

        elif op == "answer":
            send_msg(conn, {"type": "answer"})

        elif op == "hangup":
            send_msg(conn, {"type": "hangup"})

        elif op == "ivr" and len(parts) == 2:
            ivr_ext = parts[1]
            send_msg(conn, {"type": "ivr", "to": ivr_ext})

        elif op == "digit" and len(parts) == 2:
            digit = parts[1]
            send_msg(conn, {"type": "ivr_choice", "digit": digit})

        elif op == "msg" and len(parts) >= 2:
            text = " ".join(parts[1:])
            send_msg(conn, {"type": "chat", "text": text})

        elif op == "quit":
            print("[CLIENT] Έξοδος...")
            try:
                conn.close()
            except Exception:
                pass
            sys.exit(0)

        else:
            print("Άγνωστη εντολή. Διαθέσιμες: call, answer, hangup, ivr, digit, msg, quit.")


if __name__ == "__main__":
    main()
