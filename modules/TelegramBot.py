"""
Hang-proof Telegram Bot API client for MicroPython (Pico W).

Why this replaces urequests:
  * urequests opened a brand-new TLS connection for every call. At a 2 s poll that is
    ~40 000 TLS handshakes a day, each allocating ~40 KB of mbedTLS buffers.
  * urequests.get()/post() were called without a timeout. One half-open TCP connection
    (router reboot, NAT entry expired, Wi-Fi blip mid-request) blocks forever.
  * A response of unbounded size went straight into json() -> MemoryError, and the
    same update was fetched again on every poll.

This client keeps ONE TLS connection open (HTTP/1.1 keep-alive), puts a timeout on
every socket operation, caps the response size, and closes the socket on any error so
the next call starts clean. Public methods never raise.

Power: updates are fetched with Telegram *long polling*. One getUpdates request sits
open for up to `long_s` seconds and Telegram answers the moment a message arrives.
The wait is non-blocking (select.poll), so the main loop keeps serving the intercom
while the CPU sleeps and the Wi-Fi radio can doze between beacons. Compared to asking
every second, that is ~25x fewer requests and faster reaction to /open.
"""
import socket
import select
import json
import gc
from time import ticks_ms, ticks_diff, ticks_add

try:
    import ssl
except ImportError:  # very old firmware
    import ussl as ssl

API_HOST = "api.telegram.org"
MAX_BODY = 8192  # bytes. Bigger responses are skipped instead of parsed.
_MONTHS = "JanFebMarAprMayJunJulAugSepOctNovDec"
_TIMEOUT_ERRNOS = (11, 110, -11, -110)  # EAGAIN / ETIMEDOUT (mbedTLS passes them negated)


def http_date_to_unix(value):
    """'Thu, 08 Oct 2026 10:00:00 GMT' -> unix seconds, or None if unparseable."""
    try:
        _, d, mon, y, hms, _ = value.split()
        m = _MONTHS.index(mon[:3]) // 3 + 1
        hh, mm, ss = hms.split(":")
        d = int(d)
        y = int(y)
        secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
    except Exception:
        return None
    # days-from-civil (Howard Hinnant)
    if m <= 2:
        y -= 1
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return (era * 146097 + doe - 719468) * 86400 + secs


def _is_timeout(exc):
    return bool(exc.args) and exc.args[0] in _TIMEOUT_ERRNOS


class Message:
    def __init__(self, update_id, text, chat_id, sender, age_s):
        self.update_id = update_id
        self.text = text
        self.chat_id = chat_id
        self.sender = sender
        self.age_s = age_s  # seconds since the user sent it, None if unknown


