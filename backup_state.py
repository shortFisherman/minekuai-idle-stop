"""Durable cold-backup state and retirement of deleted instances."""

import json
import logging
import os
import re
import shutil

from minekuai import APIError, InstanceNotFound, RateLimitError

LOG = logging.getLogger("minekuai-cold")
RETIRE_AFTER = 30 * 86400
CONFIRM_MISSING_AFTER = 5 * 60
INSTANCE_ID = re.compile(r"[a-f0-9]{8}")


def store_json(path, data):
    pending = path.with_suffix(".tmp")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_state(path):
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(row, dict):
        raise ValueError("备份状态文件格式无效")
    return row


class BackupRetirement:
    def __init__(self, account, instances):
        self.account = account
        self.instances = instances

    def is_gone(self, instance):
        api = self.account.for_instance(instance)
        try:
            detail = api.request("GET", api.panel)
        except InstanceNotFound:
            return True
        api.endpoint_from_panel(detail)
        return False

    def purge(self, directory):
        if (directory.parent.resolve() != self.instances.resolve()
                or directory.is_symlink() or not INSTANCE_ID.fullmatch(directory.name)):
            raise ValueError("无法核对实例备份目录")
        shutil.rmtree(directory)
        LOG.info("实例 %s：已清理本地备份", directory.name)

    def reconcile(self, rows, now):
        for directory in self.instances.iterdir():
            instance = directory.name
            if directory.is_symlink() or not directory.is_dir() or not INSTANCE_ID.fullmatch(instance):
                continue
            try:
                self.reconcile_instance(directory, instance in rows, now)
            except RateLimitError:
                raise
            except (APIError, OSError, ValueError) as exc:
                LOG.warning("实例 %s：暂缓清理备份：%s", instance, exc)

    def reconcile_instance(self, directory, present, now):
        instance = directory.name
        retired = directory / "retired.json"
        if present:
            retired.unlink(missing_ok=True)
            return
        saved = read_state(retired)
        if not self.is_gone(instance):
            retired.unlink(missing_ok=True)
            return
        if not saved:
            store_json(retired, {"first_missing": now})
            return
        confirmed = "confirmed_at" in saved
        since = saved.get("confirmed_at" if confirmed else "first_missing")
        if type(since) not in (int, float) or not 0 < since <= now:
            raise ValueError("备份保留期记录无效")
        if not confirmed and now - since >= CONFIRM_MISSING_AFTER:
            store_json(retired, {"confirmed_at": now})
            LOG.info("实例 %s：开始删除后保留计时", instance)
        if confirmed and now - since >= RETIRE_AFTER:
            fresh = self.account.billing_instances(strict=True)
            if instance not in fresh and self.is_gone(instance):
                self.purge(directory)
