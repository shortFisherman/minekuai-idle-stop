"""Read Minekuai panel file lists and download signed archives."""

import http.client
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from minekuai import APIError, RateLimitError

INSTANCE_FLAGS = ("is_suspended", "is_installing", "is_transferring", "is_node_under_maintenance")


def safe_name(value):
    return (isinstance(value, str) and value not in ("", ".", "..")
            and "/" not in value and "\\" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class FileSource:
    def __init__(self, api, detail):
        self.api = api
        api.endpoint_from_panel(detail)
        attrs = detail["attributes"]
        if any(attrs.get(flag) is not False for flag in INSTANCE_FLAGS):
            raise APIError("麦块实例暂不可读取")
        try:
            identifier = UUID(attrs["uuid"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise APIError("文件面板缺少有效实例标识") from None
        if not str(identifier).startswith(api.instance + "-"):
            raise APIError("文件面板实例标识不匹配")
        self.path = f"/panel/servers/{identifier}/files"
        self.node_host = (attrs.get("sftp_details") or {}).get("ip")

    def list(self, path):
        data = self.api.request("GET", self.path + "/list?" + urlencode({"directory": str(path)}),
                                allow_uncoded=True)
        if isinstance(data, dict) and data.get("object") == "list":
            metadata = data.get("meta", {})
            pagination = metadata.get("pagination", {}) if isinstance(metadata, dict) else None
            if not isinstance(pagination, dict) or pagination.get("total_pages", 1) != 1:
                raise APIError("文件列表需要分页，跳过不完整存档")
            data = data.get("data")
        if not isinstance(data, list):
            raise APIError("文件列表格式异常")
        rows = {}
        for row in data:
            item = row.get("attributes") if isinstance(row, dict) else None
            name = item.get("name") if isinstance(item, dict) else None
            if (not safe_name(name) or name in rows or type(item.get("is_file")) is not bool
                    or type(item.get("is_symlink")) is not bool):
                raise APIError("文件列表含未知或重复条目")
            rows[name] = item
        return rows

    def download(self, path, destination, size, *, resume=False):
        partial = destination.with_suffix(".part")
        offset = partial.stat().st_size if resume and partial.is_file() else 0
        if offset == size and offset:
            partial.replace(destination)
            return
        if offset > size:
            partial.unlink()
            offset = 0
        args = urlencode({"file": str(path)})
        signed = self.api.request("GET", self.path + "/download?" + args, allow_uncoded=True)
        self.api.ensure_session_usable()
        fields = signed.get("attributes", signed) if isinstance(signed, dict) else {}
        try:
            url = fields["url"]
            target = urlsplit(url)
            if target.scheme != "https" or target.username or target.fragment:
                raise ValueError
            host = target.hostname or ""
            if not (host == self.node_host or any(host == domain or host.endswith("." + domain)
                    for domain in ("minekuai.com", "minekuai.cn", "tudouai.cn"))):
                raise ValueError
            conn = http.client.HTTPSConnection(host, target.port or 443, timeout=20)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise APIError("麦块未提供可信的 HTTPS 下载地址") from None
        try:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            conn.request("GET", target.path + ("?" + target.query if target.query else ""), headers=headers)
            response = conn.getresponse()
            if response.status == 429:
                raise RateLimitError(response.getheader("Retry-After"))
            if response.status == 206 and offset:
                if response.getheader("Content-Range") != f"bytes {offset}-{size - 1}/{size}":
                    partial.unlink(missing_ok=True)
                    raise APIError("麦块返回的续传范围与原 ZIP 不符")
            elif response.status == 200:
                offset = 0
            else:
                raise APIError(f"存档文件下载返回 HTTP {response.status}")
            length = offset
            with partial.open("ab" if offset else "wb") as stream:
                while piece := response.read(1024 * 1024):
                    length += len(piece)
                    if length > size:
                        partial.unlink(missing_ok=True)
                        raise APIError("下载期间源文件大小发生变化")
                    stream.write(piece)
            if length != size:
                raise APIError("下载文件大小与面板记录不匹配")
            partial.replace(destination)
        except (OSError, http.client.HTTPException):
            raise APIError("存档文件下载未完成") from None
        finally:
            conn.close()
            if not resume:
                partial.unlink(missing_ok=True)
