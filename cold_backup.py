"""Weekly cold export of complete Minekuai instances."""

import argparse
import logging
import signal
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import UUID

from backup_files import FileSource
from backup_state import BackupRetirement, INSTANCE_ID, read_state, store_json
from cold_zip import LOG, RESERVE_BYTES, archive_name, available, prune_previous, publish, stored_matches, verify
from keepalive import CHINA_TIME, timestamp
from minekuai import APIError, Minekuai, RateLimitError, instance_operation_lock, online_count

MAX_SECONDS = 45 * 60
MIN_DAYS = 7


class ColdBackup:
    def __init__(self, auth, root, monitor_state):
        self.root = root
        self.monitor_state = monitor_state
        self.account = Minekuai("account", auth)
        self.instances = root / "instances"
        self.retirement = BackupRetirement(self.account, self.instances)
        self.instances.mkdir(parents=True, exist_ok=True)

    def area(self, instance):
        if not INSTANCE_ID.fullmatch(instance):
            raise ValueError("无效的实例 ID")
        path = self.instances / instance
        if path.is_symlink():
            raise ValueError("实例备份目录不能是符号链接")
        path.mkdir(exist_ok=True)
        return path

    def api_for(self, instance):
        return Minekuai(instance, self.account.auth_file)

    def settle(self, api, journal):
        record = read_state(journal)
        if not record:
            return
        cycle = api.billing_cycle(timeout=5)
        if cycle is None:
            # A resume with ambiguous acknowledgment may still be in progress.
            if "cycle" in record or time.time() >= record["requested_at"] + 25:
                journal.unlink(missing_ok=True)
            return
        if "cycle" not in record:
            when = timestamp(cycle)
            if when is None or not -3 <= when - record["requested_at"] <= 25:
                journal.unlink(missing_ok=True)
                LOG.warning("实例 %s：计费开启时间不匹配，交由用户操作", api.instance)
                return
            record["cycle"] = cycle
            store_json(journal, record)
        if cycle != record["cycle"] or api.power_state(timeout=5) != "offline":
            journal.unlink(missing_ok=True)
            LOG.warning("实例 %s：计费周期或电源状态已变更，结束备份窗口", api.instance)
            return
        if api.billing_cycle(timeout=5) == cycle and api.power_state(timeout=5) == "offline":
            try:
                api.pause_billing(timeout=5)
            except RateLimitError:
                raise
            except APIError:
                pass
        # Billing state can lag behind the successful pause response.
        deadline = time.monotonic() + 12
        while True:
            confirmed = api.billing_cycle(timeout=5)
            if confirmed is None:
                journal.unlink(missing_ok=True)
                LOG.info("实例 %s：已核对暂停计费", api.instance)
                return
            if confirmed != cycle or time.monotonic() >= deadline:
                return
            time.sleep(1)

    def recover(self):
        for directory in self.instances.iterdir():
            journal = directory / "billing.json"
            if directory.is_symlink() or not INSTANCE_ID.fullmatch(directory.name) or not journal.is_file():
                continue
            try:
                with instance_operation_lock(self.monitor_state, directory.name) as locked:
                    if locked:
                        self.settle(self.api_for(directory.name), journal)
            except RateLimitError:
                raise
            except (APIError, OSError, ValueError) as exc:
                LOG.warning("实例 %s：待重试计费收尾：%s", directory.name, exc)

    @staticmethod
    def intact(api, cycle):
        if api.billing_cycle(timeout=6) != cycle or api.power_state(timeout=6) != "offline":
            raise APIError("实例的计费或停服状态发生变化")

    def open_window(self, api, journal):
        request = time.time()
        store_json(journal, {"requested_at": request})
        try:
            api.resume_billing()
        except RateLimitError:
            raise
        except APIError:
            pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            cycle = api.billing_cycle(timeout=5)
            if cycle is not None:
                started = timestamp(cycle)
                if started is None or not -3 <= started - request <= 25:
                    raise APIError("无法确认本次计费由冷备份开启")
                store_json(journal, {"requested_at": request, "cycle": cycle})
                self.intact(api, cycle)
                return cycle
            time.sleep(1)
        raise APIError("开启计费未获得确认，等待恢复任务复查")

    @staticmethod
    def list_tasks(source):
        data = source.api.request("GET", source.path + "/tasks")
        if not isinstance(data, dict) or data.get("supported") is not True or not isinstance(data.get("tasks"), list):
            raise APIError("麦块节点未确认支持文件压缩任务")
        return data["tasks"]

    def request_archive(self, source, area, resources):
        tasks = self.list_tasks(source)
        if any(isinstance(item, dict) and item.get("state") in {"queued", "pending", "running"}
               for item in tasks):
            raise APIError("文件管理已有后台任务，稍后再备份")
        entries = source.list(PurePosixPath("/"))
        if not entries:
            raise APIError("面板根目录为空")
        if any(entry["is_symlink"] for entry in entries.values()):
            raise APIError("面板根目录存在符号链接，需要核对整服打包范围")
        used = resources["used"]
        if not 0 < used or resources["limit"] - used < used + RESERVE_BYTES:
            raise APIError("麦块实例的可用空间不足以安全生成临时 ZIP")
        available(area, used)
        LOG.info("实例 %s：面板已确认停服，正在打包 %d 个根目录条目",
                 source.api.instance, len(entries))
        pending = area / "task.json"
        record = {"roots": sorted(entries)}
        store_json(pending, record)
        task = source.api.request(
            "POST", source.path + "/tasks/compress",
            payload={"root": "/", "files": [quote(name, safe="") for name in record["roots"]], "waitMs": 0},
        )
        try:
            record["id"] = str(UUID(task["id"]))
        except (TypeError, KeyError, ValueError, AttributeError):
            raise APIError("压缩任务提交状态不明，保留现场") from None
        store_json(pending, record)
        return record

    def finish_archive(self, source, area, record, cycle):
        pending = area / "task.json"
        name = record.get("archive")
        if not name:
            task_id = record.get("id")
            if not task_id:
                raise APIError("上次压缩任务提交结果不明，需要核对面板任务")
            while True:
                task = next((item for item in self.list_tasks(source)
                             if isinstance(item, dict) and item.get("id") == task_id), None)
                if not task:
                    raise APIError("麦块未返回本轮压缩任务")
                status = task.get("state")
                if status == "done":
                    break
                if status not in {"queued", "pending", "running"}:
                    pending.unlink(missing_ok=True)
                    raise APIError("麦块压缩任务失败或被取消")
                if source.api.power_state(timeout=5) != "offline":
                    raise APIError("检测到开服，暂停本轮备份")
                time.sleep(5)
            remote = task.get("archive")
            if (task.get("kind") != "compress" or task.get("root") != "/"
                    or type(remote) is not str or not remote.startswith("/") or not archive_name(remote[1:])):
                raise APIError("无法核对属于本轮的根目录压缩包")
            self.intact(source.api, cycle)
            name = remote[1:]
            record["archive"] = name
            store_json(pending, record)
        if not archive_name(name):
            raise APIError("无效的待处理压缩包记录")
        latest = read_state(area / "latest.json")
        final = area / name
        have_zip = latest.get("file") == name and stored_matches(final, latest)
        remote = source.list(PurePosixPath("/")).get(name)
        if not remote:
            if have_zip:
                prune_previous(area, final)
            pending.unlink(missing_ok=True)
            if have_zip:
                return
            raise APIError("待下载压缩包已从麦块消失")
        if not remote["is_file"] or remote["is_symlink"] or type(remote.get("size")) is not int:
            raise APIError("无法核对麦块压缩包大小")
        size = remote["size"]
        if not have_zip:
            if size <= 0:
                raise APIError("麦块压缩包为空")
            temp = area / f".{name}.incoming"
            partial = temp.with_suffix(".part")
            received = partial.stat().st_size if partial.is_file() else 0
            available(area, max(1, size - received) if received <= size else size)
            LOG.info("实例 %s：麦块 ZIP 已生成，正在下载 %d 字节", source.api.instance, size)
            try:
                source.download(PurePosixPath("/") / name, temp, size, resume=True)
                checksum = verify(temp, size, record["roots"])
                final = publish(area, temp, name)
                today = datetime.now(CHINA_TIME).date().isoformat()
                store_json(area / "latest.json", {"file": name, "size": size, "sha256": checksum, "date": today})
                LOG.info("实例 %s：整服 ZIP 已校验，保留最新一份", source.api.instance)
            finally:
                temp.unlink(missing_ok=True)
        prune_previous(area, final)
        self.intact(source.api, cycle)
        source.api.request("POST", source.path + "/delete",
                           payload={"root": "/", "files": [quote(name, safe="")]}, allow_empty=True)
        if name in source.list(PurePosixPath("/")):
            raise APIError("麦块临时 ZIP 仍存在，稍后再清理")
        pending.unlink(missing_ok=True)
        LOG.info("实例 %s：已核对麦块临时 ZIP 清理完成", source.api.instance)

    def run_instance(self, instance, *, force=False):
        area = self.area(instance)
        journal = area / "billing.json"
        last = read_state(area / "latest.json")
        task = area / "task.json"
        if not force and not task.exists() and archive_name(last.get("file")) and (area / last["file"]).is_file():
            saved = datetime.fromisoformat(last["date"]).date()
            if (datetime.now(CHINA_TIME).date() - saved).days < MIN_DAYS:
                return
        with instance_operation_lock(self.monitor_state, instance) as locked:
            if not locked:
                return
            keepalive = read_state(self.monitor_state / f"keepalive-{instance}.json")
            if "requested_at" in keepalive or (self.monitor_state / f"state-{instance}.json").is_file():
                return
            api = self.api_for(instance)
            if journal.exists():
                self.settle(api, journal)
                if journal.exists():
                    return
            if api.billing_cycle() is not None:
                LOG.info("实例 %s：用户计费已开启，跳过夜间备份", instance)
                return
            detail = api.request("GET", api.panel)
            endpoint = api.endpoint_from_panel(detail)
            if online_count(*endpoint) is not None:
                return
            signal.alarm(MAX_SECONDS)
            try:
                cycle = self.open_window(api, journal)
                detail = api.request("GET", api.panel)
                source = FileSource(api, detail)
                old_task = read_state(task)
                if not old_task:
                    raw = api.request("GET", api.panel + "/resources")["attributes"]
                    if raw["current_state"] != "offline":
                        raise APIError("目标实例尚未停服")
                    used = raw["resources"]["disk_bytes"]
                    limit = detail["attributes"]["limits"]["disk"]
                    if type(used) is not int or type(limit) is not int:
                        raise APIError("无法核对麦块磁盘空间")
                    old_task = self.request_archive(source, area, {"used": used, "limit": limit * 1024**2})
                self.finish_archive(source, area, old_task, cycle)
            finally:
                signal.alarm(0)
                self.settle(api, journal)

    def run(self):
        self.recover()
        rows = self.account.billing_instances(strict=True)
        self.retirement.reconcile(rows, time.time())
        for instance in rows:
            if datetime.now(CHINA_TIME).hour >= 4:
                LOG.info("夜间备份窗口结束，剩余实例等下次凌晨")
                return
            try:
                self.run_instance(instance)
            except RateLimitError:
                raise
            except (APIError, OSError, ValueError, KeyError, TypeError) as exc:
                LOG.warning("实例 %s：冷备份待重试：%s", instance, exc)

    def purge_retired(self, instance):
        if not INSTANCE_ID.fullmatch(instance):
            raise ValueError("无效的实例 ID")
        area = self.instances / instance
        with instance_operation_lock(self.monitor_state, instance) as locked:
            if not locked:
                raise APIError("该实例正在备份或清理，请稍后重试")
            if (not area.is_dir() or area.is_symlink()
                    or instance in self.account.billing_instances(strict=True)
                    or not self.retirement.is_gone(instance)):
                raise APIError("只允许清理已从账号删除的实例备份")
            self.retirement.purge(area)


def expired(_signal, _frame):
    raise APIError("本次冷备份到达运行上限")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "recover", "now", "purge-retired"))
    parser.add_argument("instance", nargs="?")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("/var/lib/minekuai-cold"))
    parser.add_argument("--auth", type=Path, default=Path("/etc/minekuai-stop/auth.json"))
    parser.add_argument("--monitor-state", type=Path, default=Path("/var/lib/minekuai-stop"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    worker = ColdBackup(args.auth, args.root, args.monitor_state)
    try:
        if args.action == "recover":
            worker.recover()
        elif args.action == "now":
            if not args.instance or args.instance not in worker.account.billing_instances(strict=True):
                parser.error("请提供当前账号的有效计时卡实例 ID")
            worker.run_instance(args.instance, force=True)
        elif args.action == "purge-retired":
            if not args.confirm or not args.instance:
                parser.error("请确认要提前清理的实例 ID 并指定 --confirm")
            worker.purge_retired(args.instance)
        else:
            worker.run()
    except (APIError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
