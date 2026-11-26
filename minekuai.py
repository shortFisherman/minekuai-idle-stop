"""Minekuai account API and Minecraft Java server status probes."""

import http.client
import io
import json
import logging
import socket
import struct
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from websockets.sync.client import connect

API_HOST = "api.minekuai.cn"
ORIGIN = "https://minekuai.com"
STATES = {"offline", "starting", "running", "stopping"}
WS_ERRORS = {"jwt error", "token expired", "token expiring", "daemon error"}
INSTALL_EVENTS = {"install started", "install output", "install pull progress", "install completed"}

WS_LOGGER = logging.Logger("minekuai.websocket")
WS_LOGGER.disabled = True


class APIError(Exception):
    pass


class AuthError(APIError):
    pass


class RateLimitError(APIError):
    def __init__(self, retry_after=None):
        delay = 300
        if retry_after:
            try:
                if retry_after.isascii() and retry_after.isdecimal():
                    delay = int(retry_after)
                else:
                    until = parsedate_to_datetime(retry_after)
                    if until.tzinfo is not None:
                        delay = max(0, until.timestamp() - time.time())
            except (OverflowError, OSError, TypeError, ValueError):
                pass
        self.retry_after = max(60, delay)
        super().__init__("麦块返回限流，已延后下一次查询")


