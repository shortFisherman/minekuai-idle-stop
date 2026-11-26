"""Monitor one explicitly configured Minekuai Minecraft instance."""

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from minekuai import APIError, Minekuai, RateLimitError, online_count

LOG = logging.getLogger("minekuai-stop")
DEFAULT_CONFIG = Path("/etc/minekuai-stop/config.json")
DEFAULT_AUTH = Path("/etc/minekuai-stop/auth.json")
DEFAULT_STATE = Path("/var/lib/minekuai-stop/state.json")
STAGES = {"stop_requested", "pause_requested"}
FOLLOW_UP_INTERVAL = 30
FOLLOW_UP_WINDOW = 180


class Monitor:
    def __init__(self, api, probe, state_file, idle_seconds, poll_seconds, *,
                 idle_samples=None, dry_run=False):
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

    def reset_idle(self):
        self.idle_since = self.last_sample = None
        self.empty_samples = 0

    def report(self, message, *, level=logging.INFO):
        if message != self.last_message:
            LOG.log(level, message)
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
                or type(data.get("written_at")) not in (int, float)):
            raise APIError("历史操作状态无效，停止自动操作")
        return data

    def record(self, stage, cycle):
        row = {"instance": self.api.instance, "stage": stage, "cycle": cycle, "written_at": time.time()}
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

        if pending["stage"] == "pause_requested":
            if time.time() - pending["written_at"] >= 120:
                self.report("暂停计费仍未确认，需要在麦块页面核对", level=logging.WARNING)
            else:
                self.report("已发送暂停计费，等待麦块确认")
            return

        if self.api.power_state() != "offline":
            self.offline_since = None
            if time.time() - pending["written_at"] >= 180:
                self.report("关服仍未确认，继续核对状态，需要检查麦块面板", level=logging.WARNING)
            else:
                self.report("已发送关服指令，等待面板显示已停止")
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
        self.record("pause_requested", cycle)
        self.following_up = True
        self.api.pause_billing()
        if self.api.billing_cycle() is None:
            self.following_up = False
            self.state_file.unlink(missing_ok=True)
            self.offline_since = None
            self.report("自动关服及暂停计费已核对完成")
        else:
            self.report("已发送暂停计费，等待麦块确认")

    def tick(self):
        self.following_up = False
        cycle = self.api.billing_cycle()
        if not self.dry_run:
            pending = self.read_pending()
            if pending is not None:
                self.following_up = time.time() - pending["written_at"] < FOLLOW_UP_WINDOW
                self.verify_pending(cycle, pending)
                return
        if cycle != self.cycle:
            self.cycle = cycle
            self.reset_idle()
        if cycle is None:
            self.report("目标实例已暂停计费，等待下次开服")
            return

        count = self.probe()
        now = time.monotonic()
        if self.last_sample is not None and now - self.last_sample > self.poll_seconds + 20:
            self.reset_idle()

        if count is None:
            self.reset_idle()
            self.report("在线人数未知，等待有效查询结果")
            return
        if count > 0:
            self.reset_idle()
            self.report("有人在线，空闲记录已清除")
            return
        self.last_sample = now

        if self.idle_samples is not None:
            self.empty_samples += 1
            if self.empty_samples < self.idle_samples:
                self.report(f"连续 {self.empty_samples}/{self.idle_samples} 次查到 0 人")
                return
        else:
            if self.idle_since is None:
                self.idle_since = now
                self.report(f"在线人数为 0，开始 {self.idle_seconds} 秒空闲计时")
                return
            if now - self.idle_since < self.idle_seconds:
                return

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

    def run_cycle(self):
        started = time.monotonic()
        try:
            self.tick()
        except (APIError, OSError) as exc:
            self.reset_idle()
            self.offline_since = None
            message = str(exc) if isinstance(exc, APIError) else "无法读写本地状态文件"
            self.report(message, level=logging.WARNING)
            if isinstance(exc, RateLimitError):
                return max(self.poll_seconds, exc.retry_after)

        interval = min(self.poll_seconds, FOLLOW_UP_INTERVAL) if self.following_up else self.poll_seconds
        return max(1, interval - (time.monotonic() - started))

    def run(self):
        while True:
            time.sleep(self.run_cycle())


@dataclass(frozen=True)
class Config:
    instance: str
    host: str
    port: int
    idle_seconds: int
    poll_seconds: int
    idle_samples: int | None = None


def load_config(path):
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        instance, host, port = cfg["instance"], cfg["host"], cfg["port"]
        idle = cfg.get("idle_seconds", 600)
        poll = cfg.get("poll_seconds", 30)
        samples = cfg.get("idle_samples")
        if (
            not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{8}", instance)
            or not isinstance(host, str) or not host or any(char.isspace() for char in host)
            or type(port) is not int or not 1 <= port <= 65535
            or type(idle) is not int or idle < 5
            or type(poll) is not int or not 5 <= poll <= 3600
            or (samples is None and (poll > 120 or idle < poll))
            or (samples is not None and (type(samples) is not int or samples < 2))
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("服务配置无效，请核对实例 ID、游戏地址及计时间隔") from None
    return Config(instance, host, port, idle, poll, samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth", type=Path, default=DEFAULT_AUTH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read and report the current status once")
    mode.add_argument("--dry-run", action="store_true", help="monitor without sending shutdown or billing commands")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    api = Minekuai(config.instance, args.auth)
    threshold = (
        {"idle_seconds": config.idle_seconds} if config.idle_samples is None
        else {"idle_samples": config.idle_samples}
    )

    def probe():
        return online_count(config.host, config.port)

    if args.check:
        try:
            active = api.billing_cycle() is not None
            count = probe() if active else None
            print(json.dumps({
                "instance": config.instance, "billing_active": active, "online_players": count,
                **threshold, "poll_seconds": config.poll_seconds,
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
    Monitor(
        api, probe, args.state, config.idle_seconds, config.poll_seconds,
        idle_samples=config.idle_samples, dry_run=args.dry_run,
    ).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
