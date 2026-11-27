# 麦块空服自动关停

[![CI](https://github.com/shortFisherman/minekuai-idle-stop/actions/workflows/ci.yml/badge.svg)](https://github.com/shortFisherman/minekuai-idle-stop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

麦块联机的 Minecraft Java 版计时卡实例连续 10 分钟无人时，自动正常关服并暂停计费。程序长期运行在 Ubuntu VPS 上，平时仍然从麦块网页开计费、开服，退出游戏后可以直接关电脑。

默认以单实例模式运行，也可配置账号级自动发现。默认每 30 秒通过 Minecraft 服务器列表查询读取在线人数。有人在线或人数未知时清空倒计时。达到阈值后再次核对人数和计费周期，确认正常关服完成才暂停该实例的计费，并重新读取计费状态。

## 环境

- 有 systemd 的 Linux 主机。已在 Ubuntu 24.04 和 Python 3.12 上完成实服验收。
- 主机能访问你的游戏服端口，以及麦块 API 和实例控制台的 HTTPS / WebSocket 服务。
- 麦块账号名下有一台计时卡实例。游戏服需允许 Java 版服务器列表状态查询。

## 安装

### 1. 安装程序

```bash
sudo apt install git python3 python3-venv
git clone https://github.com/shortFisherman/minekuai-idle-stop.git
cd minekuai-idle-stop
sudo bash install.sh
```

如果 VPS 能连接麦块和游戏节点，但访问不了 GitHub 或 PyPI，可以在能联网的电脑上下载源码和纯 Python wheel，再上传给 VPS。用下面的参数让安装脚本从本地 wheel 目录读取依赖。

```bash
python -m pip download --only-binary=:all: --platform any -r requirements.txt -d wheelhouse
sudo env WHEEL_DIR="$PWD/wheelhouse" bash install.sh
```

### 2. 配置实例

运行 `sudoedit /etc/minekuai-stop/config.json`。从麦块控制台复制**同一台**服务器的实例 ID 和游戏连接地址。示例地址和 ID 均为占位值。配置文件格式见 [config.example.json](config.example.json)。

| 字段 | 填写方式 |
| --- | --- |
| `instance` | 实例控制台网址 `/server/<实例 ID>` 末尾的 8 位 ID |
| `host` | 该实例的 Minecraft 连接主机名或 IP，端口填到下一项 |
| `port` | 该实例的 Minecraft 连接端口 |
| `idle_seconds` | 连续无人秒数，默认为 600 |
| `poll_seconds` | 查询间隔，默认为 30。允许 5 至 120 秒，且不能超过无人阈值 |

### 3. 绑定麦块会话

在自己的浏览器登录麦块，打开开发者工具的 **应用 → Cookie → https://minekuai.com**，复制 `Admin-Token` 的值。在 VPS 的 SSH 终端运行下列命令，并在隐藏输入提示出现后粘贴。程序会先用该会话读取目标实例的计费状态，验证成功后才保存到仅 root 可读的凭据文件。

```bash
sudo /opt/minekuai-stop/venv/bin/python /opt/minekuai-stop/authorize.py
```

`Admin-Token` 是账号会话凭据。只把它交给你信任的主机和终端，避免粘贴到命令参数、日志、截图或 GitHub。

### 4. 检查并启动

```bash
sudo /opt/minekuai-stop/venv/bin/python /opt/minekuai-stop/monitor.py --check
sudo systemctl enable --now minekuai-stop.service
sudo journalctl -u minekuai-stop.service -f
```

`--check` 是只读检查。实例暂停计费、游戏服还在启动或状态查询失败时，在线人数会显示为 `null`。希望先观察倒计时，可以在启用服务前手动运行 `monitor.py --dry-run`，确认日志后按 Ctrl+C 退出，再启用服务。

### 按次数判断空服

在实例配置中设置 `"poll_seconds": 3600` 和 `"idle_samples": 2`，即可每小时查询一次，同一计费周期连续两次查到 0 人后发起关停。启用 `idle_samples` 时按查询次数判断，允许查询间隔最长 3600 秒。有人在线、人数未知或计费周期改变都会清空空服记录。关服流程发起后会短时复查停服和停费结果。如果收到限流响应，程序会按服务端给出的等待时间延后查询。

### 可选的计时卡保活

实例配置中设置 `"keepalive_days": 6` 可以开启保活。保活从该实例的麦块计费记录读取上次结束计费的时间。达到设置的天数后，短暂恢复该实例的计费。程序确认计费已启用、游戏服仍已停止后，会立即暂停计费并复查。麦块的计费取整及数据保留规则以其控制台为准。

### 账号级自动发现

将实例配置中的 `instance`、`host`、`port` 替换为 `"scope": "account"`，即可通过当前会话自动发现账号下的计时卡实例和主联机地址。所有实例共享每轮计费查询，每台分别判断空服并记录关停状态。自动空服关停仍要求游戏服返回有效的 Minecraft Java 在线人数。

账号级模式下，可在启用 `keepalive_days` 时设置 `keepalive_instances`，只允许指定 ID 的实例保活。省略该列表时，六天保活覆盖账号内发现的全部实例，包括以后新增的实例。

## Minecraft 整服冷备份

可在同一 Linux VPS 上运行整服冷备份任务。它在北京时间凌晨 2 点检查账号，约每周备份每个计时卡实例一次。麦块在停服状态下打包实例根目录，VPS 校验收到的 ZIP，每个实例保留最新一份。
使用 `sudo bash install.sh --cold-backup` 安装这项可选功能。启用、日志、回档与清理方式见[整服冷备份说明](BACKUP.md)。

## 日常运维

- **临时保留空服**。运行 `sudo systemctl stop minekuai-stop.service`，玩完后用 `sudo systemctl start minekuai-stop.service` 恢复。重新启动服务会重新计空闲时间。
- **会话失效**。重新获取当前浏览器会话，运行 `authorize.py`，成功后执行 `sudo systemctl try-restart minekuai-stop.service`。
- **日志显示停费或关服未确认**。到麦块页面检查对应实例。程序会继续读取状态，同一次结果不明的写操作不会自动重复发送。
- **升级**。更新仓库后运行 `sudo bash install.sh`，再重启服务。安装脚本保留已有配置和会话。

计时从首次成功读到 0 人开始，下一次轮询还需要复核，因此真实触发时间可能比阈值多出约一个查询间隔。关服完成和停费确认另需等待麦块面板。若服务已手动停掉，且游戏端口无法返回人数，当前版本会将其视为状态未知，需要手动检查是否仍在计费。麦块的实例冻结及数据保留规则以其控制台为准。

## 开发

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

- `monitor.py` 管理倒计时、关停顺序和确认状态。
- `minekuai.py` 实现 Java 版服务器状态查询和麦块接口。
- `authorize.py` 提供会话验证及更新。
- `install.sh` 与 `minekuai-stop.service` 用于部署到 systemd。

本项目使用 MIT 许可证。麦块联机接口和控制台协议参考了 [minekuai-qqbot](https://github.com/LookO-666/minekuai-qqbot)，服务的运行取决于麦块维持相应接口。
