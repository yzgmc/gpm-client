"""客户端上报器：构造 Heartbeat 并通过 gpm_common.Reporter 上报客户端状态到 web-admin。

上报的 metrics 包含：
- server_url：当前连接的服务端地址
- installed_modpacks：本地已安装的整合包列表（仅 name/version）
- last_sync_at：上次同步时间
"""

from __future__ import annotations

import platform

from gpm_common import API_VERSION, Heartbeat, Light, LightLevel, Reporter

from app.config import ClientConfig, load_installed


_reporter: Reporter | None = None


def _build_heartbeat(config: ClientConfig) -> Heartbeat:
    installed = load_installed()
    installed_list = [
        {"id": rid, "name": rec.get("name", ""), "version": rec.get("version", "")}
        for rid, rec in installed.items()
        if rec.get("kind") == "modpacks"
    ]
    return Heartbeat(
        reporter_id=config.reporter_id,
        kind="client",
        name=config.client_name,
        base_url=None,  # 客户端不对外提供服务
        status="online",
        protocol_version=API_VERSION,
        light=Light(level=LightLevel.GREEN, reason="客户端在线"),  # 在线即绿灯
        metrics={
            "server_url": config.server_url,
            "installed_modpacks": installed_list,
            "installed_modpack_count": len(installed_list),
            "last_sync_at": config.last_sync_at,
            "platform": platform.platform(),
            "install_base_dir": config.install_base_dir,
        },
    )


def start_reporter(config: ClientConfig) -> None:
    """启动客户端上报线程。

    admin_url 为空时自动用 server_url 兜底（融合体场景：同步与上报同一地址），
    这样用户只填一个服务端地址即可同时同步整合包 + 上报心跳到仪表盘。
    """
    global _reporter
    admin_url = (config.admin_url or config.server_url or "").strip()
    if not admin_url:
        return
    if _reporter is not None:
        return
    _reporter = Reporter(
        admin_url=admin_url,
        build_payload=lambda: _build_heartbeat(config),
        interval=config.reporter_interval,
    )
    _reporter.start()


def stop_reporter() -> None:
    global _reporter
    if _reporter is not None:
        _reporter.stop()
        _reporter = None
