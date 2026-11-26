"""Refresh instance retention using a short, billing-only transaction."""

from datetime import datetime, timedelta, timezone
import json
import time

from minekuai import APIError, AuthError, RateLimitError

CHINA_TIME = timezone(timedelta(hours=8))
CONFIRM_SECONDS = 20
RECHECK_SECONDS = 5
RECOVERY_SECONDS = 180


def timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIME)).timestamp()
    if type(value) is int and value > 0:
        return value / 1000 if value >= 1_000_000_000_000 else value
    return None


class Keepalive:
    def __init__(self, api, path, days, report):
        self.api = api
        self.path = path
        self.age = days * 86400
        self.report = report
        self.recover_until = 0

    @property
    def following_up(self):
        return time.time() < self.recover_until

    def read(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"next_after": 0}
        except (OSError, ValueError):
            raise APIError("保活状态不可读，暂停保活") from None
        if (not isinstance(record, dict) or record.get("instance") != self.api.instance
                or type(record.get("next_after")) not in (int, float)
                or ("requested_at" in record and type(record["requested_at"]) not in (int, float))
                or ("cycle" in record and not isinstance(record["cycle"], str))
                or ("pause_sent" in record and type(record["pause_sent"]) is not bool)
                or ("pause_attempts" in record and type(record["pause_attempts"]) is not int)
                or ("last_pause_attempt" in record and type(record["last_pause_attempt"]) not in (int, float))):
            raise APIError("保活状态无效，暂停保活")
        return record

    def save(self, record):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"instance": self.api.instance, **record}, stream)
        temporary.replace(self.path)

    def clear(self, delay):
        self.recover_until = 0
        self.save({"next_after": time.time() + delay})

    def latest_pause(self):
        ends = [timestamp(value) for value in self.api.recent_billing_ends()]
        valid = [value for value in ends if value is not None and 0 < value <= time.time() + 10]
        if not valid:
            raise APIError("麦块没有返回可确认的停费时间，暂缓保活")
        return max(valid)

    def step(self, cycle):
        """Return True while handling a transaction; False permits ordinary idle checks."""
        record = self.read()
        now = time.time()
        if "requested_at" in record:
            self.recover_until = record["requested_at"] + RECOVERY_SECONDS
            self.confirm(record, cycle)
            return True
        self.recover_until = 0
        if cycle is not None or now < record["next_after"]:
            return False

        due = self.latest_pause() + self.age
        if now < due:
            self.save({"next_after": due})
            return False
        if self.api.billing_cycle(timeout=4) is not None:
            return False

        self.refresh()
        return True

    def refresh(self):
        started = time.monotonic()
        record = {"next_after": 0, "requested_at": time.time(), "pause_sent": False}
        self.save(record)
        self.recover_until = record["requested_at"] + RECOVERY_SECONDS
        self.report("计时卡保活：正在短暂开启计费")
        try:
            self.api.resume_billing()
        except (AuthError, RateLimitError):
            raise
        except APIError:
            self.report("开启计费结果未确认，正在查询实际状态")
        deadline = started + CONFIRM_SECONDS

        while time.monotonic() < deadline:
            try:
                cycle = self.api.billing_cycle(timeout=4)
                if self.confirm(record, cycle):
                    return
            except (AuthError, RateLimitError):
                raise
            except APIError:
                pass
            time.sleep(0.5)
        self.report("保活确认超过 20 秒，继续核对计费状态")

    def confirm(self, record, cycle):
        """Handle an already observed billing state. Return True when resolved."""
        if cycle is None:
            if record["pause_sent"]:
                self.clear(self.age)
                self.report("计时卡保活完成，已确认恢复暂停计费")
                return True
            if time.time() - record["requested_at"] >= RECOVERY_SECONDS:
                self.clear(3600)
                self.report("保活未确认开启计费，稍后重新核对")
                return True
            return False

        when = timestamp(cycle)
        if when is None:
            raise APIError("麦块未返回可确认的保活计费周期，暂缓停费")
        if (record.get("cycle", cycle) != cycle
                or not -3 <= when - record["requested_at"] <= CONFIRM_SECONDS + 3):
            self.clear(3600)
            self.report("计费周期已变化，保活退出")
            return True
        if self.api.power_state(timeout=4) == "offline":
            attempts = record.get("pause_attempts", int(record["pause_sent"]))
            if attempts >= 2:
                self.report("保活停费仍未确认，需要在麦块页面核对")
                return False
            if attempts and time.time() - record.get("last_pause_attempt", 0) < RECHECK_SECONDS:
                return False
            if self.api.billing_cycle(timeout=4) != cycle:
                return False
            record.update(
                cycle=cycle, pause_sent=True, pause_attempts=attempts + 1,
                last_pause_attempt=time.time(),
            )
            self.save(record)
            self.report("计费已开启，正在立即暂停")
            self.api.pause_billing(timeout=4)
            if self.api.billing_cycle(timeout=4) is None:
                self.clear(self.age)
                self.report("计时卡保活完成，已确认恢复暂停计费")
                return True
            return False
        self.clear(3600)
        self.report("面板显示游戏服并非已停止，保活退出")
        return True
