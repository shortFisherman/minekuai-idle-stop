"""Monitor Minekuai Minecraft timecard instances and suspend idle billing."""

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from inventory import Inventory
from keepalive import Keepalive, RECHECK_SECONDS, timestamp
from minekuai import APIError, AuthError, Minekuai, RateLimitError, instance_operation_lock, online_count

LOG = logging.getLogger("minekuai-stop")
DEFAULT_CONFIG = Path("/etc/minekuai-stop/config.json")
DEFAULT_AUTH = Path("/etc/minekuai-stop/auth.json")
DEFAULT_STATE = Path("/var/lib/minekuai-stop/state.json")
STAGES = {"stop_requested", "pause_requested"}
FOLLOW_UP_INTERVAL = 30
FOLLOW_UP_WINDOW = 180
MAX_PAUSE_ATTEMPTS = 3
UNREAD_BILLING = object()


class Monitor:
    def __init__(self, api, probe, state_file, idle_seconds, poll_seconds, *,
                 idle_samples=None, dry_run=False, keepalive_days=None, keepalive_state=None):
        self.api = api
        self.probe = probe
        self.state_file = state_file
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self.idle_samples = idle_samples
        self.dry_run = dry_run
        self.reset_idle()
        self.following_up = False
        self.cycle = None
        self.last_message = None
        self.offline_since = None
        self.last_probe_at = None
        self.last_online_count = None
        self.keepalive = (
            Keepalive(api, keepalive_state or state_file.with_name("keepalive.json"), keepalive_days, self.report)
            if keepalive_days is not None and not dry_run else None
        )

    def reset_idle(self):
        self.idle_since = self.last_sample = None
        self.first_empty_at = None
        self.empty_samples = 0

    def report(self, message, *, level=logging.INFO):
        if message != self.last_message:
            LOG.log(level, "实例 %s：%s", self.api.instance, message)
            self.last_message = message

    def read_pending(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            raise APIError("历史操作状态不可读，停止自动操作") from None
        if (not isinstance(data, dict) or data.get("instance") != self.api.instance
                or not isinstance(data.get("stage"), str) or data["stage"] not in STAGES or not isinstance(data.get("cycle"), str)
                or type(data.get("written_at")) not in (int, float)
                or ("pause_attempts" in data and type(data["pause_attempts"]) is not int)):
            raise APIError("历史操作状态无效，停止自动操作")
        return data

    def record(self, stage, cycle, **details):
        row = {"instance": self.api.instance, "stage": stage, "cycle": cycle, "written_at": time.time()}
        row.update(details)
        temporary = self.state_file.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(row, stream)
        temporary.replace(self.state_file)

    def verify_pending(self, cycle, pending):
        if pending["cycle"] != cycle:
            self.following_up = False
            self.state_file.unlink(missing_ok=True)
            self.offline_since = None
            if cycle is None and pending["stage"] == "pause_requested":
                self.report("自动关服及暂停计费已核对完成")
            return

        age = time.time() - pending["written_at"]
        pausing = pending["stage"] == "pause_requested"
        attempts = pending.get("pause_attempts", 1) if pausing else 0

        power = self.api.power_state()
        if power != "offline":
            self.offline_since = None
            if age >= FOLLOW_UP_WINDOW and power in {"running", "starting"}:
                self.state_file.unlink(missing_ok=True)
                self.following_up = False
                self.reset_idle()
                self.report("面板仍未确认停服，重新开始空服检查", level=logging.WARNING)
            else:
                self.report("等待面板确认停服")
            return
        if attempts >= MAX_PAUSE_ATTEMPTS:
            self.following_up = False
            self.report("暂停计费仍未确认，需要在麦块页面核对", level=logging.WARNING)
            return
        if pausing and age < FOLLOW_UP_INTERVAL:
            return
        if self.offline_since is None:
            self.offline_since = time.monotonic()
            self.report("面板已显示停服，复核后将暂停计费")
            return
        if time.monotonic() - self.offline_since < 3:
            return

        self.api.ensure_available()
        if self.probe() is not None or self.api.billing_cycle() != cycle or self.api.power_state() != "offline":
            self.offline_since = None
            self.report("实例状态已变化，暂缓暂停计费", level=logging.WARNING)
            return
        self.record("pause_requested", cycle, pause_attempts=attempts + 1)
        self.following_up = True
        try:
            self.api.pause_billing()
        except (AuthError, RateLimitError):
            raise
        except APIError:
            pass  # The request may have succeeded even if its response was lost.
        if self.api.billing_cycle() is None:
            self.following_up = False
            self.state_file.unlink(missing_ok=True)
            self.offline_since = None
            self.report("自动关服及暂停计费已核对完成")
        else:
            self.report("已发送暂停计费，等待麦块确认")

    def tick(self, cycle=UNREAD_BILLING):
        self.following_up = False
        if cycle is UNREAD_BILLING:
            cycle = self.api.billing_cycle()
        if not self.dry_run:
            pending = self.read_pending()
            if pending is not None:
                self.following_up = time.time() - pending["written_at"] < FOLLOW_UP_WINDOW
                self.verify_pending(cycle, pending)
                return
        if self.keepalive and self.keepalive.step(cycle):
            self.reset_idle()
            return
        if cycle != self.cycle:
            self.cycle = cycle
            self.reset_idle()
        if cycle is None:
            self.report("目标实例已暂停计费，等待下次开服")
            return

        if self.sample_empty():
            self.stop_idle(cycle)

    def sample_empty(self):
        count = self.probe()
        now = time.monotonic()
        self.last_probe_at = now
        self.last_online_count = count
        if self.last_sample is not None and now - self.last_sample > self.poll_seconds + 20:
            self.reset_idle()

        if count is None:
            self.reset_idle()
            self.report("在线人数未知，等待有效查询结果")
            return False
        if count > 0:
            self.reset_idle()
            self.report("有人在线，空闲记录已清除")
            return False
        self.last_sample = now
        if self.idle_since is None:
            self.idle_since = now
            self.first_empty_at = time.time()

        if self.idle_samples is not None:
            self.empty_samples += 1
            if self.empty_samples < self.idle_samples:
                self.report(f"连续 {self.empty_samples}/{self.idle_samples} 次查到 0 人")
                return False
        else:
            if self.idle_since == now:
                self.report(f"在线人数为 0，开始 {self.idle_seconds} 秒空闲计时")
            if now - self.idle_since < self.idle_seconds:
                return False
        return True

    def stop_idle(self, cycle):
        if self.dry_run:
            self.report("已达到空闲阈值，观察模式没有发送关服或停费指令")
            return
        self.api.ensure_available()
        if self.api.power_state() != "running":
            self.reset_idle()
            self.report("实例已离开运行状态，取消本次关服")
            return

        def before_send():
            if self.api.billing_cycle() != cycle or self.probe() != 0:
                self.reset_idle()
                self.report("最终复核发现计费周期或在线人数变化，取消本次关服")
                return False
            self.record("stop_requested", cycle)
            self.following_up = True
            self.report("最终复核为 0 人，正在发送正常关服指令")
            return True

        self.api.request_stop(before_send)

    def run_cycle(self, cycle=UNREAD_BILLING, *, raise_on_limit=False):
        started = time.monotonic()
        try:
            self.tick(cycle)
        except (APIError, OSError) as exc:
            self.reset_idle()
            self.offline_since = None
            message = str(exc) if isinstance(exc, APIError) else "无法读写本地状态文件"
            self.report(message, level=logging.WARNING)
            if isinstance(exc, RateLimitError):
                if raise_on_limit:
                    raise
                if self.keepalive and self.keepalive.following_up:
                    return exc.retry_after
                return max(self.poll_seconds, exc.retry_after)

        interval = self.poll_seconds
        if self.keepalive and self.keepalive.following_up:
            interval = min(interval, RECHECK_SECONDS)
        elif self.following_up:
            interval = min(interval, FOLLOW_UP_INTERVAL)
        return max(1, interval - (time.monotonic() - started))

    def run(self):
        while True:
            time.sleep(self.run_cycle())


class AccountMonitor:
    """Probe cached game endpoints and contact the panel for work that needs it."""

    def __init__(self, config, auth_file, state_file, *, dry_run=False):
        self.config = config
        self.account = Minekuai(None, auth_file, state_directory=state_file.parent)
        self.inventory = Inventory(self.account, state_file.parent)
        self.state_file = state_file
        self.dry_run = dry_run
        self.targets = {}
        self.followups = {}
        self.next_regular = 0
        self.panel_after = 0
        self.last_error = None

    def target(self, instance, endpoint):
        target = self.targets.get(instance)
        if target is not None and target.api.expected_endpoint == endpoint:
            return target
        api = self.account.for_instance(instance)
        api.expected_endpoint = endpoint

        def probe():
            return online_count(*endpoint) if endpoint is not None else None

        state = self.state_file.with_name(f"state-{instance}.json")
        keepalive_state = self.state_file.with_name(f"keepalive-{instance}.json")
        enabled = self.config.keepalive_instances is None or instance in self.config.keepalive_instances
        target = Monitor(
            api, probe, state, self.config.idle_seconds, self.config.poll_seconds,
            idle_samples=self.config.idle_samples, dry_run=self.dry_run,
            keepalive_days=self.config.keepalive_days if enabled else None,
            keepalive_state=keepalive_state,
        )
        self.targets[instance] = target
        return target

    def sync_targets(self, inventory):
        for instance in self.targets.keys() - inventory.keys():
            self.targets.pop(instance)
            self.followups.pop(instance, None)
        for instance, endpoint in inventory.items():
            self.target(instance, endpoint)

    def inspect(self):
        instances = []
        for instance, row in self.account.billing_instances(strict=True).items():
            billing = self.account.cycle_from_record(row)
            target = self.target(instance, self.account.for_instance(instance).instance_endpoint())
            instances.append({
                "instance": instance,
                "billing_active": billing is not None,
                "online_players": target.probe() if billing is not None else None,
                "keepalive_enabled": target.keepalive is not None,
            })
        return instances

    def work_for_target(self, target, regular):
        if not self.dry_run and target.read_pending() is not None:
            return "finish_stop"
        if target.keepalive:
            record = target.keepalive.read()
            if "requested_at" in record:
                return "finish_keepalive"
            if regular and time.time() >= record["next_after"]:
                return "check_keepalive"
        if regular and target.sample_empty():
            return "stop"
        return None

    def perform(self, target, cycle, work):
        target.following_up = False
        if work == "finish_stop":
            pending = target.read_pending()
            if pending is not None:
                target.following_up = time.time() - pending["written_at"] < FOLLOW_UP_WINDOW
                target.verify_pending(cycle, pending)
        elif work in ("finish_keepalive", "check_keepalive"):
            target.reset_idle()
            if work == "check_keepalive" and cycle is not None:
                # The retention clock starts only when the current billing cycle ends.
                target.keepalive.clear(target.keepalive.age)
            else:
                target.keepalive.step(cycle)
        elif cycle is not None:
            billing_start = timestamp(int(cycle) if cycle.isdecimal() else cycle)
            if target.first_empty_at is None or billing_start is None or billing_start > target.first_empty_at:
                target.reset_idle()
                target.report("无法确认两次空服查询属于当前计费周期，重新计数")
            else:
                target.stop_idle(cycle)
        else:
            target.reset_idle()

    def run_cycle(self):
        started = time.monotonic()
        regular = started >= self.next_regular
        if regular:
            self.next_regular = started + self.config.poll_seconds
        try:
            inventory = self.inventory.read()
            records = None
            if inventory is None:
                if not regular or started < self.panel_after:
                    return max(1, self.next_regular - started)
                records, inventory = self.inventory.refresh()
            self.sync_targets(inventory)
            work = {}
            for instance, target in self.targets.items():
                if regular or self.followups.get(instance, float("inf")) <= started:
                    try:
                        action = self.work_for_target(target, regular)
                    except (APIError, OSError) as exc:
                        target.report(str(exc), level=logging.WARNING)
                        action = None
                    if action is not None:
                        work[instance] = action
                    else:
                        self.followups.pop(instance, None)

            if regular:
                probes = [target for target in self.targets.values()
                          if target.api.expected_endpoint is not None
                          and target.last_probe_at is not None and target.last_probe_at >= started]
                LOG.info("游戏人数检查完成：探测 %d 台，有效人数 %d 台，名单共 %d 台",
                         len(probes), sum(target.last_online_count is not None for target in probes),
                         len(self.targets))

            if work and started >= self.panel_after:
                if records is None and "check_keepalive" in work.values():
                    records, inventory = self.inventory.refresh()
                    self.sync_targets(inventory)
                if records is None:
                    records = self.account.billing_instances(strict=True)
                for instance, action in work.items():
                    if instance not in records:
                        if instance in self.targets:
                            self.targets[instance].reset_idle()
                        self.followups.pop(instance, None)
                        continue
                    target = self.targets[instance]
                    try:
                        cycle = self.account.cycle_from_record(records[instance])
                        with instance_operation_lock(self.state_file.parent, instance) as locked:
                            if locked:
                                self.perform(target, cycle, action)
                            else:
                                target.reset_idle()
                                target.following_up = False
                    except (AuthError, RateLimitError):
                        raise
                    except (APIError, OSError) as exc:
                        target.reset_idle()
                        target.report(str(exc), level=logging.WARNING)
                    if target.keepalive and target.keepalive.following_up:
                        self.followups[instance] = time.monotonic() + RECHECK_SECONDS
                    elif target.following_up:
                        self.followups[instance] = time.monotonic() + FOLLOW_UP_INTERVAL
                    else:
                        self.followups.pop(instance, None)
                self.last_error = None
        except (APIError, OSError) as exc:
            if str(exc) != self.last_error:
                LOG.warning("账号计时卡查询：%s", exc)
                self.last_error = str(exc)
            for target in self.targets.values():
                target.reset_idle()
            if isinstance(exc, RateLimitError):
                self.panel_after = time.monotonic() + exc.retry_after
            else:
                self.panel_after = time.monotonic() + self.config.poll_seconds
        if self.followups:
            next_panel = max(self.panel_after, min(self.followups.values()))
            return max(1, min(next_panel, self.next_regular) - time.monotonic())
        return max(1, self.next_regular - time.monotonic())

    def run(self):
        while True:
            # Pick up the roster written by a separate maintenance or refresh command.
            time.sleep(min(60, self.run_cycle()))


@dataclass(frozen=True)
class Config:
    instance: str | None
    host: str | None
    port: int | None
    idle_seconds: int
    poll_seconds: int
    idle_samples: int | None = None
    keepalive_days: int | None = None
    keepalive_instances: tuple[str, ...] | None = None


def load_config(path):
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError
        scope = cfg.get("scope", "instance")
        if scope not in ("instance", "account"):
            raise ValueError
        instance = host = port = None
        if scope == "instance":
            instance, host, port = cfg["instance"], cfg["host"], cfg["port"]
            if (not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{8}", instance)
                    or not isinstance(host, str) or not host or any(char.isspace() for char in host)
                    or type(port) is not int or not 1 <= port <= 65535):
                raise ValueError
        elif any(key in cfg for key in ("instance", "host", "port")):
            raise ValueError
        idle = cfg.get("idle_seconds", 600)
        poll = cfg.get("poll_seconds", 30)
        samples = cfg.get("idle_samples")
        keepalive = cfg.get("keepalive_days")
        allowed = cfg.get("keepalive_instances")
        if allowed is not None:
            if (scope != "account" or not isinstance(allowed, list)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{8}", value)
                           for value in allowed)):
                raise ValueError
            allowed = tuple(allowed)
        if (
            type(idle) is not int or idle < 5
            or type(poll) is not int or not 5 <= poll <= 3600
            or (samples is None and (poll > 120 or idle < poll))
            or (samples is not None and (type(samples) is not int or samples < 2))
            or (keepalive is not None and (type(keepalive) is not int or not 1 <= keepalive <= 6))
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("服务配置无效，请核对实例 ID、游戏地址及计时间隔") from None
    return Config(instance, host, port, idle, poll, samples, keepalive, allowed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read and report the current status once")
    mode.add_argument("--refresh-instances", action="store_true", help="refresh cached account instances without changing billing")
    mode.add_argument("--dry-run", action="store_true", help="monitor without sending shutdown or billing commands")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    threshold = (
        {"idle_seconds": config.idle_seconds} if config.idle_samples is None
        else {"idle_samples": config.idle_samples}
    )

    if config.instance is None:
        account = AccountMonitor(config, args.auth, args.state, dry_run=args.dry_run)
        if args.refresh_instances:
            try:
                _, inventory = account.inventory.refresh()
                print(json.dumps({"instances": len(inventory),
                                  "game_addresses": sum(endpoint is not None for endpoint in inventory.values())}))
            except (APIError, OSError) as exc:
                parser.exit(1, str(exc) + "\n")
            return
        if args.check:
            try:
                print(json.dumps({
                    "scope": "account", "instances": account.inspect(), **threshold,
                    "poll_seconds": config.poll_seconds, "keepalive_days": config.keepalive_days,
                }, ensure_ascii=False))
            except APIError as exc:
                LOG.error("%s", exc)
                raise SystemExit(1) from None
            return
        LOG.info("账号级监测启动，每 %d 秒直接查询游戏服人数", config.poll_seconds)
        account.run()
        return

    if args.refresh_instances:
        parser.error("--refresh-instances 适用于账号模式")
    api = Minekuai(config.instance, args.auth, state_directory=args.state.parent)

    def probe():
        return online_count(config.host, config.port)

    if args.check:
        try:
            active = api.billing_cycle() is not None
            count = probe() if active else None
            print(json.dumps({
                "instance": config.instance, "billing_active": active, "online_players": count,
                **threshold, "poll_seconds": config.poll_seconds, "keepalive_days": config.keepalive_days,
            }, ensure_ascii=False))
        except APIError as exc:
            LOG.error("%s", exc)
            raise SystemExit(1) from None
        return

    criterion = (
        f"连续无人 {config.idle_seconds} 秒" if config.idle_samples is None
        else f"连续 {config.idle_samples} 次查到 0 人"
    )
    LOG.info("监控启动。实例 %s，关服条件为%s，每 %d 秒检查一次%s",
             config.instance, criterion, config.poll_seconds, "（观察模式）" if args.dry_run else "")
    if config.keepalive_days is not None:
        LOG.info("计时卡保活已配置。暂停满 %d 天后短暂恢复计费，确认后立即暂停", config.keepalive_days)
    Monitor(
        api, probe, args.state, config.idle_seconds, config.poll_seconds,
        idle_samples=config.idle_samples, dry_run=args.dry_run, keepalive_days=config.keepalive_days,
    ).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
