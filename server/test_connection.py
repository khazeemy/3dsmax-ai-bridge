"""
Quick standalone connectivity test - run this AFTER starting
max_ai_listener.py inside 3ds Max, to confirm the socket bridge works
before wiring anything up to Claude.

Usage:
    python test_connection.py
"""

import json
import socket
import sys
import uuid

HOST = "127.0.0.1"
PORT = 8765


def call(action, params=None):
    request = {"id": str(uuid.uuid4()), "action": action, "params": params or {}}
    payload = (json.dumps(request) + "\n").encode("utf-8")

    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(payload)
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
    line, _, _ = buffer.partition(b"\n")
    return json.loads(line.decode("utf-8"))


def main():
    print(f"Connecting to 3ds Max listener at {HOST}:{PORT} ...\n")

    try:
        print("-> get_scene_state")
        print(json.dumps(call("get_scene_state"), indent=2)[:1000])

        print("\n-> list_selection")
        print(json.dumps(call("list_selection"), indent=2))

        print("\n-> list_materials")
        print(json.dumps(call("list_materials"), indent=2))

    except ConnectionRefusedError:
        print(
            "Connection refused. Make sure 3ds Max is open and "
            "max_ai_listener.py's start_listener() has been called.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