class TelegramBot:
    def __init__(self, token, chat_id, timeout_s=8, host=API_HOST, port=443,
                 use_tls=True, offset_file="tg_offset.txt"):
        self._prefix = "/bot%s/" % token
        self.chat_id = int(chat_id)
        self.timeout_s = timeout_s
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.offset_file = offset_file
        self.offset = self._load_offset()
        self._addr = None  # cached DNS result
        self._sock = None  # the live keep-alive connection

        # Health info, read by main.py
        self.last_ok = ticks_ms()    # last time a complete HTTP response arrived
        self.failures = 0            # consecutive transport failures
        self.last_error = None
        self.server_time = None      # unix time from the last response's Date header
        self.connections = 0         # TLS connections opened since boot
        self.requests = 0
        self._backoff_until = None   # ticks_ms deadline after HTTP 429 / failures

        # Long-poll state
        self._pending = False        # a getUpdates request is waiting for its answer
        self._pending_reused = False
        self._pending_deadline = 0
        self._poller = None

    # ------------------------------------------------------------------ public

    def send_message(self, text, silent=False, chat_id=None):
        """Send to chat_id (default: the owner's CHAT_ID). True if Telegram accepted it."""
        body = {"chat_id": chat_id if chat_id is not None else self.chat_id, "text": text}
        if silent:
            body["disable_notification"] = True
        self.abort_poll()
        res = self._call("sendMessage", body=body)
        return bool(res and res.get("ok"))

    @property
    def polling(self):
        return self._pending

    def begin_poll(self, long_s=25):
        """Send a long-poll getUpdates and return immediately. No-op if one is already
        waiting, or while backing off after errors."""
        if self._pending:
            return
        if self._backoff_until is not None:
            if ticks_diff(self._backoff_until, ticks_ms()) > 0:
                return
            self._backoff_until = None
        self.requests += 1
        payload = self._payload("getUpdates", None,
                                "?offset=%d&limit=1&timeout=%d"
                                "&allowed_updates=%%5B%%22message%%22%%5D" % (self.offset, long_s))
        try:
            reused = self._sock is not None
            if not reused:
                self._connect()
            try:
                self._sock.write(payload)
            except Exception:
                if not reused:
                    raise
                self.close()  # stale keep-alive socket: one retry on a fresh one
                self._connect()
                reused = False
                self._sock.write(payload)
        except Exception as e:
            self._fail("getUpdates", e)
            return
        self._pending = True
        self._pending_reused = reused
        # Telegram answers within long_s; allow for latency before calling it dead.
        self._pending_deadline = ticks_add(ticks_ms(), (long_s + self.timeout_s + 2) * 1000)
        self._poller = select.poll()
        self._poller.register(self._sock, select.POLLIN)

    def poll_ready(self, wait_ms):
        """Sleep up to wait_ms for the long-poll answer. True when it can be read."""
        if not self._pending:
            return False
        if self._poller.poll(wait_ms):
            return True
        if ticks_diff(ticks_ms(), self._pending_deadline) > 0:
            # Half-open connection: the answer is never coming.
            self._pending = False
            self._fail("getUpdates", OSError("no answer to long poll"))
        return False

    def finish_poll(self):
        """Read the long-poll answer. Returns a Message or None. Never raises."""
        self._pending = False
        self._poller = None
        try:
            line = self._sock.readline()
            if not line:
                if self._pending_reused:
                    # Server closed an idle keep-alive connection: just poll again.
                    self.close()
                    return None
                raise OSError("connection closed by server")
            status, data = self._read_response(line)
        except Exception as e:
            self._fail("getUpdates", e)
            return None
        self._ok("getUpdates", status, data)
        return self._parse_updates(data)

    def abort_poll(self):
        """Drop a waiting long poll (the socket is needed to send something).
        Nothing is lost: Telegram re-delivers anything we did not acknowledge."""
        if self._pending:
            self.close()

    def save_offset(self):
        """Persist the offset so a command is never executed twice across reboots.
        Called only before acting on a command (a few times a day -> no flash wear)."""
        try:
            with open(self.offset_file, "w") as f:
                f.write(str(self.offset))
        except OSError as e:
            print("TG: could not save offset:", e)

    def close(self):
        self._pending = False  # a waiting long poll dies with its socket
        self._poller = None
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # --------------------------------------------------------------- internals

    def _load_offset(self):
        try:
            with open(self.offset_file) as f:
                return int(f.read().strip())
        except Exception:
            # First boot: -1 = "only the newest pending update, forget older ones".
            return -1

    def _call(self, method, body=None, query=""):
        """One blocking API call. Returns the decoded JSON dict, or None on failure."""
        self.requests += 1
        try:
            status, data = self._request(method, body, query)
        except Exception as e:
            self._fail(method, e)
            return None
        self._ok(method, status, data)
        return data

    def _ok(self, method, status, data):
        self.failures = 0
        self.last_ok = ticks_ms()
        if status != 200 and isinstance(data, dict):
            desc = data.get("description")
            self.last_error = "%s: HTTP %d %s" % (method, status, desc)
            print("TG:", self.last_error)
            if status == 429:
                retry = (data.get("parameters") or {}).get("retry_after", 5)
                self._backoff_until = ticks_add(ticks_ms(), min(int(retry), 60) * 1000)
            elif status >= 400 and method == "getUpdates":
                # e.g. 401 bad token / 409 conflict: don't spin on it
                self._backoff_until = ticks_add(ticks_ms(), 5000)

    def _fail(self, method, e):
        self.close()
        self.failures += 1
        self.last_error = "%s: %r" % (method, e)
        print("TG error:", self.last_error)
        # Retry the long poll after 2, 4, 8 ... 30 s instead of hammering a dead link.
        self._backoff_until = ticks_add(ticks_ms(), min(2 ** min(self.failures, 5), 30) * 1000)
        gc.collect()

    def _parse_updates(self, res):
        if not isinstance(res, dict):
            return None
        if "_truncated" in res:
            # A single update was larger than MAX_BODY. Skip it instead of
            # re-downloading it forever.
            head = res["_truncated"]
            i = head.find(b'"update_id":')
            if i >= 0:
                j = i + 12
                while j < len(head) and head[j] == 32:  # optional space
                    j += 1
                k = j
                while k < len(head) and 48 <= head[k] <= 57:
                    k += 1
                if k > j:
                    self.offset = int(head[j:k]) + 1
                    print("TG: skipped oversized update", self.offset - 1)
            return None

        result = res.get("result")
        if not res.get("ok") or not result:
            return None

        update = result[0]
        uid = update.get("update_id", 0)
        self.offset = uid + 1

        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        name = sender.get("username") or sender.get("first_name") or "?"
        age = None
        date = msg.get("date")
        if date and self.server_time:
            age = self.server_time - date
        return Message(uid, msg.get("text") or "", chat.get("id"), name, age)

    def _payload(self, method, body, query):
        path = self._prefix + method + query
        if body is None:
            return ("GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path, self.host)).encode()
        data = json.dumps(body).encode()
        return ("POST %s HTTP/1.1\r\nHost: %s\r\n"
                "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                % (path, self.host, len(data))).encode() + data

    def _connect(self):
        if self._addr is None:
            self._addr = socket.getaddrinfo(self.host, self.port, 0,
                                            socket.SOCK_STREAM)[0][-1]
        raw = socket.socket()
        try:
            raw.settimeout(self.timeout_s)
            raw.connect(self._addr)
            if self.use_tls:
                self._sock = ssl.wrap_socket(raw, server_hostname=self.host)
            else:
                self._sock = raw
        except Exception:
            raw.close()
            self._addr = None  # the IP may have changed; resolve again next time
            raise
        self.connections += 1

    def _request(self, method, body, query):
        payload = self._payload(method, body, query)

        for attempt in (0, 1):
            reused = self._sock is not None
            if not reused:
                self._connect()
            try:
                self._sock.write(payload)
                line = self._sock.readline()
            except Exception as e:
                line = None
                err = e
            if not line:
                self.close()
                # Nothing came back. On a kept-alive connection that usually means the
                # server dropped it while idle, so retry exactly once on a fresh one --
                # but not after a timeout (a real network problem; retrying doubles the wait).
                if line is None and isinstance(err, OSError) and _is_timeout(err):
                    raise err
                if reused and not attempt:
                    continue
                if line is None:
                    raise err
                raise OSError("connection closed by server")
            try:
                return self._read_response(line)
            except Exception:
                self.close()  # the server answered, so never resend (no duplicate messages)
                raise

    def _read_response(self, status_line):
        s = self._sock
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
            raise OSError("bad status line")
        status = int(parts[1])

        length = None
        chunked = False
        keep_alive = True
        while True:
            line = s.readline()
            if not line:
                raise OSError("connection closed in headers")
            if line in (b"\r\n", b"\n"):
                break
            key, _, value = line.partition(b":")
            key = key.strip().lower()
            value = value.strip()
            if key == b"content-length":
                length = int(value)
            elif key == b"transfer-encoding":
                chunked = b"chunked" in value.lower()
            elif key == b"connection":
                keep_alive = value.lower() != b"close"
            elif key == b"date":
                t = http_date_to_unix(value.decode())
                if t:
                    self.server_time = t

        if chunked:
            body, truncated = self._read_chunked()
        elif length is not None:
            if length > MAX_BODY:
                body, truncated = s.read(512), True
            else:
                body, truncated = s.read(length), False
                if len(body) != length:
                    raise OSError("short body")
        else:  # no length: body ends when the server closes the connection
            body = s.read(MAX_BODY + 1)
            truncated = len(body) > MAX_BODY
            keep_alive = False

        if truncated or not keep_alive:
            self.close()
        if truncated:
            return status, {"_truncated": bytes(body[:512])}
        try:
            return status, json.loads(body)
        except ValueError:
            # e.g. an HTML error page from a proxy
            if status == 200:
                raise OSError("invalid JSON")
            return status, {"ok": False, "description": "non-JSON body"}

    def _read_chunked(self):
        s = self._sock
        out = bytearray()
        while True:
            line = s.readline()
            if not line:
                raise OSError("connection closed in chunk header")
            size = int(line.split(b";")[0].strip(), 16)
            if size == 0:
                # trailers until the empty line
                while True:
                    line = s.readline()
                    if not line or line in (b"\r\n", b"\n"):
                        return out, False
            if len(out) + size > MAX_BODY:
                out.extend(s.read(min(size, 512)))
                return out, True
            chunk = s.read(size)
            if len(chunk) != size:
                raise OSError("short chunk")
            out.extend(chunk)
            s.readline()  # CRLF after the chunk
