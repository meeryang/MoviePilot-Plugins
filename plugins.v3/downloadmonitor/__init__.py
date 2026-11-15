"""下载任务监控插件：实时查看 qBittorrent / Transmission 全部任务的明细。

只读插件：不改动任何下载器状态。数据由插件自带的面板页面按秒轮询 /tasks 端点获取，
面板本体是一个自包含 HTML 页面（原始 Response 返回），不依赖前端组件映射。
"""

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Response

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper

# 排序键 -> 中文名（面板下拉框用）
SORT_LABELS: Dict[str, str] = {
    "seq": "序号",
    "name": "名称",
    "downloader": "下载器",
    "progress": "进度",
    "size": "选定大小",
    "added": "添加时间",
    "state": "状态",
    "eta": "剩余时间",
    "ratio": "比率",
    "popularity": "流行度",
    "category": "分类",
    "tags": "标签",
    "upspeed": "上传速度",
    "dlspeed": "下载速度",
    "uploaded": "累计上传",
    "downloaded": "累计下载",
}

# 字符串型字段：排序按字典序，其余按数值
_TEXT_KEYS = {"name", "downloader", "state", "category", "tags"}

# qBittorrent 状态中文映射
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
    plugin_desc = "实时监控 qBittorrent / Transmission 全部任务，支持多字段排序"
    plugin_icon = ""
    plugin_color = "#1976D2"
    plugin_version = "1.0.1"
    plugin_author = "虾虾"
    author_url = "https://github.com/meeryang/MoviePilot-Plugins"
    plugin_config_prefix = "downloadmonitor_"
    plugin_order = 26
    auth_level = 1

    _enabled: bool = False
    _interval: int = 5
    _downloaders: List[str] = []
    _default_sort: str = "added"
    _default_order: str = "desc"

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置；本插件不使用后台线程，全部数据由面板按需拉取。"""
        self._enabled = False
        self._interval = 5
        self._downloaders = []
        self._default_sort = "added"
        self._default_order = "desc"
        if config:
            self._enabled = bool(config.get("enabled"))
            try:
                self._interval = max(1, min(120, int(config.get("interval") or 5)))
            except Exception:
                self._interval = 5
            raw_dl = config.get("downloaders")
            if isinstance(raw_dl, str):
                raw_dl = [x.strip() for x in raw_dl.replace("，", ",").split(",")]
            self._downloaders = [str(n) for n in (raw_dl or []) if n]
            if config.get("default_sort") in SORT_LABELS:
                self._default_sort = config["default_sort"]
            if config.get("default_order") in ("asc", "desc"):
                self._default_order = config["default_order"]

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """无后台线程需要停止。"""
        return None

    # ------------------------------------------------------------------ 配置表单
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        # 注意：这里绝不能实例化 DownloaderHelper 等运行时服务。
        # 启动期若在校验插件表单时构造服务对象，会与主流程争锁导致启动挂死（实测卡在 futex_do_wait）。
        # 下载器名称改为纯文本配置，init_plugin 里再解析。

        def col(md: int, *content) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": list(content)}

        def switch(model: str, label: str) -> dict:
            return {"component": "VSwitch",
                    "props": {"model": model, "label": label, "color": "primary", "hide-details": "auto"}}

        def field(model: str, label: str, placeholder: Any = None, ftype: str = "text",
                  hint: str = "") -> dict:
            props: Dict[str, Any] = {"model": model, "label": label,
                                     "variant": "outlined", "density": "comfortable"}
            if placeholder is not None:
                props["placeholder"] = str(placeholder)
            if ftype != "text":
                props["type"] = ftype
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {"component": "VTextField", "props": props}

        def select(model: str, label: str, items: List[str]) -> dict:
            return {"component": "VSelect",
                    "props": {"model": model, "label": label, "variant": "outlined",
                              "density": "comfortable", "items": items, "multiple": True,
                              "chips": True, "clearable": True,
                              "hint": "留空表示监控全部下载器",
                              "persistent-hint": True}}

        rows = [
            {"component": "VRow", "content": [
                col(4, switch("enabled", "启用插件")),
                col(4, field("interval", "面板刷新间隔(秒)", 5, "number", "1~120 秒，默认 5")),
                col(4, field("default_sort", "默认排序字段", "added", "text",
                             "可选：" + "/".join(SORT_LABELS.keys()))),
            ]},
            {"component": "VRow", "content": [
                col(4, field("downloaders", "监控的下载器(逗号分隔)", "留空=全部", "text",
                             "填下载器名称，多个用英文逗号分隔")),
                col(4, field("default_order", "默认排序方向", "desc", "text", "asc 升序 / desc 降序")),
                col(4, field("rows_per_page", "每页条数", 100, "number", "面板分页大小")),
            ]},
            {"component": "VRow", "content": [
                col(12, {"component": "VAlert",
                         "props": {"type": "info", "variant": "tonal",
                                   "text": "插件只读，不会修改或暂停任何任务。"
                                           "在插件详情页点「打开监控面板」即可实时查看全部任务。"}}),
            ]},
        ]
        defaults = {
            "enabled": False,
            "interval": 5,
            "downloaders": [],
            "default_sort": "added",
            "default_order": "desc",
            "rows_per_page": 100,
        }
        return [{"component": "VForm", "content": rows}], defaults

    # ------------------------------------------------------------------ 插件页
    def get_page(self) -> Optional[List[dict]]:
        if not self._enabled:
            return [{"component": "VAlert",
                     "props": {"type": "warning", "variant": "tonal",
                               "text": "插件未启用，请先在配置中启用。"}}]
        try:
            from app.core.config import settings
            apikey = settings.API_TOKEN
        except Exception:  # noqa: BLE001
            apikey = ""
        url = f"api/v1/plugin/DownloadMonitor/ui?apikey={apikey}"
        onclick = (
            "function(e) { window.open('" + url + "', '_blank'); }"
        )
        return [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VBtn",
                     "props": {"color": "primary", "variant": "flat", "block": True,
                               "prepend-icon": "mdi-monitor-dashboard", "onclick": onclick},
                     "text": "打开监控面板"},
                ]},
                {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                    {"component": "VAlert",
                     "props": {"type": "info", "variant": "tonal", "density": "compact",
                               "text": "面板会在新标签页打开，按配置的间隔自动刷新。"}},
                ]},
            ]},
        ]

    # ------------------------------------------------------------------ API
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/tasks", "endpoint": self.api_tasks, "methods": ["GET"],
             "summary": "获取全部下载任务"},
            {"path": "/ui", "endpoint": self.api_ui, "methods": ["GET"],
             "summary": "监控面板页面"},
        ]

    def api_tasks(self, sort: str = "", order: str = "", keyword: str = "",
                  downloader: str = "") -> dict:
        """返回任务列表（信封结构，供面板消费）。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": {"items": [], "total": 0}}
        try:
            rows, errors = self._collect()
        except Exception as err:  # noqa: BLE001
            logger.error(f"下载任务监控：采集失败 {err}")
            return {"success": False, "message": str(err), "data": {"items": [], "total": 0}}

        key = sort if sort in SORT_LABELS else self._default_sort
        reverse = (order or self._default_order) == "desc"

        if downloader:
            rows = [r for r in rows if r["downloader"] == downloader]
        if keyword:
            kw = keyword.lower()
            rows = [r for r in rows
                    if kw in str(r.get("name", "")).lower()
                    or kw in str(r.get("save_path", "")).lower()
                    or kw in str(r.get("tags", "")).lower()
                    or kw in str(r.get("category", "")).lower()]

        if key != "seq":
            if key in _TEXT_KEYS:
                rows.sort(key=lambda r: str(r.get(key) or ""), reverse=reverse)
            else:
                # 空值（如 Transmission 没有流行度、eta 未知）统一排到最后，不随升降序翻到最前
                nums = [r for r in rows if isinstance(r.get(key), (int, float))]
                nulls = [r for r in rows if not isinstance(r.get(key), (int, float))]
                nums.sort(key=lambda r: r.get(key), reverse=reverse)
                rows = nums + nulls
        if not reverse and key == "seq":
            rows.sort(key=lambda r: (r.get("downloader", ""), r.get("seq", 0)))

        for idx, row in enumerate(rows, 1):
            row["no"] = idx

        return {"success": True, "message": "",
                "data": {"items": rows, "total": len(rows),
                         "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "errors": errors,
                         "sort": key, "order": "desc" if reverse else "asc",
                         "interval": self._interval,
                         "columns": SORT_LABELS}}

    def api_ui(self, apikey: str = "") -> Response:
        """返回自包含的监控面板 HTML。"""
        html = self._build_html()
        return Response(content=html, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

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
    def _build_html(self) -> str:
        """自包含面板页；占位符用 replace 注入，避免 f-string 与 CSS 花括号冲突。"""
        return (_HTML_TEMPLATE
                .replace("__INTERVAL__", str(self._interval * 1000))
                .replace("__DEFAULT_SORT__", self._default_sort)
                .replace("__DEFAULT_ORDER__", self._default_order)
                .replace("__SORT_LABELS__", json.dumps(SORT_LABELS, ensure_ascii=False)))


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>下载任务监控</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 16px; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #121212; color: #e0e0e0; font-size: 13px; }
  h1 { font-size: 16px; margin: 0 0 12px; font-weight: 600; }
  .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
  input, select, button { background: #1e1e1e; color: #e0e0e0; border: 1px solid #333;
         border-radius: 6px; padding: 6px 10px; font-size: 13px; outline: none; }
  button { cursor: pointer; }
  button:hover { border-color: #1976D2; }
  .stat { color: #9e9e9e; }
  .stat b { color: #e0e0e0; }
  .wrap { overflow: auto; border: 1px solid #2a2a2a; border-radius: 8px; max-height: calc(100vh - 150px); }
  table { border-collapse: collapse; width: 100%; white-space: nowrap; }
  thead th { position: sticky; top: 0; background: #1c1c1c; z-index: 2; text-align: left;
         padding: 8px 10px; border-bottom: 1px solid #333; cursor: pointer; user-select: none; font-weight: 600; }
  thead th:hover { color: #64b5f6; }
  thead th .arrow { color: #64b5f6; margin-left: 3px; }
  tbody td { padding: 6px 10px; border-bottom: 1px solid #222; }
  tbody tr:hover { background: #1a1a1a; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .name { max-width: 380px; overflow: hidden; text-overflow: ellipsis; }
  .path { max-width: 240px; overflow: hidden; text-overflow: ellipsis; color: #9e9e9e; }
  .prog { display: flex; align-items: center; gap: 6px; min-width: 110px; }
  .prog .track { flex: 1; height: 6px; background: #2a2a2a; border-radius: 3px; overflow: hidden; }
  .prog .fill { height: 100%; background: #1976D2; }
  .prog .fill.done { background: #43a047; }
  .tag { display: inline-block; padding: 1px 6px; border-radius: 10px; background: #263238;
         color: #90caf9; font-size: 11px; }
  .state { font-size: 12px; }
  .err { margin-bottom: 10px; padding: 8px 10px; border-radius: 6px;
         background: #3e2723; color: #ffccbc; border: 1px solid #5d4037; }
  .muted { color: #757575; }
</style>
</head>
<body>
<h1>📥 下载任务监控</h1>
<div class="bar">
  <span class="stat">共 <b id="total">0</b> 个任务</span>
  <span class="stat">刷新于 <b id="updated">—</b></span>
  <input id="kw" placeholder="搜索名称 / 路径 / 标签 / 分类" style="min-width:240px">
  <select id="dler"><option value="">全部下载器</option></select>
  <select id="sortSel"></select>
  <button id="orderBtn">降序 ↓</button>
  <button id="refreshBtn">立即刷新</button>
  <label class="muted"><input type="checkbox" id="auto" checked style="vertical-align:middle"> 自动刷新</label>
</div>
<div id="errs"></div>
<div class="wrap">
  <table>
    <thead><tr id="head"></tr></thead>
    <tbody id="body"><tr><td colspan="16" class="muted">加载中…</td></tr></tbody>
  </table>
</div>
<script>
var SORT_LABELS = __SORT_LABELS__;
var INTERVAL = __INTERVAL__;
var sortKey = "__DEFAULT_SORT__";
var sortOrder = "__DEFAULT_ORDER__";
var timer = null;
var apikey = new URLSearchParams(location.search).get("apikey") || "";

// 列定义：id 对应后端字段，label 表头，cls 样式
var COLS = [
  {id:"no", label:"序号", cls:"num", sort:"seq"},
  {id:"downloader", label:"下载器", cls:"", sort:"downloader"},
  {id:"name", label:"名称", cls:"name", sort:"name"},
  {id:"progress", label:"进度", cls:"", sort:"progress"},
  {id:"size", label:"选定大小", cls:"num", sort:"size"},
  {id:"save_path", label:"保存路径", cls:"path", sort:"name"},
  {id:"added_text", label:"添加时间", cls:"", sort:"added"},
  {id:"state", label:"状态", cls:"state", sort:"state"},
  {id:"eta_text", label:"剩余时间", cls:"", sort:"eta"},
  {id:"ratio", label:"比率", cls:"num", sort:"ratio"},
  {id:"popularity", label:"流行度", cls:"num", sort:"popularity"},
  {id:"category", label:"分类", cls:"", sort:"category"},
  {id:"tags", label:"标签", cls:"", sort:"tags"},
  {id:"upspeed", label:"上传", cls:"num", sort:"upspeed"},
  {id:"dlspeed", label:"下载", cls:"num", sort:"dlspeed"}
];

function fmtSize(n) {
  n = Number(n) || 0;
  var u = ["B","KB","MB","GB","TB"]; var i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(2)) + " " + u[i];
}
function fmtSpeed(n) { return fmtSize(n) + "/s"; }
function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function renderHead() {
  var html = "";
  COLS.forEach(function(c) {
    var arrow = (c.sort === sortKey) ? (sortOrder === "asc" ? " ↑" : " ↓") : "";
    html += '<th data-sort="' + c.sort + '">' + c.label + '<span class="arrow">' + arrow + "</span></th>";
  });
  document.getElementById("head").innerHTML = html;
  document.querySelectorAll("thead th").forEach(function(th) {
    th.onclick = function() {
      var s = th.getAttribute("data-sort");
      if (sortKey === s) { sortOrder = sortOrder === "asc" ? "desc" : "asc"; }
      else { sortKey = s; sortOrder = (s === "name" || s === "state") ? "asc" : "desc"; }
      document.getElementById("sortSel").value = sortKey;
      document.getElementById("orderBtn").textContent = sortOrder === "asc" ? "升序 ↑" : "降序 ↓";
      load();
    };
  });
}

function renderRows(items) {
  if (!items.length) {
    document.getElementById("body").innerHTML =
      '<tr><td colspan="' + COLS.length + '" class="muted">没有任务</td></tr>';
    return;
  }
  var html = "";
  items.forEach(function(r) {
    var p = Math.max(0, Math.min(100, r.progress || 0));
    var cls = p >= 100 ? "fill done" : "fill";
    html += "<tr>";
    html += '<td class="num">' + r.no + "</td>";
    html += "<td>" + esc(r.downloader) + "</td>";
    html += '<td class="name" title="' + esc(r.name) + '">' + esc(r.name) + "</td>";
    html += '<td><div class="prog"><div class="track"><div class="' + cls + '" style="width:' +
            p + '%"></div></div><span class="num">' + p.toFixed(1) + "%</span></div></td>";
    html += '<td class="num">' + fmtSize(r.size) + "</td>";
    html += '<td class="path" title="' + esc(r.save_path) + '">' + esc(r.save_path) + "</td>";
    html += "<td>" + esc(r.added_text) + "</td>";
    html += '<td class="state">' + esc(r.state) + "</td>";
    html += "<td>" + esc(r.eta_text) + "</td>";
    html += '<td class="num">' + (Number(r.ratio) || 0).toFixed(2) + "</td>";
    html += '<td class="num">' + (r.popularity === null || r.popularity === undefined
            ? "—" : Number(r.popularity).toFixed(3)) + "</td>";
    html += "<td>" + esc(r.category) + "</td>";
    html += "<td>" + (r.tags ? '<span class="tag">' + esc(r.tags) + "</span>" : "") + "</td>";
    html += '<td class="num">' + fmtSpeed(r.upspeed) + "</td>";
    html += '<td class="num">' + fmtSpeed(r.dlspeed) + "</td>";
    html += "</tr>";
  });
  document.getElementById("body").innerHTML = html;
}

function fillDownloaders(items) {
  var sel = document.getElementById("dler");
  var cur = sel.value;
  var names = {};
  items.forEach(function(r) { if (r.downloader) { names[r.downloader] = 1; } });
  var html = '<option value="">全部下载器</option>';
  Object.keys(names).sort().forEach(function(n) {
    html += '<option value="' + esc(n) + '">' + esc(n) + "</option>";
  });
  sel.innerHTML = html;
  sel.value = cur;
}

function load() {
  if (!apikey) {
    document.getElementById("errs").innerHTML =
      '<div class="err">缺少 apikey，请从 MoviePilot 插件页点「打开监控面板」进入。</div>';
    return;
  }
  var url = "api/v1/plugin/DownloadMonitor/tasks?apikey=" + encodeURIComponent(apikey) +
            "&sort=" + encodeURIComponent(sortKey) +
            "&order=" + encodeURIComponent(sortOrder) +
            "&keyword=" + encodeURIComponent(document.getElementById("kw").value || "") +
            "&downloader=" + encodeURIComponent(document.getElementById("dler").value || "");
  fetch(url, {cache: "no-store"}).then(function(r) { return r.json(); }).then(function(res) {
    if (!res || res.success !== true) {
      document.getElementById("errs").innerHTML =
        '<div class="err">' + esc((res && res.message) || "加载失败") + "</div>";
      return;
    }
    var d = res.data || {};
    var items = d.items || [];
    document.getElementById("total").textContent = d.total || 0;
    document.getElementById("updated").textContent = d.updated || "—";
    var errs = d.errors || [];
    document.getElementById("errs").innerHTML = errs.length
      ? '<div class="err">' + errs.map(esc).join("；") + "</div>" : "";
    fillDownloaders(items);
    renderRows(items);
  }).catch(function(e) {
    document.getElementById("errs").innerHTML =
      '<div class="err">请求失败：' + esc(e && e.message) + "</div>";
  });
}

function schedule() {
  if (timer) { clearInterval(timer); timer = null; }
  if (document.getElementById("auto").checked) { timer = setInterval(load, INTERVAL); }
}

(function init() {
  var sel = document.getElementById("sortSel");
  Object.keys(SORT_LABELS).forEach(function(k) {
    var o = document.createElement("option");
    o.value = k; o.textContent = SORT_LABELS[k];
    sel.appendChild(o);
  });
  sel.value = sortKey;
  sel.onchange = function() { sortKey = sel.value; load(); };
  document.getElementById("orderBtn").textContent = sortOrder === "asc" ? "升序 ↑" : "降序 ↓";
  document.getElementById("orderBtn").onclick = function() {
    sortOrder = sortOrder === "asc" ? "desc" : "asc";
    this.textContent = sortOrder === "asc" ? "升序 ↑" : "降序 ↓";
    load();
  };
  document.getElementById("refreshBtn").onclick = load;
  document.getElementById("auto").onchange = schedule;
  var t = null;
  document.getElementById("kw").oninput = function() {
    if (t) { clearTimeout(t); }
    t = setTimeout(load, 350);
  };
  document.getElementById("dler").onchange = load;
  renderHead();
  load();
  schedule();
})();
</script>
</body>
</html>
"""
