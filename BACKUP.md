# 麦块整服冷备份

VPS 每天北京时间凌晨 2 点先在本地查备份日期。备份满七个日历日、还没有备份，或者有未完成的任务时，才访问麦块并顺手刷新实例名单。程序只接手原先暂停计费的实例，短暂恢复计费，确认游戏已停止后，调用麦块文件管理的压缩功能，将可见的整个实例根目录打包。实例自带的模组可以继续生成热备份。这项 VPS 定时任务只负责整服冷档。

程序将麦块原名 ZIP 传到 VPS，核对文件大小、ZIP 条目和内容完整性，再保存校验和。新包校验通过后替换旧包，每个实例只保留一个最新的可用 ZIP。临时下载可能额外占用一个包的空间。麦块侧只清理本次压缩任务生成的准确文件名。

当天碰到实例在计费、磁盘空间不足或上一轮下载未完成，会保留已有 VPS 备份，等下一次凌晨处理。单实例预算为 45 分钟。凌晨 4 点后不再开始新的实例。VPS 另有每 5 分钟检查的计费恢复任务，仅复核冷备份遗留的本地计费记录。现有的六天保活继续独立运行。

## 安装与启用

运行以下命令安装可选的冷备份组件。离线安装时同样可传入 `WHEEL_DIR`。

```bash
sudo bash install.sh --cold-backup
```

按 README 配置账号级自动发现、保存麦块会话，并启动或重启 `minekuai-stop.service` 以加载更新。确认准备好让 VPS 在夜间临时开启计费后，再启用定时任务：

```bash
sudo systemctl enable --now minekuai-cold-recover.timer minekuai-cold.timer
```

## 查看状态和备份

```bash
sudo systemctl list-timers --all 'minekuai-cold*.timer'
sudo journalctl -u minekuai-cold.service -u minekuai-cold-recover.service -n 80 --no-pager
```

备份在 `/var/lib/minekuai-cold/instances/<实例ID>/`。其中 `latest.json` 记录当前 ZIP 的原始文件名、字节数、SHA-256 和日期。日志出现「整服 ZIP 已校验」「麦块临时 ZIP 清理完成」及「已核对暂停计费」，表示备份与计费收尾都已完成。

日志提示上次压缩任务状态不明时，该实例会暂停冷备份。先在麦块面板确认没有压缩任务仍在运行，并核对有没有遗留的临时 ZIP。确认后把 VPS 上该实例目录的 `task.json` 移出备份目录，再等下一次定时备份或手动补档。已有的 VPS 归档可以继续使用。

统一测试或人工补档时，可在 VPS 对单个实例执行以下命令。它会临时启用该实例的计费：

```bash
sudo /opt/minekuai-stop/venv/bin/python -B /opt/minekuai-stop/cold_backup.py now INSTANCE_ID
```

## 回档

1. 先按实例 ID 读取 `latest.json`，核对 ZIP 的时间和范围，运行 `sha256sum` 与记录比对，再测试 ZIP 能完整解压。
2. 优先把 ZIP 解压到单独目录或测试实例，确认世界文件、服务端核心、模组、插件及配置。面板另行管理的启动命令、Java 镜像、网络映射等设置，需要从面板复核后再开服。
3. 在恢复正式实例前正常停服并另存其当前文件，然后把验过的 ZIP 上传，通过面板解压到实例根目录。开服后检查控制台和游戏内进度。若失败，正常停服，将另存的原文件恢复原位。

## 清理已删除实例

从麦块账号删除的实例，经过两次独立复核后开始计时，30 天后再复查清理。如果需要提前释放 VPS 空间，先确认具体实例和备份，再运行：

```bash
sudo /opt/minekuai-stop/venv/bin/python -B /opt/minekuai-stop/cold_backup.py \
  purge-retired INSTANCE_ID --confirm
```
