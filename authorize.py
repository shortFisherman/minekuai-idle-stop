"""Validate and atomically replace the service's Minekuai web session."""

import argparse
import base64
import getpass
import json
import re
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import unquote

from minekuai import APIError, Minekuai
from monitor import DEFAULT_AUTH, DEFAULT_CONFIG, load_config

JWT_PART = re.compile(r"[A-Za-z0-9_-]+")


def parse_session(value):
    token = unquote(value.strip()).removeprefix("Bearer ").strip()
    pieces = token.split(".")
    if len(pieces) != 3 or not all(JWT_PART.fullmatch(part) for part in pieces):
        raise ValueError("会话格式无效，请复制 Admin-Token 的值")
    try:
        payload = pieces[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        raise ValueError("无法读取会话的客户端标识") from None
    client_id = (claims.get("clientid") or claims.get("clientId")) if isinstance(claims, dict) else None
    if not isinstance(client_id, str) or not client_id or not client_id.isascii() or not client_id.isprintable():
        raise ValueError("无法读取会话的客户端标识")
    return {"token": token, "clientid": client_id}


def replace_auth(instance, path, session):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as stream:
        staged = Path(stream.name)
        try:
            json.dump(session, stream)
            stream.close()
            Minekuai(instance, staged).billing_cycle()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.exit(1, "请在可交互的终端运行授权命令。\n")
    try:
        config = load_config(args.config)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("麦块 Admin-Token（输入不会显示）: ")
        replace_auth(config.instance, args.auth, parse_session(value))
    except (getpass.GetPassWarning, OSError, EOFError, KeyboardInterrupt):
        parser.exit(1, "无法读取或保存麦块会话。请检查终端及目录权限。\n")
    except (ValueError, APIError) as exc:
        parser.exit(1, str(exc) + "\n")
    print("已验证并保存麦块会话。运行中的服务需要重启，才能读取新会话。")


if __name__ == "__main__":
    main()
