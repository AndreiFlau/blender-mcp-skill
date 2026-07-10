"""Client for the official Blender Labs MCP extension bridge (default localhost:9876).

Executes Python inside a running Blender session and prints the response.

Usage:
  python bmcp.py "print(bpy.context.scene.name)"      # inline code
  python bmcp.py -f script.py                          # code from file
  python bmcp.py -t 600 -f long_job.py                 # custom timeout (seconds)

Protocol (null-byte-delimited JSON over TCP):
  request:  {"type": "execute", "code": "<python>", "strict_json": bool} + b"\\0"
  response: {"status": "ok"|"error", "result"|"message": ..., "stdout"?, "stderr"?} + b"\\0"

The code runs on Blender's main thread in a fresh namespace. Assign a dict to a
variable named `result` to return structured data; print() output is captured
into "stdout". Exceptions come back as status "error" with the traceback.
"""
import json
import os
import socket
import sys

HOST = os.environ.get("BLENDER_MCP_HOST", "localhost")
PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))


def execute(code, strict_json=False, timeout=120):
    payload = {"type": "execute", "code": code, "strict_json": strict_json}
    s = socket.create_connection((HOST, PORT), timeout=10)
    # The trailing null byte is mandatory: without it the server waits for the
    # rest of the request and eventually answers {"message": "Client timed out"}.
    s.sendall(json.dumps(payload).encode("utf-8") + b"\0")
    s.settimeout(timeout)
    data = b""
    while b"\0" not in data:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    if not data:
        raise RuntimeError("no response from Blender (server stopped mid-request?)")
    return json.loads(data.split(b"\0")[0].decode("utf-8"))


def show(resp):
    if resp.get("stdout"):
        print(resp["stdout"], end="" if resp["stdout"].endswith("\n") else "\n")
    if resp.get("stderr"):
        print("STDERR:", resp["stderr"], file=sys.stderr)
    if resp.get("status") == "ok":
        result = resp.get("result")
        if result:
            print("RESULT:", json.dumps(result, indent=1))
    else:
        print("ERROR:", resp.get("message", "?"))
        sys.exit(1)


def main():
    args = sys.argv[1:]
    timeout = 120
    if args and args[0] == "-t":
        timeout = float(args[1])
        args = args[2:]
    if args and args[0] == "-f":
        with open(args[1], encoding="utf-8") as f:
            code = f.read()
    elif args:
        code = args[0]
    else:
        print(__doc__)
        sys.exit(2)
    code = "import bpy\n" + code
    show(execute(code, timeout=timeout))


if __name__ == "__main__":
    main()