class Minekuai:
    def __init__(self, instance, auth_file):
        self.instance = instance
        self.auth_file = auth_file
        self.panel = f"/panel/servers/{instance}"

    def request(self, method, path, *, allow_uncoded=False):
        try:
            auth = json.loads(self.auth_file.read_text(encoding="utf-8"))
            token, client_id = auth["token"], auth["clientid"]
            if not all(isinstance(item, str) and item and item.isascii() and item.isprintable()
                       for item in (token, client_id)):
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError):
            raise AuthError("麦块会话文件缺失或格式无效") from None

        conn = http.client.HTTPSConnection(API_HOST, timeout=10)
        try:
            conn.request(method, path, headers={
                "Authorization": f"Bearer {token}",
                "clientid": client_id,
                "Origin": ORIGIN,
                "Referer": ORIGIN + "/",
                "Accept": "application/json",
                "User-Agent": "Minekuai-Idle-Stop/0.1",
            })
            response = conn.getresponse()
            if response.status in (401, 419):
                raise AuthError("麦块会话失效，需要重新授权")
            if response.status == 429:
                raise RateLimitError(response.getheader("Retry-After"))
            if response.status != 200:
                raise APIError(f"麦块接口返回 HTTP {response.status}")
            body = json.loads(response.read(2_097_153))
        except (OSError, http.client.HTTPException, ValueError):
            raise APIError("麦块接口无法连接或响应格式异常") from None
        finally:
            conn.close()

        if not isinstance(body, dict):
            raise APIError("麦块接口返回的结构异常")
        code = body.get("code")
        if code in (401, 419, "401", "419"):
            raise AuthError("麦块会话失效，需要重新授权")
        if code in (429, "429"):
            raise RateLimitError(response.getheader("Retry-After"))
        if type(code) in (str, int) and str(code) in {"200", "0"}:
            return body.get("data")
        if allow_uncoded and "code" not in body and isinstance(body.get("data"), dict):
            return body["data"]
        safe_code = str(code) if type(code) in (str, int) and str(code).isdigit() else "缺失"
        raise APIError(f"麦块接口未确认成功（业务码 {safe_code}）")

    def billing_cycle(self):
        cards = self.request("GET", "/system/timeBalance/user/userPackages")
        if not isinstance(cards, list):
            raise APIError("计时卡列表结构异常")
        matches = [
            item for card in cards if isinstance(card, dict) and isinstance(card.get("instances"), list)
            for item in card["instances"] if isinstance(item, dict) and item.get("serverId") == self.instance
        ]
        if len(matches) != 1:
            raise APIError("计时卡列表未返回唯一的目标实例")
        item = matches[0]
        status = item.get("timingStatus")
        if type(status) not in (int, str) or str(status) not in {"0", "1"}:
            raise APIError("目标实例计费状态未知")
        if str(status) == "0":
            return None
        cycle = item.get("lastStartTime")
        if type(cycle) not in (int, str) or not str(cycle).strip():
            raise APIError("目标实例正在计费，但无法确认本次计费的开始时间")
        return str(cycle)

    def ensure_available(self):
        info = self.request("GET", self.panel)
        attr = info.get("attributes") if isinstance(info, dict) else None
        flags = ("is_suspended", "is_installing", "is_transferring", "is_node_under_maintenance")
        if (not isinstance(attr, dict) or attr.get("identifier") != self.instance
                or attr.get("status") not in (None, "")
                or any(attr.get(flag) is not False for flag in flags)):
            raise APIError("面板显示实例不可操作，或处于安装、迁移、维护状态")

    def power_state(self):
        info = self.request("GET", self.panel + "/resources")
        attr = info.get("attributes") if isinstance(info, dict) else None
        state = attr.get("current_state") if isinstance(attr, dict) else None
        if not isinstance(state, str) or state not in STATES:
            raise APIError("无法确认目标实例的电源状态")
        return state

    def request_stop(self, before_send):
        data = self.request("GET", self.panel + "/websocket", allow_uncoded=True)
        if not isinstance(data, dict):
            raise APIError("实时控制台凭据格式异常")
        address, token = data.get("socket"), data.get("token")
        try:
            endpoint = urlsplit(address)
            valid = (
                isinstance(address, str) and endpoint.scheme == "wss" and endpoint.hostname
                and endpoint.username is None and endpoint.password is None and not endpoint.fragment
                and not any(char.isspace() for char in address)
                and isinstance(token, str) and token
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise APIError("实时控制台地址或凭据无效")

        try:
            with connect(address, origin=ORIGIN, proxy=None, open_timeout=10,
                         close_timeout=2, max_size=262_144, logger=WS_LOGGER) as ws:
                ws.send(json.dumps({"event": "auth", "args": [token]}))
                deadline = time.monotonic() + 10
                authenticated = False
                while time.monotonic() < deadline:
                    msg = json.loads(ws.recv(timeout=max(0, deadline - time.monotonic())))
                    if not isinstance(msg, dict):
                        continue
                    event = msg.get("event")
                    if not isinstance(event, str):
                        continue
                    if event in WS_ERRORS or event in INSTALL_EVENTS:
                        raise APIError("实时控制台认证失败，或节点存在安装、维护活动")
                    if event == "auth success":
                        authenticated = True
                    args = msg.get("args")
                    if (authenticated and event == "status" and isinstance(args, list)
                            and args and isinstance(args[0], str) and args[0] in STATES):
                        if args[0] != "running":
                            raise APIError("目标实例实时状态已发生变化，取消关服")
                        if before_send():
                            ws.send(json.dumps({"event": "set state", "args": ["stop"]}))
                        return
                raise APIError("未收到实时控制台的运行状态")
        except APIError:
            raise
        except Exception:
            raise APIError("实时控制台连接或发送结果不明，请核对停服状态") from None

    def pause_billing(self):
        self.request("POST", f"/system/timeBalance/user/instance/{self.instance}/stop")


def encode_varint(value):
    encoded = bytearray()
    value &= 0xffffffff
    while value > 0x7f:
        encoded.append((value & 0x7f) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def read_varint(stream):
    value = 0
    for offset in range(0, 35, 7):
        item = stream.read(1)
        if not item:
            raise ValueError("数据包不完整")
        value |= (item[0] & 0x7f) << offset
        if item[0] < 0x80:
            return value
    raise ValueError("数据包长度无效")


def online_count(host, port):
    """Return a confirmed player count, or None for an unknown result."""
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            encoded_host = host.encode("idna")
            handshake = (
                b"\x00" + encode_varint(-1) + encode_varint(len(encoded_host))
                + encoded_host + struct.pack(">H", port) + b"\x01"
            )
            sock.sendall(encode_varint(len(handshake)) + handshake + b"\x01\x00")
            with sock.makefile("rb") as stream:
                size = read_varint(stream)
                if not 0 < size <= 1_048_576:
                    return None
                packet = stream.read(size)
            if len(packet) != size:
                return None
            with io.BytesIO(packet) as body:
                if read_varint(body) != 0:
                    return None
                size = read_varint(body)
                raw = body.read(size)
            if len(raw) != size:
                return None
            count = json.loads(raw)["players"]["online"]
            return count if type(count) is int and count >= 0 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
