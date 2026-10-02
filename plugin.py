"""麦麦插件安装审批器（maibot-plugin-installer）。

职责：把「造好的插件」经**人（管理员）批准**后自动安装并启用。

链路：
  1. 任何来源（如 dsh 桥）通过跨插件 API `submit` 交来一个待安装插件目录；
  2. 本插件在**发起任务的聊天流**里发一条审批请求；
  3. 管理员回复 `/pi_approve <id>` → 自动复制到 plugins/<id>、启用、重载；
     回复 `/pi_deny <id>` → 撤销待审（插件目录原样留在大夫手里，不删）。

安全：
  - 只有 permission.allowed_users 里的管理员能审批；
  - 安装目标必须在 plugins 根目录内；
  - 拒绝不删源目录，可回滚。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase

PLUGINS_ROOT = Path(__file__).resolve().parent.parent  # .../plugins


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class InstallConfig(PluginConfigBase):
    __ui_label__ = "安装"
    __ui_icon__ = "download"
    __ui_order__ = 1

    plugins_root: str = Field(default="", description="plugins 根目录；留空自动推断")
    auto_reload: bool = Field(default=True, description="安装后自动重载该插件")
    api_endpoint: str = Field(default="submit", description="对外暴露的提交接口名")


class PermissionConfig(PluginConfigBase):
    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否启用管理员白名单")
    allowed_users: str = Field(default="", description="有权审批安装的管理员 ID，逗号分隔；留空且启用则拒绝所有人")


class InstallerConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    install: InstallConfig = Field(default_factory=InstallConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)


class PluginInstaller(MaiBotPlugin):
    config_model = InstallerConfig

    async def on_load(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        try:
            await self._register_api()
        except Exception:  # noqa: BLE001
            self.ctx.logger.warning("注册提交接口失败", exc_info=True)
        self.ctx.logger.info(
            "插件安装审批器已加载 (plugins_root=%s, endpoint=%s)",
            self._plugins_root(),
            self.config.install.api_endpoint,
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("插件安装审批器已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version

    # ── 内部辅助 ──────────────────────────────────────────────

    def _plugins_root(self) -> Path:
        root = self.config.install.plugins_root.strip()
        if root:
            return Path(root)
        return PLUGINS_ROOT

    async def _register_api(self) -> None:
        ok = await self.sync_dynamic_apis()
        self.ctx.logger.info("提交接口同步%s", "成功" if ok else "失败")

    @API(
        "submit",
        description="提交一个待安装的 MaiBot 插件目录，向发起流请求管理员审批安装。",
        version="1",
        public=True,
    )
    async def handle_submit(self, **kwargs: Any) -> dict[str, Any]:
        plugin_dir = str(kwargs.get("plugin_dir") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        return await self._submit(plugin_dir, stream_id)

    @staticmethod
    def _extract_user_id(kwargs: dict[str, Any]) -> str:
        user_id = (
            kwargs.get("sender_id")
            or kwargs.get("user_id")
            or kwargs.get("operator")
            or ""
        )
        if isinstance(user_id, dict):
            user_id = user_id.get("user_id") or user_id.get("id") or ""
        return str(user_id).strip()

    def _is_admin(self, kwargs: dict[str, Any]) -> bool:
        perm = self.config.permission
        if not perm.enabled:
            return True
        user_id = self._extract_user_id(kwargs)
        allowed = {u.strip() for u in perm.allowed_users.split(",") if u.strip()}
        return bool(user_id and user_id in allowed)

    def _arg(self, kwargs: dict[str, Any], name: str, pattern: str) -> str:
        matched = kwargs.get("matched_groups")
        if isinstance(matched, dict):
            val = matched.get(name)
            if val:
                return str(val).strip()
        raw = str(kwargs.get("text") or "")
        m = re.match(pattern, raw, re.DOTALL)
        if m:
            return m.group(1).strip()
        return ""

    # ── 提交（内部 + API 共用） ────────────────────────────────

    async def _submit(self, plugin_dir: str, stream_id: str) -> dict[str, Any]:
        p = Path(plugin_dir)
        manifest = p / "_manifest.json"
        if not p.is_dir() or not manifest.is_file():
            return {"success": False, "error": "不是有效的插件目录（缺 _manifest.json）"}
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"manifest 解析失败：{exc}"}
        pid = str(meta.get("id") or p.name).strip()
        name = str(meta.get("name") or pid).strip()
        if not pid:
            return {"success": False, "error": "manifest 缺少 id"}

        self._pending[pid] = {"dir": str(p), "stream_id": stream_id, "name": name}
        self.ctx.logger.info("收到待审插件：%s (%s) from %s", pid, name, p)
        if stream_id:
            try:
                await self.ctx.send.text(
                    f"📦 发现待安装插件：{name}（{pid}）\n"
                    f"管理员回复 /pi_approve {pid} 安装，/pi_deny {pid} 忽略。",
                    stream_id,
                )
            except Exception:  # noqa: BLE001
                self.ctx.logger.warning("审批请求发送失败", exc_info=True)
        return {"success": True, "plugin_id": pid, "name": name, "pending": True}

    # ── 安装 ──────────────────────────────────────────────────

    async def _install(self, pid: str, info: dict[str, Any]) -> tuple[bool, str]:
        src = Path(info["dir"])
        if not src.is_dir():
            return False, f"源目录不存在：{src}"
        dst = (self._plugins_root() / pid).resolve()
        try:
            dst.relative_to(self._plugins_root().resolve())
        except ValueError:
            return False, f"安全拒绝：目标越出 plugins 目录（{dst}）"
        if dst.exists():
            return False, f"目标已存在：{dst}（请先手动处理）"

        try:
            shutil.copytree(src, dst)
        except Exception as exc:  # noqa: BLE001
            return False, f"复制失败：{exc}"

        # 启用：把 config.toml 的 enabled = false 改成 true
        cfg = dst / "config.toml"
        if cfg.is_file():
            try:
                text = cfg.read_text(encoding="utf-8")
                text = re.sub(r"(?m)^(\s*enabled\s*=\s*)false", r"\1true", text)
                cfg.write_text(text, encoding="utf-8")
            except Exception:  # noqa: BLE001
                self.ctx.logger.warning("启用 config 改写失败", exc_info=True)

        self._pending.pop(pid, None)
        # 注意：把插件写进 plugins/ 本身就会触发宿主扫描并加载新插件；
        # 此处不再显式 reload_plugin，避免与宿主重载撞车（后者会中断本命令）。
        return True, f"✅ 已安装并启用插件 {pid}（{info.get('name', pid)}），宿主将自动加载"

    # ── 命令：审批 ────────────────────────────────────────────

    @Command("pi_approve", description="批准安装待审插件", pattern=r"^/pi_approve\s+(?P<plugin_id>[^\s]+)$")
    async def handle_approve(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件未启用", False
        if not self._is_admin(kwargs):
            await self.ctx.send.text("你没有权限审批安装", stream_id)
            return False, "无权限", False
        pid = self._arg(kwargs, "plugin_id", r"^/pi_approve\s+([^\s]+)$")
        info = self._pending.get(pid)
        if not info:
            await self.ctx.send.text(f"没有待审插件：{pid}", stream_id)
            return False, "未找到待审", False
        ok, msg = await self._install(pid, info)
        await self.ctx.send.text(msg, stream_id)
        return ok, msg, False

    @Command("pi_deny", description="忽略待审插件", pattern=r"^/pi_deny\s+(?P<plugin_id>[^\s]+)$")
    async def handle_deny(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件未启用", False
        if not self._is_admin(kwargs):
            await self.ctx.send.text("你没有权限审批安装", stream_id)
            return False, "无权限", False
        pid = self._arg(kwargs, "plugin_id", r"^/pi_deny\s+([^\s]+)$")
        removed = self._pending.pop(pid, None)
        if removed:
            await self.ctx.send.text(f"已忽略待审插件 {pid}（源目录保留）", stream_id)
            return True, "已忽略", False
        await self.ctx.send.text(f"没有待审插件：{pid}", stream_id)
        return False, "未找到待审", False

    @Command("pi_pending", description="列出现有待审插件", pattern=r"^/pi_pending$")
    async def handle_pending(self, stream_id: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return False, "插件未启用", False
        if not self._is_admin(kwargs):
            await self.ctx.send.text("你没有权限查看待审列表", stream_id)
            return False, "无权限", False
        if not self._pending:
            await self.ctx.send.text("当前没有待审插件", stream_id)
            return True, "空", False
        lines = [f"- {pid}：{info.get('name', pid)}（{info.get('dir', '')}）" for pid, info in self._pending.items()]
        await self.ctx.send.text("待审插件：\n" + "\n".join(lines), stream_id)
        return True, f"{len(self._pending)} 个待审", False


def create_plugin() -> PluginInstaller:
    return PluginInstaller()
