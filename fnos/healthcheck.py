"""Used by the lifecycle script to confirm HTTP readiness over the local socket."""
import http.client
import json
import socket
import sys


def check(path, prefix):
    connection = http.client.HTTPConnection("localhost", timeout=1)
    try:
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(1)
        connection.sock.connect(path)
        connection.request("GET", prefix + "/api/health", headers={
            "X-Trim-Userid": "0", "X-Trim-Isadmin": "true",
        })
        response = connection.getresponse()
        return response.status == 200 and json.load(response).get("ok") is True
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(0 if check(sys.argv[1], sys.argv[2]) else 1)
