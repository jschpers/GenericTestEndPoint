"""
GenericTestEndPoint - a catch-all HTTP endpoint for NON-PRODUCTION test environments.

Every request (any HTTP method, any path, any body) is written to its own new
text file so you can see exactly what an external system sent. The response
only contains the name of the file that was created.

Uses only the Python standard library; no third-party packages are required.
See README.md for run/test instructions and example curl commands.

Configuration (command-line arguments override environment variables):
    --address     / GTE_HOST          IP address to bind     (default: 0.0.0.0 = all interfaces)
    --port        / GTE_PORT          Port to listen on      (default: 80)
    --output-dir  / GTE_OUTPUT_DIR    Folder for log files   (default: ./received-payloads)
"""

import argparse
import ipaddress
import json
import logging
import os
import socket
import sys
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("GenericTestEndPoint")

# Separates the metadata section from the raw body inside each output file.
BODY_SEPARATOR = "=" * 30 + " RAW REQUEST BODY (exact bytes as received) " + "=" * 30


class CaptureRequestHandler(BaseHTTPRequestHandler):
    """Writes every incoming request to a new file, regardless of HTTP method."""

    # HTTP/1.1 lets the server answer "Expect: 100-continue" (curl sends it for large bodies).
    protocol_version = "HTTP/1.1"

    # Set at startup by main(); the folder where request files are written.
    output_dir: Path = Path("./received-payloads")

    # ---- Accept ANY HTTP method ------------------------------------------------
    # BaseHTTPRequestHandler dispatches a request by looking up a method named
    # "do_<VERB>" (do_GET, do_POST, ...). Instead of defining a fixed list, we
    # answer every "do_*" lookup with the same capture handler, so standard and
    # custom verbs (e.g. FOOBAR, PROPFIND) are all accepted.
    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.handle_any_method
        raise AttributeError(name)

    def handle_any_method(self):
        received_at = datetime.now().astimezone()

        # Read the raw body bytes; it is never parsed or deserialized.
        try:
            body = self.read_raw_body()
        except Exception:
            logger.exception("Failed to read request body for %s %s", self.command, self.path)
            body = b""
            body_error = "ERROR: request body could not be read (see server log)."
        else:
            body_error = None

        # Build a unique filename: local timestamp (to the millisecond) + GUID.
        filename = (
            f"{received_at.strftime('%Y%m%d_%H%M%S')}_{received_at.microsecond // 1000:03d}"
            f"_{uuid.uuid4()}.txt"
        )

        try:
            self.write_capture_file(filename, received_at, body, body_error)
        except Exception:
            # Basic error logging if the file cannot be written.
            logger.exception("Failed to write capture file %s", filename)
            self.send_json(500, {"error": "Failed to write capture file. See server log."})
            return

        logger.info("%s %s -> %s (%d body bytes)", self.command, self.path, filename, len(body))
        # Only the filename is returned; payload contents are never echoed back.
        self.send_json(200, {"filename": filename})

    # ---- Request body -------------------------------------------------------------
    def read_raw_body(self) -> bytes:
        """Return the request body exactly as sent (empty bytes if there is none)."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self.read_chunked_body()

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def read_chunked_body(self) -> bytes:
        """Decode a 'Transfer-Encoding: chunked' body into its raw payload bytes."""
        chunks = []
        while True:
            size_line = self.rfile.readline()
            size = int(size_line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                # Consume optional trailer headers up to the terminating blank line.
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks)
            chunks.append(self.rfile.read(size))
            self.rfile.readline()  # CRLF after each chunk

    # ---- Output file ---------------------------------------------------------------
    def write_capture_file(self, filename, received_at, body, body_error):
        """Write metadata (UTF-8 text), a separator, then the raw body bytes unchanged."""
        split = urlsplit(self.path)
        host = self.headers.get("Host", f"{self.server.server_address[0]}:{self.server.server_address[1]}")
        # self.path is normally "/path?query"; build the complete URL from the Host header.
        full_url = self.path if split.scheme else f"http://{host}{self.path}"

        lines = [
            f"Date/Time Received : {received_at.isoformat(timespec='milliseconds')}",
            f"HTTP Method        : {self.command}",
            f"Full URL           : {full_url}",
            f"Path               : {split.path}",
            f"Query String       : {split.query}",
            f"Content-Type       : {self.headers.get('Content-Type', '')}",
            f"Content-Length     : {self.headers.get('Content-Length', '')}",
            f"Body Bytes Read    : {len(body)}",
            f"Remote IP Address  : {self.client_address[0]}",
            f"HTTP Version       : {self.request_version}",
            "",
            "Request Headers:",
        ]
        # headers.items() keeps the original order and any duplicate headers.
        lines += [f"  {name}: {value}" for name, value in self.headers.items()]
        if body_error:
            lines += ["", body_error]
        lines += ["", BODY_SEPARATOR, ""]

        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Mode "xb" = create a NEW file, fail if it somehow already exists (never append).
        with open(self.output_dir / filename, "xb") as f:
            f.write("\n".join(lines).encode("utf-8"))
            f.write(body)

    # ---- Response ------------------------------------------------------------------
    def send_json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if "filename" in payload:
            # Also exposed as a header so HEAD requests (which have no response body) can see it.
            self.send_header("X-Captured-Filename", payload["filename"])
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    # Route http.server's built-in access/error messages through the logging module.
    def log_message(self, format, *args):
        logger.debug("%s - %s", self.client_address[0], format % args)


def parse_address(value: str) -> str:
    """argparse type: accept only a literal IPv4/IPv6 address."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid IP address (e.g. 0.0.0.0, 127.0.0.1, 192.168.1.25)")


def parse_port(value: str) -> int:
    """argparse type: accept only an integer port from 1 to 65535."""
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid port (must be 1-65535)")
    return port


def main():
    parser = argparse.ArgumentParser(description="Catch-all HTTP endpoint that writes each request to a file.")
    # String defaults are run through the type function too, so environment values are validated as well.
    parser.add_argument("--address", "--host", dest="address", type=parse_address,
                        default=os.environ.get("GTE_HOST", "0.0.0.0"),
                        help="IP address to bind (default: 0.0.0.0, all interfaces)")
    parser.add_argument("--port", type=parse_port, default=os.environ.get("GTE_PORT", "80"),
                        help="Port to listen on (default: 80)")
    parser.add_argument("--output-dir", default=os.environ.get("GTE_OUTPUT_DIR", "./received-payloads"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Create the output folder up front so configuration problems show at startup.
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    CaptureRequestHandler.output_dir = output_dir

    # ThreadingHTTPServer handles each request on its own thread. It defaults to IPv4,
    # so switch the socket family when an IPv6 address was requested. SO_REUSEADDR is
    # disabled on Windows, where it would let us silently share a port already in use.
    server_class = type("CaptureHTTPServer", (ThreadingHTTPServer,), {
        "address_family": socket.AF_INET6 if ipaddress.ip_address(args.address).version == 6 else socket.AF_INET,
        "allow_reuse_address": os.name != "nt",
    })

    # Binding happens in the constructor; fail startup clearly if the address/port is unavailable.
    try:
        server = server_class((args.address, args.port), CaptureRequestHandler)
    except OSError as e:
        logger.error("Cannot listen on %s:%d - %s. Check that the address belongs to this machine, "
                     "the port is not already in use, and you have permission to use it "
                     "(ports below 1024 may require administrator/root).", args.address, args.port, e)
        sys.exit(1)

    # Log the address/port actually bound by the socket.
    bound_address, bound_port = server.server_address[:2]
    logger.info("Listening on %s:%d - writing requests to %s", bound_address, bound_port, output_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
