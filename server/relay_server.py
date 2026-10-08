#!/usr/bin/env python3
"""Blue Circuit's dependency-free HTTP/WebSocket room relay.

Run: python server/relay_server.py --root build/web
The host game, not this relay, owns movement, buoy claims and scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import mimetypes
import os
import secrets
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_MESSAGE = 65536
MAX_HTTP_HEADERS = 16384
MAX_CLIENTS = 256
MAX_ROOMS = 128
IDLE_TIMEOUT = 75.0
HTTP_FILE_DRAIN_TIMEOUT = 60.0  # Browser WASM compilation can pause large asset consumption.
VALID_CHARACTERS = {"asuka", "misaki"}
MIME_OVERRIDES = {".wasm": "application/wasm", ".pck": "application/octet-stream",
                  ".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8"}


class FrameError(Exception):
    def __init__(self, message: str, code: int = 1002):
        super().__init__(message)
        self.code = code


@dataclass(eq=False)
class Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    room: Room | None = None
    slot: int = -1
    last_seq: int = -1
    tokens: float = 240.0
    token_time: float = field(default_factory=time.monotonic)
    closed: bool = False
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        self.queue_frame(1, payload)

    def queue_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            return
        try:
            self.outbox.put_nowait((opcode, payload))
        except asyncio.QueueFull:
            asyncio.create_task(self.close(1008, "slow receiver"))

    async def write_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        header = bytes((0x80 | opcode, length)) if length < 126 else (
            bytes((0x80 | opcode, 126)) + struct.pack("!H", length) if length < 65536
            else bytes((0x80 | opcode, 127)) + struct.pack("!Q", length))
        async with self.write_lock:
            self.writer.write(header + payload)
            await asyncio.wait_for(self.writer.drain(), 5.0)

    async def flush(self) -> None:
        try:
            while not self.closed:
                opcode, payload = await self.outbox.get()
                await self.write_frame(opcode, payload)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            await self.close(1001, "connection lost")

    async def heartbeat(self) -> None:
        while not self.closed:
            await asyncio.sleep(25.0)
            self.queue_frame(9, b"blue-circuit")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
            await self.write_frame(8, struct.pack("!H", code) + reason.encode("utf-8")[:100])
        self.writer.close()

    def allow_request(self) -> bool:
        now = time.monotonic()
        self.tokens = min(240.0, self.tokens + (now - self.token_time) * 120.0)
        self.token_time = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


@dataclass
class Room:
    code: str
    players: list[Client | None]
    characters: list[str]
    duration: int
    ready: list[bool] = field(default_factory=lambda: [False, False])
    phase: str = "lobby"
    match_id: str = ""
    paused: bool = False


def no_json_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def safe_json(value) -> bool:
    """Bound nesting/number values before adding relay headers and serializing again."""
    stack, visited = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 32 or visited > 4096:
            return False
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return True


class Relay:
    def __init__(self, root: Path, public_ws_url: str = ""):
        self.root = root.resolve()
        self.public_ws_url = public_ws_url
        self.rooms: dict[str, Room] = {}
        self.clients: set[Client] = set()

    @staticmethod
    def error(client: Client, message: str) -> None:
        client.queue({"type": "error", "message": message})

    @staticmethod
    def broadcast(room: Room, message: dict) -> None:
        for peer in room.players:
            if peer is not None:
                peer.queue(message)

    @staticmethod
    def lobby(room: Room) -> None:
        players = [{"present": peer is not None, "character": room.characters[slot],
                    "ready": room.ready[slot]} for slot, peer in enumerate(room.players)]
        for slot, peer in enumerate(room.players):
            if peer is not None:
                peer.queue({"type": "lobby", "code": room.code, "slot": slot,
                            "players": players, "duration": room.duration, "phase": room.phase})

    def detach(self, client: Client) -> None:
        room, slot = client.room, client.slot
        client.room, client.slot, client.last_seq = None, -1, -1
        if room is None:
            return
        room.players[slot] = None
        other = room.players[1 - slot]
        if slot == 0:
            self.rooms.pop(room.code, None)
            if other is not None:
                other.room, other.slot, other.last_seq = None, -1, -1
                other.queue({"type": "peer_left", "slot": 0,
                             "message": "1Pが切断しました。ルームは終了しました。"})
        elif other is not None:
            room.phase, room.match_id, room.paused = "lobby", "", False
            room.ready = [False, False]
            other.last_seq = -1
            other.queue({"type": "peer_left", "slot": 1,
                         "message": "2Pが切断しました。新しい参加者を待っています。"})
            self.lobby(room)

    def dispatch(self, client: Client, message: dict) -> None:
        """Mutate room state without awaiting; simultaneous ready/leave cannot race."""
        kind = message.get("type")
        if kind == "ping":
            client.queue({"type": "pong"})
            return
        if kind == "leave":
            self.detach(client)
            client.queue({"type": "left"})
            return
        if kind in {"create", "join"}:
            if client.room is not None:
                self.error(client, "参加中のルームから退出してから操作してください。")
                return
            character = message.get("character", "asuka")
            if character not in VALID_CHARACTERS:
                self.error(client, "キャラクターを選び直してください。")
                return
            if kind == "create":
                duration = message.get("duration", 600)
                if type(duration) is not int or duration not in (180, 600):
                    self.error(client, "試合時間は180秒または600秒を指定してください。")
                    return
                if len(self.rooms) >= MAX_ROOMS:
                    self.error(client, "サーバーが混雑しています。少し待って再試行してください。")
                    return
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
                while code in self.rooms:
                    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
                room = Room(code, [client, None], [character, "misaki"], duration)
                self.rooms[code] = room
                client.room, client.slot = room, 0
            else:
                code_value = message.get("code", "")
                if not isinstance(code_value, str):
                    self.error(client, "ルームコードを確認してください。")
                    return
                code = code_value.strip().upper()
                room = self.rooms.get(code)
                if room is None:
                    self.error(client, "ルームが見つかりません。コードを確認してください。")
                    return
                if room.players[1] is not None or room.phase != "lobby":
                    self.error(client, "このルームは満員か試合中です。")
                    return
                room.players[1] = client
                room.characters[1] = character
                room.ready = [False, False]
                client.room, client.slot = room, 1
            self.lobby(room)
            return
        room = client.room
        if room is None:
            self.error(client, "ルームを作成するか、ルームコードで参加してください。")
            return
        slot = client.slot
        if kind == "select":
            character = message.get("character")
            if room.phase != "lobby" or character not in VALID_CHARACTERS:
                self.error(client, "待機中にアスカまたはミサキを選択してください。")
                return
            room.characters[slot] = character
            room.ready = [False, False]
            self.lobby(room)
        elif kind == "ready":
            ready = message.get("ready")
            if room.phase != "lobby" or type(ready) is not bool:
                self.error(client, "準備状態を変更できません。")
                return
            room.ready[slot] = ready
            self.lobby(room)
            if all(room.players) and all(room.ready):
                room.phase, room.match_id = "playing", secrets.token_hex(12)
                for peer in room.players:
                    peer.last_seq = -1
                self.broadcast(room, {"type": "start", "match_id": room.match_id,
                                      "characters": room.characters[:], "duration": room.duration})
        elif kind in {"input", "snapshot", "pause", "finish"}:
            if room.phase != "playing" or message.get("match_id") != room.match_id:
                self.error(client, "終了済みか別の試合の通信を受信しました。")
                return
            if kind in {"input", "snapshot"}:
                required_slot = 1 if kind == "input" else 0
                seq, data = message.get("seq"), message.get("data")
                if slot != required_slot:
                    self.error(client, "このプレイヤーはその同期データを送信できません。")
                    return
                if type(seq) is not int or not (0 <= seq <= 9007199254740991) or not isinstance(data, dict):
                    self.error(client, "同期データの形式が正しくありません。")
                    return
                if seq <= client.last_seq:
                    return  # Quietly ignore duplicate/reordered frames; never apply twice.
                client.last_seq = seq
                other = room.players[1 - slot]
                if other is not None:
                    other.queue({"type": kind, "match_id": room.match_id, "seq": seq, "data": data})
            elif kind == "pause":
                paused = message.get("paused")
                if type(paused) is not bool:
                    self.error(client, "一時停止状態の形式が正しくありません。")
                    return
                room.paused = paused
                self.broadcast(room, {"type": "pause", "match_id": room.match_id, "paused": paused})
            else:
                if slot != 0:
                    self.error(client, "試合終了は1Pが判定します。")
                    return
                room.phase = "results"
                self.broadcast(room, {"type": "finish", "match_id": room.match_id})
        elif kind == "rematch":
            if room.phase != "results":
                self.error(client, "試合が終了してから再戦してください。")
                return
            room.phase, room.match_id, room.paused = "lobby", "", False
            room.ready = [False, False]
            for peer in room.players:
                if peer is not None:
                    peer.last_seq = -1
            self.lobby(room)
        else:
            self.error(client, "対応していない操作です。")

    async def websocket(self, client: Client) -> None:
        self.clients.add(client)
        sender = asyncio.create_task(client.flush())
        heartbeat = asyncio.create_task(client.heartbeat())
        fragments = bytearray()
        fragmented = False
        try:
            while not client.closed:
                final, opcode, payload = await asyncio.wait_for(read_frame(client.reader), IDLE_TIMEOUT)
                if opcode == 8:
                    if len(payload) == 1:
                        raise FrameError("invalid close payload")
                    code = 1000
                    if payload:
                        code = struct.unpack("!H", payload[:2])[0]
                        if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                            raise FrameError("invalid close code")
                        payload[2:].decode("utf-8")
                    await client.close(code)
                    break
                if opcode == 9:
                    client.queue_frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode == 2:
                    raise FrameError("text messages required", 1003)
                if opcode == 1:
                    if fragmented:
                        raise FrameError("new message before final continuation")
                    fragments = bytearray(payload)
                    fragmented = not final
                elif opcode == 0:
                    if not fragmented:
                        raise FrameError("unexpected continuation")
                    fragments.extend(payload)
                    fragmented = not final
                else:
                    raise FrameError("unknown opcode")
                if len(fragments) > MAX_MESSAGE:
                    raise FrameError("message too large", 1009)
                if fragmented:
                    continue
                if not client.allow_request():
                    raise FrameError("request rate exceeded", 1008)
                text = fragments.decode("utf-8")
                try:
                    message = json.loads(text, parse_constant=no_json_constant)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    self.error(client, "通信データを読み取れませんでした。")
                    continue
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    self.error(client, "通信データの形式が正しくありません。")
                    continue
                if not safe_json(message):
                    self.error(client, "通信データが複雑すぎるか数値が正しくありません。")
                    continue
                # JSON object/list values may be supplied for a scalar field; reject instead of crashing.
                if "character" in message and not isinstance(message["character"], str):
                    self.error(client, "キャラクターの指定が正しくありません。")
                    continue
                self.dispatch(client, message)
        except FrameError as exc:
            await client.close(exc.code, str(exc))
        except UnicodeDecodeError:
            await client.close(1007, "invalid UTF-8")
        except asyncio.TimeoutError:
            await client.close(1001, "connection timed out")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.detach(client)
            self.clients.discard(client)
            sender.cancel()
            heartbeat.cancel()
            await asyncio.gather(sender, heartbeat, return_exceptions=True)
            client.writer.close()
            with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.wait_for(client.writer.wait_closed(), 1.0)

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upgraded = False
        file_response_started = False
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
            if len(request) > MAX_HTTP_HEADERS:
                await http_response(writer, 431, b"Headers too large")
                return
            lines = request.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            path = urlsplit(target).path
            if path == "/ws":
                if len(self.clients) >= MAX_CLIENTS:
                    await http_response(writer, 503, b"Server busy")
                    return
                if method != "GET" or version != "HTTP/1.1" or headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in {token.strip().lower() for token in headers.get("connection", "").split(",")}:
                    await http_response(writer, 400, b"WebSocket upgrade required")
                    return
                if headers.get("sec-websocket-version") != "13":
                    await http_response(writer, 426, b"WebSocket version 13 required", {"Sec-WebSocket-Version": "13"})
                    return
                key = headers.get("sec-websocket-key", "")
                try:
                    if len(base64.b64decode(key, validate=True)) != 16:
                        raise ValueError("invalid key")
                except (ValueError, base64.binascii.Error):
                    await http_response(writer, 400, b"Invalid WebSocket key")
                    return
                accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
                upgraded = True
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
                await writer.drain()
                await self.websocket(Client(reader, writer))
                return
            if method not in {"GET", "HEAD"}:
                await http_response(writer, 405, b"GET or HEAD required", {"Allow": "GET, HEAD"})
                return
            if path == "/health":
                body = json.dumps({"status": "ok", "rooms": len(self.rooms), "clients": len(self.clients)}).encode()
                await http_response(writer, 200, body, {"Content-Type": "application/json"}, method == "HEAD")
                return
            if path == "/client-config.js":
                # This is a public URL, never a place for passwords or API secrets.
                config = json.dumps({"websocket_url": self.public_ws_url}, ensure_ascii=True,
                                    separators=(",", ":"), allow_nan=False)
                body = f"window.BLUE_CIRCUIT_CONFIG = {config};\n".encode("ascii")
                await http_response(writer, 200, body,
                                    {"Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-cache"},
                                    method == "HEAD")
                return
            decoded = unquote(path, errors="strict")
            if "\\" in decoded or "\x00" in decoded:
                await http_response(writer, 403, b"Forbidden")
                return
            candidate = (self.root / decoded.lstrip("/")).resolve()
            if not candidate.is_relative_to(self.root):
                await http_response(writer, 403, b"Forbidden")
                return
            if candidate.is_dir():
                candidate = (candidate / "index.html").resolve()
            # Resolving again also blocks an index.html symlink outside the export root.
            if not candidate.is_relative_to(self.root) or not candidate.is_file():
                await http_response(writer, 404, b"Web export not found. Export to build/web/index.html first.")
                return
            served = candidate
            response_headers = {}
            accepts_gzip = False
            for token in headers.get("accept-encoding", "").split(","):
                parts = [part.strip() for part in token.split(";")]
                if parts[0] == "gzip":
                    quality = next((part[2:] for part in parts[1:] if part.startswith("q=")), "1")
                    with contextlib.suppress(ValueError):
                        accepts_gzip = float(quality) > 0
            gzip_sibling = candidate.with_name(candidate.name + ".gz")
            if accepts_gzip and gzip_sibling.is_file() and gzip_sibling.resolve().is_relative_to(self.root):
                served = gzip_sibling
                response_headers["Content-Encoding"] = "gzip"
                response_headers["Vary"] = "Accept-Encoding"
            if candidate.suffix == ".gz":
                response_headers["Content-Encoding"] = "gzip"
                suffix = Path(candidate.stem).suffix
            else:
                suffix = candidate.suffix
            response_headers["Content-Type"] = MIME_OVERRIDES.get(suffix, mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
            response_headers["Cache-Control"] = "no-cache"
            file_response_started = True
            await send_file(writer, served, response_headers, method == "HEAD")
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            if not file_response_started:
                with contextlib.suppress(ConnectionError, OSError):
                    await http_response(writer, 400, b"Invalid HTTP request")
        except (ConnectionError, OSError):
            pass
        finally:
            if not upgraded:
                await finish_http_connection(reader, writer)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1.0)


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    first, second = await reader.readexactly(2)
    final, opcode, length = bool(first & 0x80), first & 0x0F, second & 0x7F
    if first & 0x70 or not second & 0x80:
        raise FrameError("RSV bits or unmasked client frame")
    if opcode >= 8 and (not final or length > 125):
        raise FrameError("invalid control frame")
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
        if length < 126:
            raise FrameError("noncanonical frame length")
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length < 65536 or length >> 63:
            raise FrameError("invalid frame length")
    if length > MAX_MESSAGE:
        raise FrameError("frame too large", 1009)
    mask = await reader.readexactly(4)
    raw = await reader.readexactly(length)
    return final, opcode, bytes(value ^ mask[index % 4] for index, value in enumerate(raw))


HTTP_STATUS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 426: "Upgrade Required", 431: "Request Header Fields Too Large", 503: "Service Unavailable"}


def response_header(status: int, length: int, headers: dict | None = None) -> bytes:
    values = {"Content-Length": str(length), "Connection": "close",
              "Content-Type": "text/plain; charset=utf-8", "X-Content-Type-Options": "nosniff",
              "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp",
              "Cross-Origin-Resource-Policy": "same-origin"}
    values.update(headers or {})
    return (f"HTTP/1.1 {status} {HTTP_STATUS[status]}\r\n" +
            "".join(f"{key}: {value}\r\n" for key, value in values.items()) + "\r\n").encode("ascii")


async def http_response(writer, status, body, headers=None, head=False):
    writer.write(response_header(status, len(body), headers))
    if not head:
        writer.write(body)
    await asyncio.wait_for(writer.drain(), 10.0)


async def send_file(writer, path: Path, headers: dict, head: bool) -> None:
    writer.write(response_header(200, path.stat().st_size, headers))
    if not head:
        with path.open("rb") as stream:
            while chunk := stream.read(16384):
                writer.write(chunk)
                await asyncio.wait_for(writer.drain(), HTTP_FILE_DRAIN_TIMEOUT)
                # Cooperate even when drain() returns immediately below its high-water mark.
                # Large single writes followed by close truncated the JS response on Windows.
                await asyncio.sleep(0)
    await asyncio.wait_for(writer.drain(), HTTP_FILE_DRAIN_TIMEOUT)


async def finish_http_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Half-close before releasing a socket so a reset cannot erase the last response.

    drain() only flushes asyncio's buffer; it does not guarantee a client has read the
    bytes. RFC 9112 section 9.6 recommends shutting down the write side, then reading
    the peer's close before closing fully. The read is bounded in both time and size.
    WebSocket connections retain their own close-handshake path.
    """
    if writer.is_closing() or not writer.can_write_eof():
        return
    with contextlib.suppress(ConnectionError, OSError, asyncio.TimeoutError):
        writer.write_eof()
        async with asyncio.timeout(1.0):
            remaining = 65536
            while remaining > 0:
                chunk = await reader.read(min(8192, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)


async def run(args) -> None:
    relay = Relay(args.root, args.public_ws_url)
    server = await asyncio.start_server(relay.connection, args.host, args.port, limit=MAX_HTTP_HEADERS)
    print(f"Blue Circuit relay: http://{args.host}:{args.port}/ | ws://{args.host}:{args.port}/ws", flush=True)
    print(f"Static export: {relay.root}", flush=True)
    print("Ctrl+C to stop. Public HTTPS/WSS is provided by your hosting service or reverse proxy.", flush=True)
    async with server:
        await server.serve_forever()


def port_number(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def bind_host(value: str) -> str:
    if not value or any(character.isspace() for character in value) or "/" in value:
        raise argparse.ArgumentTypeError("host must be an IP address or hostname, not a URL")
    return value


def public_ws_url(value: str) -> str:
    if not value:
        return ""  # The browser derives ws/wss and /ws from its own page URL.
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme == "wss" and parsed.hostname and parsed.username is None
                and parsed.password is None and not parsed.fragment
                and (port is None or 1 <= port <= 65535)
                and not any(character.isspace() for character in value)):
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("public WebSocket URL must be wss://host/path without credentials or fragment")


def parse_args(argv=None, environ=None) -> argparse.Namespace:
    """CLI values take precedence over deployment environment and local defaults."""
    environment = os.environ if environ is None else environ

    def configured(name, fallback):
        value = environment.get(name, "").strip()
        return value or fallback

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=bind_host, default=configured("BLUE_CIRCUIT_HOST", "127.0.0.1"),
                        help="Bind address; BLUE_CIRCUIT_HOST or 127.0.0.1 locally; 0.0.0.0 in a public container")
    parser.add_argument("--port", type=port_number,
                        default=configured("BLUE_CIRCUIT_PORT", configured("PORT", "8765")),
                        help="Listen port; BLUE_CIRCUIT_PORT, then provider PORT, then 8765")
    parser.add_argument("--root", type=Path,
                        default=configured("BLUE_CIRCUIT_WEB_ROOT", Path(__file__).resolve().parents[1] / "build" / "web"),
                        help="Web export folder; BLUE_CIRCUIT_WEB_ROOT or project's build/web")
    parser.add_argument("--public-ws-url", type=public_ws_url,
                        default=configured("BLUE_CIRCUIT_PUBLIC_WS_URL", ""),
                        help="Optional public wss:// URL, or BLUE_CIRCUIT_PUBLIC_WS_URL; empty uses the page's own /ws")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    try:
        # Selector sockets avoid Windows Proactor shutdown stalls after an abrupt peer reset.
        # No subprocess or Windows pipe operations are needed by this service.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(run(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise SystemExit(f"Server startup failed: {exc}") from None


if __name__ == "__main__":
    main()
