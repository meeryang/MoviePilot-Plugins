"""下载任务监控插件：把全部下载器的全部任务桥接进 MoviePilot 原生「下载管理」。

职责只有两件事：
1. 桥接：让原生下载管理列出 qB + Transmission 的全部任务（含外部添加、无标签、做种完成），
   并支持暂停 / 开始 / 删除 / 改标签 / 限速（自动纠正 MP 猜错的下载器）。
2. 概览：插件详情页与首页仪表板展示任务统计。
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

_QB_STATE = {
    "error": "错误", "missingfiles": "文件缺失", "uploading": "做种中", "stalledup": "做种等待",
    "forcedup": "强制做种", "queuedup": "排队做种", "pausedup": "已暂停", "stoppedup": "已停止",
    "checkingup": "校验中", "downloading": "下载中", "stalleddl": "下载等待",
    "forceddl": "强制下载", "queueddl": "排队下载", "pauseddl": "已暂停", "stoppeddl": "已停止",
    "checkingdl": "校验中", "metadl": "获取元数据", "allocating": "分配空间",
    "moving": "移动中", "checkingresume": "校验恢复", "unknown": "未知",
}

# Transmission 状态中文映射
_TR_STATE = {
    "stopped": "已暂停", "check pending": "等待校验", "checking": "校验中",
    "download pending": "等待下载", "downloading": "下载中",
    "seed pending": "等待做种", "seeding": "做种中", "isolated": "错误",
}


def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    """按属性或字典键依次取第一个非 None 值，兼容 qB / TR 两套对象。"""
    for key in keys:
        try:
            val = getattr(obj, key)
        except Exception:
            val = None
        if val is None and isinstance(obj, dict):
            val = obj.get(key)
        if val is not None:
            return val
    return default


def _to_ts(value: Any) -> Optional[int]:
    """把 epoch / datetime 统一成时间戳。"""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return int(value.timestamp())
        return int(float(value))
    except Exception:
        return None


class DownloadMonitor(_PluginBase):
    """qBittorrent / Transmission 下载任务监视器（只读）。"""

    plugin_name = "下载任务监控"
    plugin_desc = "把全部下载器的全部任务桥接进 MoviePilot 原生下载管理"
    plugin_icon = ""
    plugin_color = "#1976D2"
    plugin_version = "3.0.0"
    plugin_author = "虾虾"
    author_url = "https://github.com/meeryang/MoviePilot-Plugins"
    plugin_config_prefix = "downloadmonitor_"
    plugin_order = 26
    auth_level = 1

    _enabled: bool = False
    # 仪表板卡片刷新间隔（秒），固定值，不再对外暴露配置
    _interval: int = 15
    _downloaders: List[str] = []
    # 桥接 MP 原生「下载管理」：列出并管理全部下载器的全部任务
    _bridge_native: bool = True
    # 最近一次 init_plugin 的实例（reload 后线程/补丁仍取最新配置）
    _inst: Any = None
    # hash → 所属下载器 的短时缓存，用于纠正 MP 传错的 downloader
    _hash_owner: Dict[str, str] = {}
    _hash_owner_ts: float = 0.0
    # 仪表板短时缓存（类级，避免频繁打下载器）
    _cache: Optional[List[dict]] = None
    _cache_ts: float = 0.0

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置；本插件不使用后台线程，全部数据由面板按需拉取。"""
        self._enabled = False
        self._downloaders = []
        self._bridge_native = True
        if config:
            self._enabled = bool(config.get("enabled"))
            self._bridge_native = bool(config.get("bridge_native", True))
            raw_dl = config.get("downloaders")
            if isinstance(raw_dl, str):
                raw_dl = [x.strip() for x in raw_dl.replace("，", ",").split(",")]
            self._downloaders = [str(n) for n in (raw_dl or []) if n]
        DownloadMonitor._inst = self
        try:
            if self._enabled and self._bridge_native:
                self._install_bridge()
            else:
                self._uninstall_bridge()
        except Exception as err:  # noqa: BLE001
            logger.error(f"【{self.plugin_name}】桥接原生下载管理失败：{err}")

    # ------------------------------------------------- 原生「下载管理」桥接
    @staticmethod
    def _downloader_modules(method: str) -> List[Any]:
        """取所有实现了该方法的运行中下载器模块。

        原生 `run_module` 只采用「第一个」返回结果的模块，配了 qB + TR 也只看得到 qB，
        所以要自己枚举全部模块再合并。
        """
        try:
            from app.application.module import get_module_manager
            return list(get_module_manager().get_running_modules(method) or [])
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】枚举下载器模块失败：{err}")
            return []

    @staticmethod
    def _downloader_names() -> List[str]:
        """取全部下载器实例名（如 qbittorrent / transmisson）。

        注意：模块方法传 `downloader=None` 时走 `get_instance(None)`，多实例或未设默认实例
        会返回 None 直接不执行 —— 所以广播操作必须按实例名逐个调用。
        """
        try:
            from app.sdk.services import DownloaderHelper
            return [str(k) for k in (DownloaderHelper().get_configs() or {}).keys()]
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】读取下载器实例名失败：{err}")
            return []

    def _install_bridge(self) -> None:
        """让 MP 原生「下载管理」看到并管理全部下载器的全部任务。

        原生链路有两处限制：
        1) `list_torrents` / `start_torrents` / `stop_torrents` / `remove_torrents`
           都经 `run_module` 分发，只取第一个模块的结果 → 多下载器只剩一个。
        2) `DownloadTaskService.downloading()` 固定 `status=DOWNLOADING` 且
           `include_all_tags=False` → 只列带系统标签的下载中任务。

        这里在未指定下载器时合并全部模块、全部状态，并把暂停/开始/删除广播到所有模块；
        指定了下载器（`name`/`downloader` 非空）时完全走原生逻辑，不改变原有行为。
        """
        from app.chain.download import DownloadChain
        if getattr(DownloadChain, "_dm_bridge_installed", False):
            return
        plugin = self
        prev_downloading = DownloadChain.downloading
        prev_start = DownloadChain.start_torrents
        prev_stop = DownloadChain.stop_torrents
        prev_remove = DownloadChain.remove_torrents
        prev_tag = DownloadChain.set_torrents_tag
        prev_update = DownloadChain.update_torrent

        def _enabled() -> bool:
            inst = DownloadMonitor._inst or plugin
            return bool(getattr(inst, "_enabled", False)
                        and getattr(inst, "_bridge_native", True))

        def _annotate(chain_self: Any, rows: List[Any]) -> List[Any]:
            """补全 MP 下载历史归属（媒体/站点/用户），失败不影响列表本身。"""
            try:
                hashes = [r.hash for r in rows if getattr(r, "hash", None)]
                repo = getattr(chain_self, "download_history_repository", None)
                hmap = repo.get_by_hashes(hashes) if (repo and hashes) else {}
            except Exception:
                return rows
            if not hmap:
                return rows
            try:
                from app.schemas.transfer import DownloadTaskMedia
            except Exception:
                return rows
            for row in rows:
                hist = hmap.get(getattr(row, "hash", None))
                if not hist:
                    continue
                try:
                    row.media = DownloadTaskMedia(
                        media_source=getattr(hist, "media_source", None),
                        media_id=getattr(hist, "media_id", None),
                        type=getattr(hist, "type", None),
                        title=getattr(hist, "title", None),
                        season=getattr(hist, "seasons", None),
                        episode=getattr(hist, "episodes", None),
                        image=getattr(hist, "poster", None),
                        poster=getattr(hist, "poster", None),
                        backdrop=getattr(hist, "image", None),
                    )
                    row.site_name = getattr(hist, "torrent_site", None)
                    row.userid = getattr(hist, "userid", None)
                    row.username = getattr(hist, "username", None)
                except Exception:
                    continue
            return rows

        def _fetch(chain_self: Any, only: Optional[str]) -> List[Any]:
            """取任务列表：`only` 为空时合并全部下载器，否则只取该下载器。

            两种情况都用「全部状态 + 全部标签」，否则会漏掉做种完成与外部添加的任务。
            逐个模块调用而不是走 `chain_self.list_torrents`：`run_module` 只采用第一个
            返回非空结果的模块，指定 `downloader` 时可能被别的模块提前截断。
            """
            rows: List[Any] = []
            seen = set()
            for mod in DownloadMonitor._downloader_modules("list_torrents"):
                got = None
                try:
                    got = mod.list_torrents(status=None, hashs=None, downloader=only,
                                            include_all_tags=True)
                except TypeError:
                    try:
                        got = mod.list_torrents(status=None)
                    except Exception:
                        got = None
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"【下载任务监控】读取模块任务失败：{err}")
                    got = None
                for row in got or []:
                    key = (getattr(row, "downloader", None), getattr(row, "hash", None))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
            return rows

        def patched_downloading(chain_self: Any, name: Any = None, **kwargs: Any) -> Any:
            if not _enabled():
                return prev_downloading(chain_self, name, **kwargs)
            rows = _fetch(chain_self, name or None)
            if not rows:
                return prev_downloading(chain_self, name, **kwargs)
            return _annotate(chain_self, rows)

        def _owner_of(chain_self: Any, hash_str: Optional[str]) -> Optional[str]:
            """查某个 hash 真正属于哪个下载器。

            MP 的 `DownloadTaskMutationService` 会自己「猜」下载器，猜错时把请求发给 qB，
            而 qB 对不存在的 hash 也会返回 success —— 于是界面提示改成功、任务实际没变。
            这里建一张 hash → 下载器 的短时映射来纠正。
            """
            if not hash_str:
                return None
            now = time.time()
            cache = DownloadMonitor._hash_owner
            if (not cache) or (now - DownloadMonitor._hash_owner_ts > 120):
                mapping: Dict[str, str] = {}
                for name in DownloadMonitor._downloader_names():
                    try:
                        got = chain_self.list_torrents(hashs=None, downloader=name,
                                                       include_all_tags=True)
                    except Exception:
                        got = None
                    for row in got or []:
                        h = getattr(row, "hash", None)
                        if h:
                            mapping[str(h)] = name
                DownloadMonitor._hash_owner = mapping
                DownloadMonitor._hash_owner_ts = now
                cache = mapping
            return cache.get(str(hash_str))

        def _pick_hash(kwargs: Dict[str, Any]) -> Optional[str]:
            for key in ("hash_string", "hash_str", "hash"):
                val = kwargs.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            hashs = kwargs.get("hashs")
            if isinstance(hashs, str) and hashs.strip():
                return hashs.strip()
            if isinstance(hashs, (list, tuple)) and len(hashs) == 1:
                return str(hashs[0])
            return None

        def _redirect(chain_self: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
            """把 MP 猜错的 downloader 纠正为 hash 真正所属的下载器。"""
            given = kwargs.get("downloader")
            owner = _owner_of(chain_self, _pick_hash(kwargs))
            if owner and owner != given:
                fixed = dict(kwargs)
                fixed["downloader"] = owner
                logger.info(f"【{self.plugin_name}】纠正下载器 {given} → {owner} "
                            f"(hash={str(_pick_hash(kwargs))[:12]})")
                return fixed
            return kwargs

        def _broadcast(chain_self: Any, method: str, prev: Any,
                       args: tuple, kwargs: dict) -> Any:
            if kwargs.get("downloader") or not _enabled():
                return prev(chain_self, *args, **kwargs)
            names = DownloadMonitor._downloader_names()
            if not names:
                return prev(chain_self, *args, **kwargs)
            ok = False
            results = []
            for name in names:
                call_kwargs = dict(kwargs)
                call_kwargs["downloader"] = name
                try:
                    rv = prev(chain_self, *args, **call_kwargs)
                    results.append(f"{name}={rv}")
                    if rv:
                        ok = True
                except Exception as err:  # noqa: BLE001
                    results.append(f"{name}=E({err})")
            logger.info(f"【{self.plugin_name}】广播 {method} {args} "
                        f"{results} → {ok}")
            return ok

        def patched_start(chain_self: Any, *args: Any, **kwargs: Any) -> Any:
            return _broadcast(chain_self, "start_torrents", prev_start, args,
                              _redirect(chain_self, kwargs))

        def patched_stop(chain_self: Any, *args: Any, **kwargs: Any) -> Any:
            return _broadcast(chain_self, "stop_torrents", prev_stop, args,
                              _redirect(chain_self, kwargs))

        def patched_remove(chain_self: Any, *args: Any, **kwargs: Any) -> Any:
            return _broadcast(chain_self, "remove_torrents", prev_remove, args,
                              _redirect(chain_self, kwargs))

        def patched_set_tag(chain_self: Any, *args: Any, **kwargs: Any) -> Any:
            """标签设置同样按实例名广播，否则 qB 先返回会让 TR 任务静默失败。"""
            return _broadcast(chain_self, "set_torrents_tag", prev_tag, args,
                              _redirect(chain_self, kwargs))

        def patched_update_torrent(chain_self: Any, *args: Any, **kwargs: Any) -> Any:
            """限速/Tracker/保存目录/分类/分享率修改：按实例名广播并合并各字段结果。"""
            if not _enabled():
                return prev_update(chain_self, *args, **kwargs)
            kwargs = _redirect(chain_self, kwargs)
            if kwargs.get("downloader"):
                return prev_update(chain_self, *args, **kwargs)
            names = DownloadMonitor._downloader_names()
            if not names:
                return prev_update(chain_self, *args, **kwargs)
            merged: Dict[str, bool] = {}
            results = []
            for name in names:
                call_kwargs = dict(kwargs)
                call_kwargs["downloader"] = name
                try:
                    rv = prev_update(chain_self, *args, **call_kwargs) or {}
                except Exception as err:  # noqa: BLE001
                    results.append(f"{name}=E({err})")
                    continue
                results.append(f"{name}={rv}")
                if isinstance(rv, dict):
                    for key, val in rv.items():
                        if val:
                            merged[key] = True
                        else:
                            merged.setdefault(key, False)
            logger.info(f"【{self.plugin_name}】广播 update_torrent {results} → {merged}")
            return merged or None

        DownloadChain.downloading = patched_downloading
        DownloadChain.start_torrents = patched_start
        DownloadChain.stop_torrents = patched_stop
        DownloadChain.remove_torrents = patched_remove
        DownloadChain.set_torrents_tag = patched_set_tag
        DownloadChain.update_torrent = patched_update_torrent
        DownloadChain._dm_bridge_installed = True
        DownloadChain._dm_bridge_prev = {
            "downloading": prev_downloading,
            "start_torrents": prev_start,
            "stop_torrents": prev_stop,
            "remove_torrents": prev_remove,
            "set_torrents_tag": prev_tag,
            "update_torrent": prev_update,
        }
        logger.info(f"【{self.plugin_name}】已桥接原生下载管理：全部下载器 + 全部状态 + 标签/限速")

    def _uninstall_bridge(self) -> None:
        """还原原生方法，避免插件停用后仍被补丁接管。"""
        try:
            from app.chain.download import DownloadChain
        except Exception:
            return
        prev = getattr(DownloadChain, "_dm_bridge_prev", None)
        if not prev:
            return
        for attr, fn in prev.items():
            setattr(DownloadChain, attr, fn)
        DownloadChain._dm_bridge_installed = False
        DownloadChain._dm_bridge_prev = {}
        logger.info(f"【{self.plugin_name}】已解除原生下载管理桥接")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """插件停用/卸载时还原原生方法。"""
        self._uninstall_bridge()
        return None

    # ------------------------------------------------------------------ 配置表单
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """精简配置：只保留开关、桥接开关、下载器过滤。

        注意：这里绝不能实例化 DownloaderHelper 等运行时服务，启动期校验表单时会争锁死。
        """

        def col(md: int, *content) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": list(content)}

        def switch(model: str, label: str, hint: str = "") -> dict:
            props: Dict[str, Any] = {"model": model, "label": label,
                                     "color": "primary", "hide-details": "auto"}
            if hint:
                props["hint"], props["persistent-hint"] = hint, True
            return {"component": "VSwitch", "props": props}

        def field(model: str, label: str, placeholder: str = "", hint: str = "") -> dict:
            props: Dict[str, Any] = {"model": model, "label": label,
                                     "variant": "outlined", "density": "comfortable"}
            if placeholder:
                props["placeholder"] = placeholder
            if hint:
                props["hint"], props["persistent-hint"] = hint, True
            return {"component": "VTextField", "props": props}

        rows = [
            {"component": "VRow", "content": [
                col(4, switch("enabled", "启用插件")),
                col(4, switch("bridge_native", "桥接原生下载管理")),
                col(4, field("downloaders", "统计的下载器(逗号分隔)", "留空=全部")),
            ]},
            {"component": "VRow", "content": [
                col(12, {"component": "VAlert",
                         "props": {"type": "info", "variant": "tonal",
                                   "text": "桥接开启后，MoviePilot 自带「下载管理」会列出全部下载器"
                                           "（qB + TR）的全部任务，并支持暂停 / 开始 / 删除 / "
                                           "改标签 / 限速；关闭则恢复 MP 原生行为。"}}),
            ]},
        ]
        defaults = {"enabled": False, "bridge_native": True, "downloaders": ""}
        return [{"component": "VForm", "content": rows}], defaults

    # ------------------------------------------------------------------ 插件页
    def get_page(self) -> Optional[List[dict]]:
        """插件详情页：卡片式布局，与 LX 音乐插件保持一致。

        顶部四张统计卡 → 动作按钮行 → 下载器分布 → 说明。
        """
        rows = self._cached_rows()
        total = len(rows)
        downloading = sum(1 for r in rows if r.get("state_raw") == "downloading")
        seeding = sum(1 for r in rows if r.get("state_raw") == "completed")
        paused = sum(1 for r in rows if r.get("state_raw") == "paused")

        per_dl: Dict[str, Dict[str, int]] = {}
        for row in rows:
            name = row.get("downloader") or "未知"
            bucket = per_dl.setdefault(name, {"total": 0, "downloading": 0, "seeding": 0})
            bucket["total"] += 1
            if row.get("state_raw") == "downloading":
                bucket["downloading"] += 1
            elif row.get("state_raw") == "completed":
                bucket["seeding"] += 1

        def btn(text: str, icon: str, color: str, onclick: str,
                variant: str = "flat") -> dict:
            return {"component": "VBtn",
                    "props": {"color": color, "variant": variant, "block": True,
                              "prepend-icon": icon, "onclick": onclick},
                    "text": text}

        open_js = "function(e) { window.open('/downloading', '_blank'); }"
        rescan_js = (
            "function(e) {"
            "  window.MoviePilotAPI.post('plugin/DownloadMonitor/rescan', {})"
            "    .then(function(r) {"
            "      if (!r || r.success === false) { alert((r && r.message) || '失败'); return; }"
            "      alert(r.message || '已重建缓存');"
            "    })"
            "    .catch(function(err) { console.error(err); alert('失败，请查看日志'); });"
            "}"
        )

        content: List[dict] = [{"component": "VRow", "content": [
            self._stat_card("任务总数", str(total), "mdi-download-network", 6, 3),
            self._stat_card("下载中", str(downloading), "mdi-download", 6, 3),
            self._stat_card("做种/完成", str(seeding), "mdi-upload", 6, 3),
            self._stat_card("暂停", str(paused), "mdi-pause", 6, 3),
        ]}, {"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6},
             "content": [btn("打开原生下载管理", "mdi-monitor-dashboard", "primary", open_js)]},
            {"component": "VCol", "props": {"cols": 12, "md": 6},
             "content": [btn("重建任务归属缓存", "mdi-cached", "info", rescan_js, "tonal")]},
        ]}]

        lines = []
        for name, bucket in sorted(per_dl.items()):
            lines.append({"component": "div",
                          "props": {"class": "text-body-2 py-1"},
                          "text": (f"· {name}：共 {bucket['total']} 条"
                                   f"（下载中 {bucket['downloading']} · 做种 {bucket['seeding']}）")})
        if not lines:
            lines.append({"component": "div",
                          "props": {"class": "text-body-2 py-1 text-medium-emphasis"},
                          "text": "暂无任务数据"})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VCard", "props": {"variant": "outlined"}, "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div",
                         "props": {"class": "text-subtitle-2 font-weight-bold mb-1"},
                         "text": "下载器分布"},
                    ] + lines},
                ]},
            ]},
        ]})

        bridge_text = ("已开启：原生「下载管理」显示全部下载器、全部状态的任务，"
                       "并支持暂停 / 开始 / 删除 / 改标签 / 限速。"
                       if self._bridge_native else
                       "已关闭：恢复 MP 原生行为（只显示默认下载器的下载中任务）。")
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert",
                 "props": {"type": "info" if self._bridge_native else "warning",
                           "variant": "tonal", "title": "原生桥接状态",
                           "text": bridge_text}},
            ]},
        ]})
        return content

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/rescan", "endpoint": self.api_rescan, "methods": ["POST"],
             "summary": "重建任务归属缓存",
             "description": "清空 hash → 下载器 的映射缓存，下次操作时自动重建"},
        ]

    def api_rescan(self) -> dict:
        """清空 hash→下载器 归属缓存与任务缓存。"""
        DownloadMonitor._hash_owner = {}
        DownloadMonitor._hash_owner_ts = 0.0
        DownloadMonitor._cache = None
        DownloadMonitor._cache_ts = 0.0
        return {"success": True, "message": "缓存已清空，下次查询会自动重建"}

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """注册首页仪表板条目。"""
        if not self._enabled:
            return []
        return [{"key": "downloadmonitor", "name": "下载任务监控"}]

    def get_dashboard(
        self, key: str = "", **kwargs: Any
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Optional[List[Dict[str, Any]]]]]:
        """首页仪表板：任务概览卡片 + 正在下载的 TOP5。"""
        if not self._enabled:
            return None
        cols = {"cols": 12, "md": 6}
        attrs = {"refresh": max(int(self._interval), 15), "title": "下载任务监控"}
        rows = self._cached_rows()
        if not rows:
            return cols, attrs, [
                {"component": "VAlert",
                 "props": {"type": "info", "variant": "tonal",
                           "text": "暂无任务，或下载器未连接。点插件页的「打开监控面板」查看详情。"}}
            ]

        total = len(rows)
        downloading = sum(1 for r in rows if "下载" in str(r.get("state", "")))
        seeding = sum(1 for r in rows if "做种" in str(r.get("state", "")))
        paused = sum(1 for r in rows
                     if "暂停" in str(r.get("state", "")) or "停止" in str(r.get("state", "")))
        finished = sum(1 for r in rows if float(r.get("progress") or 0) >= 100)
        upspeed = sum(int(r.get("upspeed") or 0) for r in rows)
        dlspeed = sum(int(r.get("dlspeed") or 0) for r in rows)

        cards = [
            {"component": "VRow", "content": [
                self._stat_card("任务总数", str(total), "mdi-database", 4, 2),
                self._stat_card("下载中", str(downloading), "mdi-download", 4, 2),
                self._stat_card("做种中", str(seeding), "mdi-upload", 4, 2),
                self._stat_card("已完成", str(finished), "mdi-check-circle-outline", 4, 2),
                self._stat_card("总下载速度", self._fmt_speed(dlspeed), "mdi-speedometer", 4, 2),
                self._stat_card("总上传速度", self._fmt_speed(upspeed), "mdi-cloud-upload", 4, 2),
            ]},
        ]

        active = [r for r in rows if int(r.get("dlspeed") or 0) > 0]
        if not active:
            active = [r for r in rows if float(r.get("progress") or 0) < 100]
        active.sort(key=lambda r: (int(r.get("dlspeed") or 0), float(r.get("progress") or 0)),
                    reverse=True)
        top = active[:5]
        if top:
            lines: List[dict] = [
                {"component": "div",
                 "props": {"class": "text-subtitle-2 font-weight-bold mt-3 mb-1"},
                 "text": "正在下载 TOP5"},
            ]
            for r in top:
                name = str(r.get("name") or "")
                if len(name) > 46:
                    name = name[:46] + "…"
                lines.append({
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis d-flex"},
                    "content": [
                        {"component": "span", "props": {"class": "flex-grow-1", "style": "min-width:0"},
                         "text": name},
                        {"component": "span", "props": {"class": "ms-2"},
                         "text": f"{float(r.get('progress') or 0):.1f}%  {self._fmt_speed(r.get('dlspeed'))}"},
                    ],
                })
            cards.append({"component": "div", "content": lines})

        paused_text = f" ｜ 暂停 {paused} 个" if paused else ""
        cards.append({
            "component": "div",
            "props": {"class": "text-caption text-medium-emphasis mt-2"},
            "text": f"刷新间隔 {self._interval} 秒 ｜ 数据来自 {len({r.get('downloader') for r in rows})} 个下载器"
                    + paused_text,
        })
        return cols, attrs, cards

    def _cached_rows(self) -> List[dict]:
        """仪表板渲染期内复用短时缓存，避免每次刷新都打一遍下载器。"""
        cls = DownloadMonitor
        now = time.time()
        if cls._cache is not None and (now - cls._cache_ts) < max(5, self._interval):
            return cls._cache
        try:
            rows, _errs = self._collect()
        except Exception as err:  # noqa: BLE001
            logger.error(f"下载任务监控：仪表板采集失败 {err}")
            return []
        cls._cache, cls._cache_ts = rows, now
        return rows

    @staticmethod
    def _stat_card(title: str, value: str, icon: str, cols: int, md: int) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [
                {"component": "VCard", "props": {"variant": "tonal"},
                 "content": [
                     {"component": "VCardText", "props": {"class": "d-flex align-center"},
                      "content": [
                          {"component": "VIcon",
                           "props": {"icon": icon, "size": 30, "class": "me-3", "color": "primary"}},
                          {"component": "div", "content": [
                              {"component": "div",
                               "props": {"class": "text-caption text-medium-emphasis"},
                               "text": title},
                              {"component": "div",
                               "props": {"class": "text-h6 font-weight-bold"},
                               "text": value},
                          ]},
                      ]},
                 ]},
            ],
        }

    @staticmethod
    def _fmt_speed(value: Any) -> str:
        try:
            n = float(value or 0)
        except Exception:
            n = 0.0
        for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
            if n < 1024 or unit == "GB/s":
                return (f"{n:.0f} {unit}" if unit == "B/s" else f"{n:.2f} {unit}")
            n /= 1024.0
        return f"{n:.2f} GB/s"

    # ------------------------------------------------------------------ 采集
    def _collect(self) -> Tuple[List[dict], List[str]]:
        errors: List[str] = []
        rows: List[dict] = []
        try:
            services = DownloaderHelper().get_services(name_filters=self._downloaders or None) or {}
        except Exception as err:  # noqa: BLE001
            logger.error(f"下载任务监控：获取下载器服务失败 {err}")
            return [], [f"读取下载器失败：{err}"]

        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if instance is None:
                errors.append(f"{name}：未就绪")
                continue
            is_tr = getattr(instance, "trc", None) is not None
            try:
                result = instance.get_torrents()
            except Exception as err:  # noqa: BLE001
                errors.append(f"{name}：{err}")
                logger.error(f"下载任务监控：读取 {name} 任务失败 {err}")
                continue
            if isinstance(result, tuple):
                torrents, error = (result + (False,))[:2]
            else:
                torrents, error = result, False
            if error:
                errors.append(f"{name}：查询失败")
                continue
            seq = 0
            for torrent in torrents or []:
                seq += 1
                try:
                    rows.append(self._norm(torrent, name, is_tr, seq))
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"下载任务监控：任务解析跳过 {err}")
        return rows, errors

    def _norm(self, torrent: Any, downloader: str, is_tr: bool, seq: int) -> dict:
        """把 qB / TR 的任务对象归一化成统一字典。"""
        progress_raw = _g(torrent, "progress", "percent_done", "percentDone", default=0) or 0
        try:
            progress = float(progress_raw)
        except Exception:
            progress = 0.0
        if progress <= 1.0:
            progress *= 100.0

        size = _g(torrent, "size", "size_when_done", "sizeWhenDone", "total_size", default=0) or 0
        total_size = _g(torrent, "total_size", "totalSize", "size_when_done", default=size) or size

        state_raw = _g(torrent, "state", "status", default="") or ""
        state_text = str(state_raw)
        table = _TR_STATE if is_tr else _QB_STATE
        state_cn = table.get(state_text.lower(), state_text)

        eta_raw = _g(torrent, "eta", default=None)
        try:
            eta = int(eta_raw) if eta_raw is not None else None
        except Exception:
            eta = None

        ratio = _g(torrent, "ratio", "upload_ratio", "uploadRatio", default=0)
        try:
            ratio = float(ratio)
        except Exception:
            ratio = 0.0

        popularity = _g(torrent, "popularity", default=None)
        try:
            popularity = float(popularity) if popularity is not None else None
        except Exception:
            popularity = None

        labels = _g(torrent, "tags", "labels", default="") or ""
        if isinstance(labels, (list, tuple)):
            labels = ", ".join(str(x) for x in labels if x)

        added = _to_ts(_g(torrent, "added_on", "added_date", "addedDate", default=None))

        upspeed = _g(torrent, "upspeed", "up_speed", "rate_upload", "rateUpload", default=0) or 0
        dlspeed = _g(torrent, "dlspeed", "dl_speed", "rate_download", "rateDownload", default=0) or 0
        uploaded = _g(torrent, "uploaded", "uploaded_ever", "uploadedEver", default=0) or 0
        downloaded = _g(torrent, "downloaded", "downloaded_ever", "downloadedEver", default=0) or 0

        category = _g(torrent, "category", default="") or ""
        if not category and is_tr and labels:
            category = str(labels).split(",")[0].strip()

        return {
            "seq": seq,
            "downloader": downloader,
            "hash": str(_g(torrent, "hash", "hash_string", "hashString", default="") or ""),
            "name": str(_g(torrent, "name", default="") or ""),
            "progress": round(progress, 1),
            "size": int(size),
            "total_size": int(total_size),
            "save_path": str(_g(torrent, "save_path", "download_dir", "downloadDir", default="") or ""),
            "added": added or 0,
            "added_text": (datetime.fromtimestamp(added).strftime("%Y-%m-%d %H:%M")
                           if added else ""),
            "state": state_cn,
            "state_raw": state_text,
            "eta": eta,
            "eta_text": self._fmt_eta(eta),
            "ratio": round(ratio, 2),
            "popularity": (round(popularity, 3) if popularity is not None else None),
            "category": str(category),
            "tags": str(labels),
            "upspeed": int(upspeed),
            "dlspeed": int(dlspeed),
            "uploaded": int(uploaded),
            "downloaded": int(downloaded),
        }

    @staticmethod
    def _fmt_eta(eta: Optional[int]) -> str:
        if eta is None or eta < 0 or eta >= 8640000:
            return "—"
        if eta == 0:
            return "0秒"
        days, rem = divmod(int(eta), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}小时")
        if minutes and not days:
            parts.append(f"{minutes}分")
        if not parts:
            parts.append(f"{seconds}秒")
        return "".join(parts)

    # ------------------------------------------------------------------ 面板 HTML
