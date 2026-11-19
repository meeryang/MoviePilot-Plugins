"""下载任务监控插件：把全部下载器的全部任务桥接进 MoviePilot 原生「下载管理」。

职责只有两件事：
1. 桥接：让原生下载管理列出 qB + Transmission 的全部任务（含外部添加、无标签、做种完成），
   并支持暂停 / 开始 / 删除 / 改标签 / 限速（自动纠正 MP 猜错的下载器）。
2. 概览：插件详情页与首页仪表板展示任务统计。
"""

import json
import hashlib
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
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
    plugin_version = "3.3.0"
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
    # 封面：配置开关 / 标题→海报URL缓存 / 待查队列 / 线程状态
    _poster_on: bool = True
    _poster_cache: Dict[str, str] = {}
    _poster_pending: set = set()
    _poster_lock = threading.Lock()
    _poster_dirty: int = 0
    _poster_started: bool = False
    # 原生状态统计缓存
    _raw_stats_cache: Dict[str, Dict[str, int]] = {}
    _raw_stats_ts: float = 0.0
    _raw_merged: Dict[str, int] = {}
    _poster_file: Optional[Path] = None
    # 仪表板短时缓存（类级，避免频繁打下载器）
    _cache: Optional[List[dict]] = None
    _cache_ts: float = 0.0

    # ---------- 前端「下载管理筛选栏」补丁 ----------
    # 补丁标记：出现即认为已打过
    _FE_MARK = "_dmS={bar:"
    # 补丁写入位置：用户过滤锚点之后（setup 作用域，可用 n=ref / l=computed / f=任务列表）
    _FE_HELPER = (
        'const _dmS={bar:{display:"flex",gap:"8px",alignItems:"center",flexWrap:"wrap",'
        'margin:"0 0 12px",padding:"8px 10px",border:"1px solid rgba(128,128,128,.28)",'
        'borderRadius:"10px"},inp:{flex:"1 1 240px",minWidth:"160px",height:"34px",'
        'padding:"0 10px",borderRadius:"8px",border:"1px solid rgba(128,128,128,.35)",'
        'background:"transparent",color:"inherit",outline:"none",fontSize:"13px"},'
        'chip:{height:"30px",padding:"0 11px",borderRadius:"15px",'
        'border:"1px solid rgba(128,128,128,.35)",background:"transparent",color:"inherit",'
        'cursor:"pointer",fontSize:"12.5px",lineHeight:"1",whiteSpace:"nowrap"},'
        'chipOn:{height:"30px",padding:"0 11px",borderRadius:"15px",cursor:"pointer",'
        'fontSize:"12.5px",lineHeight:"1",whiteSpace:"nowrap",fontWeight:"600",'
        'color:"inherit",border:"1px solid rgba(var(--v-theme-primary,25,118,210),.65)",'
        'background:"rgba(var(--v-theme-primary,25,118,210),.16)"},'
        'cnt:{marginLeft:"auto",fontSize:"12px",opacity:".7",whiteSpace:"nowrap"},'
        'emp:{padding:"8px 2px",fontSize:"13px",opacity:".7"}}'
        ',_dmKind=t=>{const s=t.state||"",p=parseFloat(t.progress||"0")||0,'
        'u=parseFloat(t.upspeed||"0")||0,dl=parseFloat(t.dlspeed||"0")||0;'
        'return{all:!0,downloading:"downloading"===s,seeding:p>=100&&"paused"!==s,'
        'uploading:u>0,active:dl>0||u>0,paused:"paused"===s}}'
        ',_dmKw=n(""),_dmSt=n("all"),_dmLow=l((()=>_dmKw.value.trim().toLowerCase())),'
        '_dmCnt=l((()=>{const a={all:f.value.length,downloading:0,seeding:0,uploading:0,'
        'active:0,paused:0};for(const t of f.value){const k=_dmKind(t);'
        'k.downloading&&a.downloading++;k.seeding&&a.seeding++;k.uploading&&a.uploading++;'
        'k.active&&a.active++;k.paused&&a.paused++}return a})),'
        '_dmFlt=l((()=>{const a=_dmLow.value,e=_dmSt.value;return f.value.filter((t=>{'
        'if("all"!==e&&!_dmKind(t)[e])return!1;if(!a)return!0;'
        'return[t.title,t.name,t.site_name,t.hash,t.downloader,t.tags]'
        '.filter(Boolean).join(" ").toLowerCase().includes(a)}))}));'
    )
    # 渲染作用域里补一个创建节点的简写
    _FE_ALIAS = ',_dmB=(...z)=>(d(),t(...z))'
    # 筛选栏 vnode（插在 return d(),t(s,null,[ 之后）
    _FE_BAR = (
        '_dmB("div",{style:_dmS.bar},['
        '_dmB("input",{style:_dmS.inp,'
        'placeholder:"筛选任务：标题 / 番剧名 / 站点 / hash",value:_dmKw.value,'
        'onInput:(a=>{_dmKw.value=a.target.value})},null,8,["value"]),'
    ) + ''.join(
        '_dmB("button",{style:("%s"===_dmSt.value?_dmS.chipOn:_dmS.chip),'
        'onClick:(a=>{_dmSt.value="%s"})},"%s "+_dmCnt.value.%s,5),' % (k, k, label, k)
        for k, label in (("all", "全部"), ("downloading", "下载中"), ("seeding", "做种"),
                         ("uploading", "上传中"), ("active", "活动"), ("paused", "已暂停"))
    ) + (
        '_dmB("span",{style:_dmS.cnt},'
        '"已筛选 "+_dmFlt.value.length+" / 共 "+f.value.length+" 条",1)],0),'
        '(_(v)&&0===_dmFlt.value.length&&f.value.length>0)'
        '?_dmB("div",{style:_dmS.emp},"没有匹配的任务，试试换个关键词或状态",1):m("",!0),'
    )
    # 锚点（上游结构未变时才能自动打）
    _FE_ANCHOR_USER = 'filter((a=>a.userid===e||a.username===e))}))'
    _FE_ANCHOR_RENDER = '(a,o)=>{const n=K;return d(),t(s,null,['
    _FE_DIR = '/config/plugins/DownloadMonitor/fe'

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置；本插件不使用后台线程，全部数据由面板按需拉取。"""
        self._enabled = False
        self._downloaders = []
        self._bridge_native = True
        self._poster_on = True
        if config:
            self._enabled = bool(config.get("enabled"))
            self._bridge_native = bool(config.get("bridge_native", True))
            self._poster_on = bool(config.get("poster_fill", True))
            raw_dl = config.get("downloaders")
            if isinstance(raw_dl, str):
                raw_dl = [x.strip() for x in raw_dl.replace("，", ",").split(",")]
            self._downloaders = [str(n) for n in (raw_dl or []) if n]
        DownloadMonitor._inst = self
        if self._enabled:
            threading.Thread(target=self._fe_autopatch, daemon=True).start()
        if self._enabled:
            try:
                self._poster_start()
            except Exception as err:  # noqa: BLE001
                logger.debug(f"【下载任务监控】启动封面线程失败：{err}")
        try:
            if self._enabled and self._bridge_native:
                self._install_bridge()
            else:
                self._uninstall_bridge()
        except Exception as err:  # noqa: BLE001
            logger.error(f"【{self.plugin_name}】桥接原生下载管理失败：{err}")

    # ------------------------------------------------- 下载任务封面（TMDB）
    # 字幕组/压制组、语言、格式等噪声词（命中即丢弃该分组）
    _POSTER_NOISE = (
        "nekomoe", "kissaten", "lolihouse", "kitaujisub", "anti-dei", "baha", "dynamis",
        "orion origin", "mteam", "mteamhd", "jpsc", "webrip", "web-dl", "webdl", "bluray",
        "blu-ray", "remux", "hevc", "avc", "aac", "flac", "dts", "truehd", "atmos", "10bit",
        "8bit", "hdr", "dolby", "vision", "1080p", "2160p", "720p", "480p", "x264", "x265",
        "cht", "chs", "简繁", "中字", "内封", "字幕", "国语", "粤语", "双语", "complete",
        "fhd", "uhd", "hd", "sd", "xvid", "divx",
        "repack", "proper", "internal", "multi",
    )

    @classmethod
    def _poster_tokens(cls, title: Any) -> List[Tuple[str, bool]]:
        """切成候选片段：(文本, 是否括号外)。括号外的通常是片名。"""
        t = unicodedata.normalize("NFKC", str(title or ""))
        t = re.sub(r"\.(mkv|mp4|avi|ts|m2ts|rmvb|iso|wmv|flv)$", " ", t, flags=re.I)
        out: List[Tuple[str, bool]] = []
        for grp in re.findall(r"[\[\{（(【]([^\]\}）)】]*)[\]\}）)】]", t):
            out.append((grp, False))
        outside = re.sub(r"[\[\{（(【][^\]\}）)】]*[\]\}）)】]", " ", t).strip()
        if outside:
            out.insert(0, (outside, True))
        return out

    @classmethod
    def _poster_clean(cls, text: str) -> str:
        """去掉噪声词、年份、集数、音轨/分辨率、发行组尾巴。"""
        t = unicodedata.normalize("NFKC", str(text or ""))
        t = re.sub(
            r"\b(2160p|1080p|1080i|720p|480p|x264|x265|h ?264|h ?265|hevc|avc|remux|bluray|"
            r"blu-?ray|web-?dl|webdl|webrip|hdtv|dts|dts-?hd|aac|ac3|eac3|ddp?|flac|truehd|"
            r"10bit|8bit|hdr|dolby|vision|atmos|proper|repack|internal|complete|multi|chs|cht|"
            r"ma|lpcm|opus|mkv|mp4)\b", " ", t, flags=re.I)
        t = re.sub(r"(?i)\bS\d{1,2}(E\d{1,3})?\b", " ", t)          # S05 / S05E03
        t = re.sub(r"(?i)\bE\d{1,3}\b", " ", t)
        t = re.sub(r"\b\d\.\d\b", " ", t)                     # 5.1 / 7.1
        t = re.split(r"\b(?:19|20)\d{2}\b", t)[0]               # 年份及之后
        t = re.sub(r"(?i)(?:\s*[-–—]\s*|\bep\.?\s*|第\s*)(\d{1,4})(\s*(?:v\d|集|话|話))?\s*$",
                   " ", t)                                        # 结尾集数
        t = re.sub(r"\s*[-–—]\s*[A-Za-z0-9&]+$", " ", t)         # 结尾发行组
        t = re.sub(r"[._\-]+", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip(" -!·,")
        return t[:80]

    @classmethod
    def _poster_key(cls, title: Any) -> str:
        """主检索键：优先括号外文本，其次最长的有效括号组。"""
        best: Tuple[int, str] = (0, "")
        for text, outside in cls._poster_tokens(title):
            cleaned = cls._poster_clean(text)
            if not cleaned or re.fullmatch(r"[\d\s.]+", cleaned):
                continue
            low = cleaned.lower()
            if any(n in low for n in cls._POSTER_NOISE):
                continue
            score = (1000 if outside else 0) + len(cleaned)
            if score > best[0]:
                best = (score, cleaned)
        return best[1] or cls._poster_clean(str(title or ""))

    @staticmethod
    def _poster_similar(a: str, b: str) -> float:
        """粗略相似度，用于降低 TMDB 错配。"""
        import difflib
        na = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(a or "").lower())
        nb = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(b or "").lower())
        if not na or not nb:
            return 0.0
        if na in nb or nb in na:
            return 1.0
        return difflib.SequenceMatcher(None, na, nb).ratio()

    @classmethod
    def _poster_lookup(cls, title: str) -> str:
        """多候选查 TMDB 海报；结果少时信任 TMDB 排序，多时用相似度筛。"""
        key = str(title or "").strip()
        if not key:
            return ""
        # 纯 hash / 纯数字结尾的任务名没有检索价值
        if re.fullmatch(r"[0-9a-fA-F]{16,}", key) or len(key) < 2:
            return ""
        candidates = [key]
        short = re.sub(r"\s+\d{1,4}$", "", key).strip()
        if short and short != key:
            candidates.append(short)
        words = short.split()
        if len(words) > 4:
            candidates.append(" ".join(words[:4]))
        try:
            from app.modules.themoviedb.tmdbapi import TmdbApi
            api = TmdbApi()
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】TMDB 不可用：{err}")
            return ""
        for cand in candidates:
            try:
                recs = api.search_multiis(cand) or []
            except Exception as err:  # noqa: BLE001
                logger.debug(f"【下载任务监控】TMDB 检索失败 {cand!r}：{err}")
                continue
            top = [r for r in recs if isinstance(r, dict) and r.get("poster_path")][:4]
            if not top:
                continue
            best_score, best_rec = 0.0, top[0]
            for rec in top:
                names = [rec.get("title"), rec.get("name"),
                         rec.get("original_title"), rec.get("original_name")]
                score = max((cls._poster_similar(cand, n) for n in names if n), default=0.0)
                if score > best_score:
                    best_score, best_rec = score, rec
            # 中日文片名与罗马音/英文标题相似度天然为 0，此时信任 TMDB 的相关度排序
            chosen = best_rec if best_score >= 0.45 else top[0]
            path = chosen.get("poster_path")
            if path:
                return f"https://image.tmdb.org/t/p/w500{path}"
        return ""

    def _poster_load(self) -> None:
        """从插件数据目录读取缓存。"""
        try:
            path = Path(self.get_data_path()) / "posters.json"
            DownloadMonitor._poster_file = path
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    DownloadMonitor._poster_cache = {str(k): str(v) for k, v in data.items()}
                    logger.info(f"【{self.plugin_name}】封面缓存载入 {len(data)} 条")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】读取封面缓存失败：{err}")

    def _poster_save(self) -> None:
        """把缓存落盘（失败不影响功能）。"""
        path = DownloadMonitor._poster_file
        if not path:
            return
        try:
            with DownloadMonitor._poster_lock:
                data = dict(DownloadMonitor._poster_cache)
                DownloadMonitor._poster_dirty = 0
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】写封面缓存失败：{err}")

    def _poster_worker(self) -> None:
        """后台限速补全封面，避免拖慢列表请求。"""
        while True:
            try:
                inst = DownloadMonitor._inst
                if not inst or not getattr(inst, "_enabled", False) \
                        or not getattr(inst, "_poster_on", False):
                    time.sleep(3.0)
                    continue
                with DownloadMonitor._poster_lock:
                    key = DownloadMonitor._poster_pending.pop() if DownloadMonitor._poster_pending else None
                if not key:
                    time.sleep(2.0)
                    continue
                if key in DownloadMonitor._poster_cache:
                    continue
                url = self._poster_lookup(key)
                with DownloadMonitor._poster_lock:
                    DownloadMonitor._poster_cache[key] = url
                    DownloadMonitor._poster_dirty += 1
                if DownloadMonitor._poster_dirty >= 20:
                    self._poster_save()
                time.sleep(0.5)
            except Exception as err:  # noqa: BLE001
                logger.debug(f"【下载任务监控】封面线程异常：{err}")
                time.sleep(2.0)

    def _poster_start(self) -> None:
        """启动封面后台线程（幂等）。"""
        if DownloadMonitor._poster_started or not DownloadMonitor._poster_on:
            return
        if not DownloadMonitor._poster_file:
            self._poster_load()
        DownloadMonitor._poster_started = True
        threading.Thread(target=self._poster_worker, name="dm-poster", daemon=True).start()
        logger.info(f"【{self.plugin_name}】封面补全线程已启动")

    def _apply_posters(self, rows: List[Any]) -> List[Any]:
        """把已知封面写进 task.media.poster，未知标题丢给后台线程。"""
        if not DownloadMonitor._poster_on or not rows:
            return rows
        try:
            from app.schemas.transfer import DownloadTaskMedia
        except Exception:
            return rows
        cache = DownloadMonitor._poster_cache
        queued = 0
        for row in rows:
            media = getattr(row, "media", None)
            if media is not None and getattr(media, "poster", None):
                continue
            key = self._poster_key(getattr(row, "title", None) or getattr(row, "name", None))
            if not key:
                continue
            url = cache.get(key)
            if url is None:
                with DownloadMonitor._poster_lock:
                    if key not in DownloadMonitor._poster_pending:
                        DownloadMonitor._poster_pending.add(key)
                        queued += 1
                continue
            if not url:
                continue
            try:
                if media is not None:
                    media.poster = url
                else:
                    row.media = DownloadTaskMedia(poster=url)
            except Exception:
                try:
                    row.media = DownloadTaskMedia(poster=url)
                except Exception:
                    continue
        if queued:
            logger.debug(f"【下载任务监控】新增 {queued} 个待查封面标题")
        return rows

    def _poster_stats(self) -> Dict[str, int]:
        cache = DownloadMonitor._poster_cache
        return {"cached": len(cache), "hit": sum(1 for v in cache.values() if v),
                "miss": sum(1 for v in cache.values() if not v),
                "pending": len(DownloadMonitor._poster_pending)}

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
            return (DownloadMonitor._inst or plugin)._apply_posters(_annotate(chain_self, rows))

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
                col(4, switch("poster_fill", "自动补封面（TMDB）")),
            ]},
            {"component": "VRow", "content": [
                col(12, {"component": "VAlert",
                         "props": {"type": "info", "variant": "tonal",
                                   "text": "桥接开启后，MoviePilot 自带「下载管理」会列出全部下载器"
                                           "（qB + TR）的全部任务，并支持暂停 / 开始 / 删除 / "
                                           "改标签 / 限速；关闭则恢复 MP 原生行为。"}}),
            ]},
        ]
        defaults = {"enabled": False, "bridge_native": True, "downloaders": "", "poster_fill": True}
        return [{"component": "VForm", "content": rows}], defaults

    # ------------------------------------------------------------------ 插件页
    # ------------------------------------------------- 原生状态统计（7 分类）
    # 顺序即页面展示顺序：(键, 中文, 图标)
    _STAT_ITEMS = (
        ("downloading", "下载中", "mdi-download"),
        ("uploading", "上传中", "mdi-arrow-up-bold"),
        ("seeding", "做种中", "mdi-upload"),
        ("completed", "完成", "mdi-check-circle-outline"),
        ("paused", "暂停", "mdi-pause"),
        ("stopped", "停止", "mdi-stop"),
        ("error", "错误", "mdi-alert-circle-outline"),
    )
    # 下载器原生状态 → 分类（MP 会把状态归一成 downloading/completed/paused，
    # 想把「做种 / 停止 / 错误」分出来就必须直接读下载器原始状态）
    _RAW_BUCKET = {
        # qBittorrent
        "downloading": "downloading", "forceddl": "downloading", "stalleddl": "downloading",
        "queueddl": "downloading", "checkingdl": "downloading", "metadl": "downloading",
        "allocating": "downloading",
        "uploading": "seeding", "forcedup": "seeding", "stalledup": "seeding",
        "queuedup": "seeding", "checkingup": "seeding",
        "pauseddl": "paused", "pausedup": "paused",
        "stoppeddl": "stopped", "stoppedup": "stopped",
        "error": "error", "missingfiles": "error",
        # Transmission
        "downloading": "downloading", "download pending": "downloading",
        "seeding": "seeding", "seed pending": "seeding",
        "stopped": "paused",  # Transmission 只有 stopped，语义即暂停
        "isolated": "error",
    }

    # Transmission 的 status 可能是数字（RPC 原始值）或字符串
    _TR_STATUS = {0: "stopped", 1: "check pending", 2: "checking", 3: "download pending",
                  4: "downloading", 5: "seed pending", 6: "seeding"}

    @classmethod
    def _raw_fields(cls, item: Any) -> Tuple[str, float, float, str]:
        """从原始任务对象里取 (状态文本, 进度%, 上传速度B/s, hash)。

        qBittorrent 用 dict（state/progress/upspeed），Transmission 用对象或 dict
        （status/percentDone 或 percent_done/rateUpload 或 rate_upload），两种都要兼容。
        """
        raw: Any = ""
        prog: Any = 0
        ups: Any = 0
        try:
            if hasattr(item, "get"):
                raw = item.get("state")
                if raw in (None, ""):
                    raw = item.get("status")
                prog = item.get("progress")
                if prog is None:
                    prog = item.get("percentDone", item.get("percent_done"))
                ups = item.get("upspeed")
                if ups is None:
                    ups = item.get("rateUpload", item.get("rate_upload"))
            else:
                raw = getattr(item, "status", None)
                if raw in (None, ""):
                    raw = getattr(item, "state", "")
                prog = getattr(item, "percent_done", None)
                if prog is None:
                    prog = getattr(item, "percentDone", None)
                if prog is None:
                    prog = getattr(item, "progress", 0)
                ups = getattr(item, "rate_upload", None)
                if ups is None:
                    ups = getattr(item, "rateUpload", 0)
        except Exception:  # noqa: BLE001
            return "", 0.0, 0.0, ""
        if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
            raw = cls._TR_STATUS.get(int(raw), "")
        try:
            prog_f = float(prog or 0)
        except Exception:  # noqa: BLE001
            prog_f = 0.0
        if prog_f <= 1.0:  # 0-1 比例 → 百分比
            prog_f *= 100
        try:
            ups_f = float(ups or 0)
        except Exception:  # noqa: BLE001
            ups_f = 0.0
        hash_val = ""
        try:
            if hasattr(item, "get"):
                hash_val = str(item.get("hash") or item.get("hashString") or "")
            else:
                hash_val = str(getattr(item, "hash_string", "") or getattr(item, "hash", "") or "")
        except Exception:  # noqa: BLE001
            hash_val = ""
        return str(raw or "").strip().lower(), prog_f, ups_f, hash_val.strip().lower()

    @classmethod
    def _bucket_of(cls, raw: str, prog: float) -> str:
        hit = cls._RAW_BUCKET.get(raw)
        if hit:
            return hit
        return "completed" if prog >= 99.9 else "downloading"

    @staticmethod
    def _empty_stat() -> Dict[str, int]:
        stat = {key: 0 for key, _, _ in DownloadMonitor._STAT_ITEMS}
        stat["total"] = 0
        stat["other"] = 0
        return stat

    def _raw_stats(self) -> Dict[str, Dict[str, int]]:
        """按下载器实例统计原生状态；带 10 秒缓存，避免拖慢页面。"""
        now = time.time()
        if DownloadMonitor._raw_stats_cache and now - DownloadMonitor._raw_stats_ts < 10:
            return DownloadMonitor._raw_stats_cache
        result: Dict[str, Dict[str, int]] = {}
        try:
            from app.application.module import get_module_manager
            mods = list(get_module_manager().get_running_modules("list_torrents") or [])
        except Exception as err:  # noqa: BLE001
            logger.debug(f"【下载任务监控】枚举下载器模块失败：{err}")
            return result
        merged = self._empty_stat()
        seen_hash: set = set()
        seen_up: set = set()
        for name in DownloadMonitor._downloader_names():
            for mod in mods:
                try:
                    inst = mod.get_instance(name)
                except Exception:  # noqa: BLE001
                    inst = None
                if not inst:
                    continue
                items = None
                try:
                    got = inst.get_torrents()
                    items = got[0] if isinstance(got, tuple) else got
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"【下载任务监控】读取 {name} 原始任务失败：{err}")
                if items is None:
                    continue
                stat = self._empty_stat()
                for item in items:
                    raw, prog, ups, hash_val = self._raw_fields(item)
                    bucket = self._bucket_of(raw, prog)
                    stat["total"] += 1
                    if bucket in stat:
                        stat[bucket] += 1
                    else:
                        stat["other"] += 1
                    if ups > 0:  # 上传中＝当前真有上传流量（做种中的活跃子集）
                        stat["uploading"] += 1
                    # 同一个种子可能同时挂在两个下载器上，跨下载器按 hash 去重
                    if hash_val and hash_val not in seen_hash:
                        seen_hash.add(hash_val)
                        merged["total"] += 1
                        if bucket in merged:
                            merged[bucket] += 1
                        else:
                            merged["other"] += 1
                    if ups > 0 and (not hash_val or hash_val not in seen_up):
                        if hash_val:
                            seen_up.add(hash_val)
                        merged["uploading"] += 1
                result[name] = stat
                break
        DownloadMonitor._raw_stats_cache = result
        DownloadMonitor._raw_merged = merged
        DownloadMonitor._raw_stats_ts = now
        return result

    def _stat_tile(self, label: str, value: int, icon: str, md: int = 3) -> dict:
        """下载器卡片里的一个小指标块。"""
        return {"component": "VCol", "props": {"cols": 6, "md": md}, "content": [
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
             "text": label},
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"},
             "text": str(value)},
        ]}

    # ------------------------------------------------- 插件页
    def get_page(self) -> Optional[List[dict]]:
        """插件页：全局统计（原生状态 7 分类）+ 每个下载器单独一张卡。"""
        stats = self._raw_stats()

        def btn(text: str, icon: str, color: str, onclick: str, variant: str = "flat") -> dict:
            return {"component": "VBtn",
                    "props": {"color": color, "variant": variant, "block": True,
                              "prepend-icon": icon, "onclick": onclick},
                    "text": text}

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
        poster_js = (
            "function(e) {"
            "  window.MoviePilotAPI.post('plugin/DownloadMonitor/fill_posters', {})"
            "    .then(function(r) {"
            "      if (!r || r.success === false) { alert((r && r.message) || '失败'); return; }"
            "      alert(r.message || '已入队');"
            "    })"
            "    .catch(function(err) { console.error(err); alert('失败，请查看日志'); });"
            "}"
        )

        fe_js = (
            "function(e) {"
            "  window.MoviePilotAPI.post('plugin/DownloadMonitor/fe_fix', {})"
            "    .then(function(r) {"
            "      if (!r || r.success === false) { alert((r && r.message) || '失败'); return; }"
            "      alert(r.message || '已恢复');"
            "      if (window.location && window.location.reload) { window.location.reload(); }"
            "    })"
            "    .catch(function(err) { console.error(err); alert('失败，请查看日志'); });"
            "}"
        )
        fe_status = DownloadMonitor._fe_status()
        fe_ok = bool(fe_status.get('patched'))
        fe_text = (
            f"筛选栏已生效（{fe_status.get('chunk')} · 版本 {fe_status.get('version') or '—'}）"
            if fe_ok else
            "筛选栏未生效（MoviePilot 升级后前端补丁会丢失），点右侧按钮一键恢复"
        )
        content: List[dict] = []
        if not stats:
            content.append({"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert",
                     "props": {"type": "warning", "variant": "tonal",
                               "title": "读不到下载器",
                               "text": "没有可用的下载器实例，请先在「设置-下载器」里配置并启用。"}}]}]})
            return content

        # ---- 全局合计 ----
        total = dict(DownloadMonitor._raw_merged) or self._empty_stat()
        if not total.get("total"):
            total = self._empty_stat()
            for stat in stats.values():
                for key in total:
                    total[key] += stat.get(key, 0)

        def card(stat: Dict[str, int], tile_md: int = 3, per_row: int = 4) -> List[dict]:
            """把 7 项指标排成若干行；tile_md 控制每个指标占的宽度。"""
            tiles = [self._stat_tile(label, stat.get(key, 0), icon, tile_md)
                     for key, label, icon in self._STAT_ITEMS]
            return [{"component": "VRow", "content": tiles[i:i + per_row]}
                    for i in range(0, len(tiles), per_row)]

        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VCard", "props": {"variant": "tonal", "color": "primary"},
                 "content": [{"component": "VCardText", "content": [
                     {"component": "div",
                      "props": {"class": "text-subtitle-2 font-weight-bold mb-2"},
                      "text": f"全部下载器（去重共 {total['total']} 个任务）"}] + card(total)}]}]}]})

        # ---- 动作按钮（已移除「打开原生下载管理」）----
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 6},
             "content": [btn("重建任务归属缓存", "mdi-cached", "info", rescan_js, "tonal")]},
            {"component": "VCol", "props": {"cols": 12, "md": 6},
             "content": [btn("补全任务封面", "mdi-image-search", "primary", poster_js, "tonal")]},
        ]})
        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                {"component": "VAlert",
                 "props": {"type": "success" if fe_ok else "warning", "variant": "tonal",
                           "density": "compact", "title": "下载管理筛选栏",
                           "text": fe_text}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                btn("恢复下载管理筛选栏", "mdi-restore", "warning", fe_js, "tonal")]},
        ]})

        # ---- 每个下载器一张卡 ----
        dl_cols: List[dict] = []
        for name in sorted(stats):
            stat = stats[name]
            dl_cols.append({
                "component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VCard", "props": {"variant": "outlined", "height": "100%"},
                     "content": [{"component": "VCardText", "content": [
                         {"component": "div",
                          "props": {"class": "text-subtitle-2 font-weight-bold mb-2"},
                          "text": f"{name}（共 {stat['total']} 个任务）"},
                     ] + card(stat, tile_md=6, per_row=2)}]}]})
        # 固定两个框一行，从左到右排列，数量不足时只占一半宽度
        for i in range(0, len(dl_cols), 2):
            content.append({"component": "VRow", "content": dl_cols[i:i + 2]})

        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert",
                 "props": {"type": "info", "variant": "tonal", "title": "统计说明",
                           "text": "下载中 / 做种中 / 暂停 / 停止 / 错误均取下载器原生状态；"
                                   "上传中＝当前确有上传流量（做种中的活跃子集，与做种中重叠）；"
                                   "完成＝已下完但当前未做种；全部合计按 hash 去重"
                                   "（部分种子同时挂在两个下载器上）。"}}]}]})
        return content


    # ================= 前端筛选栏补丁（升级后一键恢复） =================
    @classmethod
    def _fe_chunk(cls) -> Optional[Path]:
        """找到当前的下载管理 chunk（升级后文件名会变）。"""
        assets = Path('/public/assets')
        if not assets.exists():
            return None
        files = list(assets.glob('downloading-*.js'))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    @classmethod
    def _fe_status(cls) -> Dict[str, Any]:
        """检查筛选栏补丁与缓存穿透版本是否到位。"""
        chunk = cls._fe_chunk()
        out: Dict[str, Any] = {'chunk': None, 'patched': False, 'busted': False,
                               'version': None, 'writable': False}
        if not chunk:
            out['message'] = '找不到 /public/assets/downloading-*.js'
            return out
        out['chunk'] = chunk.name
        try:
            txt = chunk.read_text(encoding='utf-8', errors='ignore')
            out['patched'] = cls._FE_MARK in txt
            out['writable'] = os.access(chunk, os.W_OK)
            idx = Path('/public/index.html')
            html = idx.read_text(encoding='utf-8', errors='ignore')
            m = re.search(r'main-[\w-]+\.js\?v=(\w+)', html)
            out['version'] = m.group(1) if m else None
            out['busted'] = bool(m)
        except Exception as err:  # noqa: BLE001
            out['message'] = f'读取失败: {err}'
        return out

    @classmethod
    def _fe_bust(cls, version: str) -> List[str]:
        """给改过的前端文件加 ?v= 版本号，绕过 Cloudflare 30 天静态缓存。"""
        touched: List[str] = []
        public = Path('/public')
        bak = Path(cls._FE_DIR)
        bak.mkdir(parents=True, exist_ok=True)

        def backup(p: Path) -> None:
            dst = bak / (p.name + '.orig')
            if not dst.exists():
                dst.write_text(p.read_text(encoding='utf-8', errors='ignore'), encoding='utf-8')

        idx = public / 'index.html'
        html = idx.read_text(encoding='utf-8', errors='ignore')
        m_main = re.search(r'\./assets/(main-[\w-]+\.js)', html)
        m_boot = re.search(r'\./assets/(mf-entry-bootstrap-[\w-]+\.js)', html)
        if not m_main or not m_boot:
            raise RuntimeError('index.html 里找不到 main / bootstrap 引用')
        main_name, boot_name = m_main.group(1), m_boot.group(1)

        # index.html：preload 与 script 引用都带上版本
        if html.replace('./assets/' + main_name, './assets/' + main_name + '?v=' + version) != html:
            backup(idx)
            new_html = re.sub(r'(\./assets/' + re.escape(main_name) + r')(\?v=\w+)?',
                              r'\1?v=' + version, html)
            idx.write_text(new_html, encoding='utf-8')
            touched.append('index.html')

        # bootstrap：它负责加载 main
        boot = public / 'assets' / boot_name
        if boot.exists():
            bt = boot.read_text(encoding='utf-8', errors='ignore')
            nb = re.sub(r'(\./' + re.escape(main_name) + r')(\?v=\w+)?', r'\1?v=' + version, bt)
            if nb != bt:
                backup(boot)
                boot.write_text(nb, encoding='utf-8')
                touched.append(boot.name)

        # main：它引用下载管理 chunk
        main = public / 'assets' / main_name
        if main.exists():
            mt = main.read_text(encoding='utf-8', errors='ignore')
            chunk = cls._fe_chunk()
            if chunk:
                nm = re.sub(r'(' + re.escape(chunk.name) + r')(\?v=\w+)?',
                            r'\1?v=' + version, mt)
                if nm != mt:
                    backup(main)
                    main.write_text(nm, encoding='utf-8')
                    touched.append(main_name)
        return touched

    @classmethod
    def _fe_apply(cls, force: bool = False) -> Dict[str, Any]:
        """把筛选栏补丁打到当前 chunk 上，并做缓存穿透。幂等，可反复调用。"""
        chunk = cls._fe_chunk()
        if not chunk:
            return {'success': False, 'message': '找不到 /public/assets/downloading-*.js'}
        try:
            txt = chunk.read_text(encoding='utf-8', errors='ignore')
        except Exception as err:  # noqa: BLE001
            return {'success': False, 'message': f'读取 chunk 失败: {err}'}

        if cls._FE_MARK in txt and not force:
            try:
                st = cls._fe_status()
                ver = st.get('version')
                if not ver:
                    ver = 'dmfe' + hashlib.sha1(txt.encode()).hexdigest()[:8]
                    cls._fe_bust(ver)
                return {'success': True, 'already': True, 'chunk': chunk.name,
                        'version': ver, 'message': '筛选栏已存在，无需恢复'}
            except Exception as err:  # noqa: BLE001
                return {'success': True, 'already': True, 'chunk': chunk.name,
                        'message': f'筛选栏已存在（缓存穿透未完成: {err}）'}

        # 结构校验：上游改版就不能盲打
        missing = []
        if cls._FE_ANCHOR_USER not in txt:
            missing.append('用户过滤锚点')
        if cls._FE_ANCHOR_RENDER not in txt:
            missing.append('列表渲染锚点')
        if txt.count('_(f)') < 2:
            missing.append('任务列表渲染引用')
        if missing:
            return {'success': False, 'chunk': chunk.name,
                    'message': 'MoviePilot 前端结构已变化，自动补丁需要人工适配：缺少 ' + '、'.join(missing)}

        bak = Path(cls._FE_DIR)
        bak.mkdir(parents=True, exist_ok=True)
        (bak / (chunk.name + '.orig')).write_text(txt, encoding='utf-8')

        # 1) setup 作用域插入筛选栏状态与过滤逻辑
        new = txt.replace(cls._FE_ANCHOR_USER,
                          cls._FE_ANCHOR_USER + cls._FE_HELPER, 1)
        # 2) 渲染作用域插入节点简写 + 筛选栏 vnode
        new = new.replace(cls._FE_ANCHOR_RENDER,
                          '(a,o)=>{const n=K' + cls._FE_ALIAS + ';return d(),t(s,null,['
                          + cls._FE_BAR, 1)
        # 3) 只把前两处 _(f) 换成过滤后的列表（后面几处 f 是别的含义）
        new = new.replace('_(f)', '_(_dmFlt)', 2)
        if new == txt or cls._FE_MARK not in new:
            return {'success': False, 'message': '补丁未生效（替换失败）'}

        chunk.write_text(new, encoding='utf-8')
        version = 'dmfe' + hashlib.sha1(new.encode()).hexdigest()[:8]
        touched: List[str] = []
        try:
            touched = cls._fe_bust(version)
        except Exception as err:  # noqa: BLE001
            logger.warning(f'[DownloadMonitor] 缓存穿透失败: {err}')

        # 回读校验
        back = chunk.read_text(encoding='utf-8', errors='ignore')
        ok = cls._FE_MARK in back and back.count('_dmFlt') >= 5
        return {'success': ok, 'chunk': chunk.name, 'version': version,
                'size_before': len(txt), 'size_after': len(back),
                'busted': touched,
                'message': ('筛选栏已恢复：' + chunk.name + '，请刷新页面') if ok else '写入后校验失败'}

    def api_fe_fix(self) -> dict:
        """恢复前端下载管理筛选栏（升级后一键重打）。"""
        return DownloadMonitor._fe_apply(force=False)

    def api_fe_status(self) -> dict:
        """查询筛选栏补丁状态。"""
        st = DownloadMonitor._fe_status()
        st['success'] = True
        return st


    @classmethod
    def _fe_autopatch(cls) -> None:
        """启动后自检：筛选栏丢失就自动重打（升级后无需手动点）。"""
        try:
            time.sleep(6)
            st = cls._fe_status()
            if st.get('patched') and st.get('version'):
                logger.info(f"[DownloadMonitor] 筛选栏补丁在位（{st.get('chunk')} v={st.get('version')}）")
                return
            res = cls._fe_apply()
            logger.info(f"[DownloadMonitor] 筛选栏自动恢复: {res.get('message')}")
        except Exception as err:  # noqa: BLE001
            logger.warning(f'[DownloadMonitor] 筛选栏自动恢复失败: {err}')

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/rescan", "endpoint": self.api_rescan, "methods": ["POST"],
             "summary": "重建任务归属缓存",
             "description": "清空 hash → 下载器 的映射缓存，下次操作时自动重建"},
            {"path": "/posters", "endpoint": self.api_posters, "methods": ["GET"],
             "summary": "封面缓存统计"},
            {"path": "/fill_posters", "endpoint": self.api_fill_posters, "methods": ["POST"],
             "summary": "补全全部任务封面",
             "description": "把当前全部任务的标题入队，由后台线程限速查询 TMDB 海报"},
            {"path": "/fe_fix", "endpoint": self.api_fe_fix, "methods": ["POST"],
             "summary": "恢复下载管理筛选栏",
             "description": "MP 升级后前端补丁会丢，调用即重新打补丁并刷新缓存版本号"},
            {"path": "/fe_status", "endpoint": self.api_fe_status, "methods": ["GET"],
             "summary": "筛选栏补丁状态"},
        ]

    def api_posters(self) -> dict:
        """封面缓存统计。"""
        stats = self._poster_stats()
        stats.update({"success": True, "enabled": bool(DownloadMonitor._poster_on),
                      "running": bool(DownloadMonitor._poster_started)})
        return stats

    def api_fill_posters(self) -> dict:
        """把当前全部任务标题入队，交给后台线程补封面。"""
        rows = self._cached_rows()
        added = 0
        with DownloadMonitor._poster_lock:
            for row in rows:
                key = self._poster_key(row.get("title") or row.get("name"))
                if key and key not in DownloadMonitor._poster_cache:
                    if key not in DownloadMonitor._poster_pending:
                        DownloadMonitor._poster_pending.add(key)
                        added += 1
        self._poster_start()
        return {"success": True, "queued": added, "pending": len(DownloadMonitor._poster_pending),
                "message": f"已入队 {added} 个待查标题，后台会限速补全"}

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
