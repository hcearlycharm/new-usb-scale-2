#!/usr/bin/env python3
"""
USB Scale WebSocket Server — Python 3
Serves the HTML UI on port 8000 and WebSocket data on port 8001
"""

import sys
print("Script is starting...", flush=True)

import argparse
import asyncio
import concurrent.futures
import json
import os
import queue
import ssl
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import websockets

# ── Scale state ───────────────────────────────────────────────────────────────

scale = None
SCALE_UNAVAILABLE = {'lbs': 'No scale connected', 'ozs': 'No scale connected'}

# A thread-safe queue that holds the latest weight reading
# maxsize=1 means it only ever holds the most recent reading
weight_queue = queue.Queue(maxsize=1)


def try_connect_scale():
    """Attempt to connect to the USB scale. Returns the scale object or None."""
    global scale
    try:
        from readscale import set_scale
        scale = set_scale()
        print("Scale connected successfully.", flush=True)
        return scale
    except Exception as e:
        print(f"WARNING: Could not connect to scale: {e}", flush=True)
        scale = None
        return None


def scale_reader_thread():
    """
    Runs forever in a dedicated background thread.
    This is the ONLY thread that ever touches the USB scale.
    Continuously reads weight and puts it into weight_queue.
    """
    global scale
    print("Scale reader thread started.", flush=True)

    # Keep last known good weight so brief timeouts don't show "No scale connected"
    last_good_weight = None

    while True:
        # If scale isn't connected, try to connect
        if scale is None:
            try_connect_scale()
            if scale is None:
                time.sleep(2)
                continue

        # Try to read the scale
        try:
            scale.update()
            weight = {
                'lbs': scale.pounds,
                'ozs': round(scale.ounces, 2)
            }
            last_good_weight = weight  # Save last successful reading

        except Exception as e:
            print(f"Scale read error: {e} - reconnecting...", flush=True)
            scale = None
            # Use last known good weight instead of "No scale connected"
            weight = last_good_weight if last_good_weight is not None else SCALE_UNAVAILABLE

        # Put latest reading in queue
        try:
            weight_queue.get_nowait()
        except queue.Empty:
            pass
        weight_queue.put(weight)

        time.sleep(0.1)


# Holds the last successfully retrieved weight so we never flicker to "No scale connected"
last_sent_weight = None

def get_latest_weight():
    """
    Get the most recent weight reading from the queue.
    Returns the last known weight if the queue is empty between reads.
    Only returns SCALE_UNAVAILABLE if no weight has ever been received.
    """
    global last_sent_weight
    try:
        last_sent_weight = weight_queue.get_nowait()
    except queue.Empty:
        pass  # Queue empty between reads - just use last known value
    
    if last_sent_weight is not None:
        return last_sent_weight
    return SCALE_UNAVAILABLE


# ── WebSocket server (port 8001) ──────────────────────────────────────────────

connected_clients = set()


async def scale_websocket_handler(websocket):
    """
    Called once per browser connection.
    Pulls latest weight from the queue and pushes it to the browser every 200ms.
    Never touches USB directly - that's the scale_reader_thread's job.
    """
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"Browser connected: {client_addr}", flush=True)

    try:
        while True:
            # Just read from the queue - no blocking USB calls here
            weight = get_latest_weight()
            message = json.dumps(weight)
            await websocket.send(message)
            await asyncio.sleep(0.2)

    except websockets.exceptions.ConnectionClosedOK:
        print(f"Browser disconnected cleanly: {client_addr}", flush=True)
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"Browser disconnected with error: {client_addr} - {e}", flush=True)
    finally:
        connected_clients.discard(websocket)


# ── HTTP server (port 8000) ───────────────────────────────────────────────────

class HTMLPageHandler(BaseHTTPRequestHandler):
    """Serves templates/index.html for any GET request."""

    def do_GET(self):
        html_path = os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
        try:
            with open(html_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write('<h1>404 - templates/index.html not found</h1>'.encode('utf-8'))

    def log_message(self, format, *args):
        pass


def start_http_server(ssl_context=None, port=8000):
    """Runs the HTTP server in a background thread."""
    server = HTTPServer(('localhost', port), HTMLPageHandler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
        print(f"HTTP server listening on https://localhost:{port}/", flush=True)
    else:
        print(f"HTTP server listening on http://localhost:{port}/", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── SSL helper ────────────────────────────────────────────────────────────────

def build_ssl_context(certfile, keyfile):
    for path in (certfile, keyfile):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"SSL file not found: {path}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    return ctx


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description='Serve USB scale weights over WebSockets (Python 3)'
    )
    parser.add_argument('-k', '--key',  help='Path to SSL private key (.pem)')
    parser.add_argument('-c', '--cert', help='Path to SSL certificate (.pem)')
    parser.add_argument('--http-port', type=int, default=8000,
                        help='Port for the HTML page (default: 8000)')
    parser.add_argument('--ws-port', type=int, default=8001,
                        help='Port for the WebSocket feed (default: 8001)')
    return parser.parse_args()


# ── Main entry point ──────────────────────────────────────────────────────────

async def run(args):
    print("run() started", flush=True)

    ssl_ctx = None
    if args.cert and args.key:
        print("Building SSL context...", flush=True)
        ssl_ctx = build_ssl_context(args.cert, args.key)
        print("SSL context OK", flush=True)
        ws_scheme   = 'wss'
        http_scheme = 'https'
    else:
        ws_scheme   = 'ws'
        http_scheme = 'http'

    # Start the dedicated scale reader thread
    # daemon=True means it stops automatically when the main program exits
    reader = threading.Thread(target=scale_reader_thread, daemon=True)
    reader.start()

    # Start the HTTP server
    print("Starting HTTP server...", flush=True)
    start_http_server(ssl_context=ssl_ctx, port=args.http_port)
    print("HTTP server started", flush=True)

    # Start the WebSocket server
    print("Starting WebSocket server...", flush=True)
    async with websockets.serve(
        scale_websocket_handler,
        'localhost',
        args.ws_port,
        ssl=ssl_ctx
    ):
        print("WebSocket server started", flush=True)
        print(f"", flush=True)
        print(f"  Open your browser at: {http_scheme}://localhost:{args.http_port}/", flush=True)
        print(f"  WebSocket data at:    {ws_scheme}://localhost:{args.ws_port}/", flush=True)
        print(f"", flush=True)
        print("Press Ctrl+C to stop the server.", flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)