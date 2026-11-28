"""Share verified Minekuai game addresses between the monitor and maintenance jobs."""

import json
import logging
import os
import re
import tempfile

from minekuai import APIError, AuthError, RateLimitError, instance_operation_lock

LOG = logging.getLogger("minekuai-inventory")
INSTANCE_ID = re.compile(r"[a-f0-9]{8}")


class Inventory:
    def __init__(self, api, directory):
        self.api = api
        self.directory = directory
        self.path = directory / "instances.json"

    def read(self):
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise APIError("VPS 实例名单不可读，请手动刷新") from None
        if not isinstance(rows, dict):
            raise APIError("VPS 实例名单格式无效，请手动刷新")
        result = {}
        for instance, endpoint in rows.items():
            if not INSTANCE_ID.fullmatch(instance) or (endpoint is not None and (
                not isinstance(endpoint, list) or len(endpoint) != 2
                or not isinstance(endpoint[0], str) or not endpoint[0]
                or type(endpoint[1]) is not int or not 1 <= endpoint[1] <= 65535
            )):
                raise APIError("VPS 实例名单格式无效，请手动刷新")
            result[instance] = tuple(endpoint) if endpoint is not None else None
        return result

    def refresh(self):
        with instance_operation_lock(self.directory, "inventory") as locked:
            if not locked:
                raise APIError("已有实例名单刷新正在进行")
            rows = self.api.billing_instances(strict=True)
            endpoints = {}
            for instance in rows:
                try:
                    endpoints[instance] = self.api.for_instance(instance).instance_endpoint()
                except (AuthError, RateLimitError):
                    raise
                except APIError as exc:
                    endpoints[instance] = None
                    LOG.warning("实例 %s：暂时无法读取游戏地址：%s", instance, exc)
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".instances-", delete=False) as stream:
                path = self.directory / os.path.basename(stream.name)
                try:
                    if os.name == "posix" and os.geteuid() == 0:
                        owner = self.directory.stat()
                        os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
                    json.dump(endpoints, stream)
                    stream.close()
                    path.replace(self.path)
                finally:
                    path.unlink(missing_ok=True)
        LOG.info("已刷新计时卡实例名单，共 %d 台", len(rows))
        return rows, endpoints
