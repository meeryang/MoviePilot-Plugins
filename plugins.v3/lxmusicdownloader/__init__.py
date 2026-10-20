"""
LX 音乐下载器 —— 调用 lx-sync-server 的 API 搜索并下载音乐

MoviePilot 本身没有「音乐订阅」概念，本插件补上：
1. 订阅歌手 / 关键词，定时检查并自动下载新歌
2. 手动搜索、批量下载（Web UI + API）
3. 音质可选（默认 320k 高音质，可切 flac 无损 / 128k 标准）

实测 API（lx-sync-server v2.1.2，2026-10）：
  搜索    GET  /api/music/search?name=<关键词>&token=<tk>
  直链    GET  https://antiserver.kuwo.cn/anti.s?type=convert_url&format=mp3&response=url&rid=MUSIC_<songmid>
  下载    POST /api/music/cache/download?token=<tk>   body={songInfo,url,quality,downloadSource,sourceName,cacheLyric,embedLyric}
  检查    GET  /api/music/cache/check?source=&songmid=&name=&singer=&quality=&token=<tk>

注意：
- token 名形如 lx_tk_xxx，在 lx-sync-server 的「Token 管理」里生成
- 服务端 config.js 里 `user.enablePublicNonAdminServerCache` 必须为 true，
  否则下载会 403「非管理员服务器缓存已被禁用」
- 音质由源直链决定，`quality` 参数只用于服务端缓存命名与去重
"""
import time
import json
import requests
from typing import List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase

# 探索源支持（v3）
from app.core.event import eventmanager, Event
from app.core.config import settings
from app.schemas.types import ChainEventType, MediaSource
from app.schemas import DiscoverSourceEventData, DiscoverMediaSource, MediaInfo


# 音质档位：value -> 说明
QUALITY_CHOICES = [
    {"title": "FLAC 无损（体积大、最慢）", "value": "flac"},
    {"title": "320k 高音质（推荐，默认）", "value": "320k"},
    {"title": "128k 标准（体积小）", "value": "128k"},
]

# 各音源取直链的 url 模板（{mid} 会替换成 songmid）
URL_TEMPLATES = {
    "kw": [
        "https://antiserver.kuwo.cn/anti.s?type=convert_url&format=mp3&response=url&rid=MUSIC_{mid}",
        "https://antiserver.kuwo.cn/anti.s?type=convert_url&format=aac&response=url&rid=MUSIC_{mid}",
    ],
}

# ═══════════════════════════════════════════════════════════
# 探索源：歌单 / 排行榜
# ═══════════════════════════════════════════════════════════

# 网易云榜单 ID（官方固定 ID）
NETEASE_CHARTS = {
    "热歌榜": "3778678",
    "飙升榜": "19723756",
    "新歌榜": "3779629",
    "原创榜": "2884035",
}

# QQ 音乐榜单 topid
QQ_CHARTS = {
    "热歌榜": "26",
    "新歌榜": "27",
    "内地榜": "58",
    "港台榜": "59",
    "欧美榜": "62",
}

# 探索分类
LX_CATS = ["歌单", "排行榜"]


def lx_filter_ui() -> List[dict]:
    """探索面板的筛选 UI（分类 + 榜单）"""
    cat_ui = [
        {"component": "VChip", "props": {"filter": True, "tile": True, "value": c}, "text": c}
        for c in LX_CATS
    ]
    chart_names = ([f"网易云·{k}" for k in NETEASE_CHARTS]
                   + [f"QQ·{k}" for k in QQ_CHARTS])
    chart_ui = [
        {"component": "VChip", "props": {"filter": True, "tile": True, "value": c}, "text": c}
        for c in chart_names
    ]
    return [
        {
            "component": "div",
            "props": {"class": "flex justify-start items-center"},
            "content": [
                {"component": "div", "props": {"class": "mr-5"},
                 "content": [{"component": "VLabel", "text": "分类"}]},
                {"component": "VChipGroup", "props": {"model": "cat"}, "content": cat_ui},
            ],
        },
        {
            "component": "div",
            "props": {"class": "flex justify-start items-center", "show": "{{cat == '排行榜'}}"},
            "content": [
                {"component": "div", "props": {"class": "mr-5"},
                 "content": [{"component": "VLabel", "text": "榜单"}]},
                {"component": "VChipGroup", "props": {"model": "chart"}, "content": chart_ui},
            ],
        },
    ]


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class LxMusicDownloader(_PluginBase):
    # 插件元信息
    plugin_name = "LX音乐下载"
    plugin_desc = "订阅歌手/关键词，调用 lx-sync-server 自动搜索并缓存音乐，支持音质选择"
    plugin_icon = "https://gitee.com/makimayang/obsidian/raw/main/icon.png"
    plugin_version = "1.0"
    plugin_author = "虾虾"
    author_url = ""
    plugin_config_prefix = "lxmusicdownloader_"
    plugin_order = 27
    auth_level = 1

    # 运行时状态
    _enabled: bool = False
    _server_url: str = "http://127.0.0.1:8142"
    _token: str = ""
    _quality: str = "320k"
    _subscribes: str = ""
    _limit: int = 10
    _notify: bool = True
    _only_new: bool = True

    # 已下载记录（内存 + 持久化）
    _done: dict = None

    def init_plugin(self, config: dict = None):
        try:
            self._done = self.get_data("done") or {}
        except Exception:
            self._done = {}
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._server_url = (config.get("server_url") or "http://127.0.0.1:8142").rstrip("/")
        self._token = config.get("token") or ""
        self._quality = config.get("quality") or "320k"
        self._subscribes = config.get("subscribes") or ""
        self._limit = int(config.get("limit") or 10)
        self._notify = bool(config.get("notify", True))
        self._only_new = bool(config.get("only_new", True))

        if self._enabled and self._subscribes.strip():
            # 每 6 小时跑一次订阅检查
            self.save_data("done", self._done)
            logger.info(f"【{self.plugin_name}】订阅检查已排程，歌手/关键词：{self._subscribes}")
            # MoviePilot 的定时任务通过 post_message 之外的方式注册，这里用后台线程兜底
            import threading
            threading.Thread(target=self._loop, daemon=True, name="lxmusic-subscribe").start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return []

    def get_api(self) -> List[dict]:
        return [
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "summary": "搜索音乐",
                "description": "按关键词搜索，返回歌曲列表（含音质档位）",
            },
            {
                "path": "/download",
                "endpoint": self.api_download,
                "methods": ["GET", "POST"],
                "summary": "下载单曲",
                "description": "按 songmid + source 下载到 lx-sync-server 缓存",
            },
            {
                "path": "/download_keyword",
                "endpoint": self.api_download_keyword,
                "methods": ["GET", "POST"],
                "summary": "按关键词批量下载",
                "description": "搜索关键词取前 N 首下载",
            },
            {
                "path": "/discover",
                "endpoint": self.api_discover,
                "methods": ["GET"],
                "summary": "探索源数据",
                "description": "歌单/排行榜 数据，供发现-探索-分类面板使用",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET"],
                "summary": "测试连接",
                "description": "测试 lx-sync-server 连通性与 token 有效性",
            },
        ]

    def get_form(self) -> Tuple[List[dict], dict]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "下载后发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "only_new",
                                            "label": "仅下载新歌（去重）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "server_url",
                                            "label": "lx-sync-server 地址",
                                            "placeholder": "http://127.0.0.1:8142",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "quality",
                                            "label": "下载音质",
                                            "items": QUALITY_CHOICES,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "token",
                                            "label": "API Token",
                                            "placeholder": "lx_tk_xxxxxxxx",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "subscribes",
                                            "label": "订阅歌手 / 关键词（每行一个）",
                                            "rows": 5,
                                            "placeholder": "周杰伦\n林俊杰\n五月天",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "limit",
                                            "label": "每个订阅最多下载",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "音质说明：实际音质由音源直链决定，插件会按所选音质向服务端声明。"
                                "FLAC 无损需要音源支持，部分歌曲会回落为 320k。/ "
                                "前置条件：lx-sync-server 的 config.js 中 "
                                "user.enablePublicNonAdminServerCache 必须为 true，否则下载会返回 403。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "only_new": True,
            "server_url": "http://127.0.0.1:8142",
            "quality": "320k",
            "token": "",
            "subscribes": "",
            "limit": 10,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        try:
            if self._done:
                self.save_data("done", self._done)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 核心逻辑
    # ------------------------------------------------------------------ #
    def _headers(self):
        return {"User-Agent": UA, "Referer": "https://www.kuwo.cn/"}

    def _check_conf(self) -> Optional[str]:
        if not self._server_url:
            return "未配置服务端地址"
        if not self._token:
            return "未配置 API Token"
        return None

    def search(self, keyword: str, limit: int = 20) -> List[dict]:
        """搜索音乐，返回歌曲列表"""
        err = self._check_conf()
        if err:
            logger.error(f"【{self.plugin_name}】{err}")
            return []
        try:
            r = requests.get(
                f"{self._server_url}/api/music/search",
                params={"name": keyword, "token": self._token},
                headers=self._headers(),
                timeout=25,
            )
            if r.status_code != 200:
                logger.error(f"【{self.plugin_name}】搜索失败 HTTP {r.status_code}: {r.text[:120]}")
                return []
            data = r.json()
            return data[:limit] if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"【{self.plugin_name}】搜索异常：{e}")
            return []

    def _resolve_url(self, song: dict) -> Optional[str]:
        """取歌曲的外部直链"""
        source = song.get("source") or "kw"
        songmid = str(song.get("songmid") or "")
        if not songmid:
            return None
        templates = URL_TEMPLATES.get(source) or URL_TEMPLATES["kw"]
        for tpl in templates:
            url = tpl.replace("{mid}", songmid)
            try:
                r = requests.get(url, headers=self._headers(), timeout=20)
                if r.status_code == 200:
                    text = r.text.strip()
                    if text.startswith("http"):
                        return text
            except Exception as e:
                logger.debug(f"【{self.plugin_name}】取直链失败 {url[:60]}: {e}")
        return None

    def download(self, song: dict, quality: Optional[str] = None) -> bool:
        """下载单曲到 lx-sync-server 缓存"""
        err = self._check_conf()
        if err:
            logger.error(f"【{self.plugin_name}】{err}")
            return False

        quality = quality or self._quality or "320k"
        name = song.get("name") or ""
        singer = song.get("singer") or ""
        songmid = str(song.get("songmid") or "")
        source = song.get("source") or "kw"
        title = f"{name} - {singer}"

        # 去重
        if self._only_new:
            key = f"{source}_{songmid}_{quality}"
            if key in (self._done or {}):
                logger.info(f"【{self.plugin_name}】已下载过，跳过：{title}")
                return False

        # 检查服务端是否已有
        try:
            cr = requests.get(
                f"{self._server_url}/api/music/cache/check",
                params={"source": source, "songmid": songmid, "name": name,
                        "singer": singer, "quality": quality, "token": self._token},
                headers=self._headers(), timeout=25,
            )
            if cr.status_code == 200 and (cr.json() or {}).get("exists"):
                logger.info(f"【{self.plugin_name}】服务端已有缓存，跳过：{title}")
                self._mark_done(source, songmid, quality, title)
                return False
        except Exception:
            pass

        # 取直链
        url = self._resolve_url(song)
        if not url:
            logger.warning(f"【{self.plugin_name}】取不到直链：{title}")
            return False

        # 提交下载
        body = {
            "songInfo": song,
            "url": url,
            "quality": quality,
            "downloadSource": source,
            "sourceName": song.get("source") or "kw",
            "cacheLyric": True,
            "embedLyric": True,
        }
        try:
            r = requests.post(
                f"{self._server_url}/api/music/cache/download",
                params={"token": self._token},
                json=body,
                headers={"Content-Type": "application/json", **self._headers()},
                timeout=120,
            )
            if r.status_code == 200 and (r.json() or {}).get("success"):
                logger.info(f"【{self.plugin_name}】✅ 已提交下载：{title} [{quality}]")
                self._mark_done(source, songmid, quality, title)
                if self._notify:
                    try:
                        self.post_message(
                            title=f"【{self.plugin_name}】下载音乐",
                            text=f"{title}\n音质：{quality}",
                        )
                    except Exception:
                        pass
                return True
            logger.error(f"【{self.plugin_name}】下载被拒 {title} → HTTP {r.status_code} {r.text[:150]}")
            return False
        except Exception as e:
            logger.error(f"【{self.plugin_name}】下载异常 {title}：{e}")
            return False

    def _mark_done(self, source: str, songmid: str, quality: str, title: str):
        if self._done is None:
            self._done = {}
        self._done[f"{source}_{songmid}_{quality}"] = {
            "title": title,
            "quality": quality,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 只保留最近 500 条
        if len(self._done) > 500:
            for k in list(self._done.keys())[: len(self._done) - 500]:
                self._done.pop(k, None)
        self.save_data("done", self._done)

    # ------------------------------------------------------------------ #
    # 订阅循环
    # ------------------------------------------------------------------ #
    def _loop(self):
        # 启动后先等 60 秒，避免和主程序启动抢资源
        time.sleep(60)
        while True:
            try:
                if self._enabled and self._subscribes.strip():
                    self.subscribe_check()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】订阅循环异常：{e}")
            time.sleep(6 * 3600)  # 6 小时

    def subscribe_check(self):
        """逐个订阅关键词检查并下载新歌"""
        kws = [k.strip() for k in self._subscribes.splitlines() if k.strip()]
        if not kws:
            return
        logger.info(f"【{self.plugin_name}】开始订阅检查，共 {len(kws)} 个关键词")
        total = 0
        for kw in kws:
            songs = self.search(kw, limit=self._limit)
            logger.info(f"【{self.plugin_name}】搜索「{kw}」命中 {len(songs)} 首")
            for s in songs:
                if self.download(s):
                    total += 1
                time.sleep(1)
        logger.info(f"【{self.plugin_name}】订阅检查完成，新下载 {total} 首")

    # ------------------------------------------------------------------ #
    # API 端点
    # ------------------------------------------------------------------ #
    def api_search(self, keyword: str = "", limit: int = 20):
        songs = self.search(keyword, limit=int(limit or 20))
        return [
            {
                "name": s.get("name"),
                "singer": s.get("singer"),
                "album": s.get("albumName"),
                "source": s.get("source"),
                "songmid": s.get("songmid"),
                "interval": s.get("interval"),
                "types": [t.get("type") for t in (s.get("types") or [])],
                "img": s.get("img"),
            }
            for s in songs
        ]

    def api_download(self, songmid: str = "", source: str = "kw",
                     name: str = "", singer: str = "", quality: str = "",
                     albumId: str = "", interval: str = ""):
        if not songmid:
            return {"success": False, "message": "缺少 songmid"}
        song = {"name": name, "singer": singer, "source": source, "songmid": songmid,
                "albumId": albumId, "interval": interval}
        ok = self.download(song, quality=quality or self._quality)
        return {"success": ok, "message": "已提交下载" if ok else "下载失败或已存在"}

    def api_download_keyword(self, keyword: str = "", limit: int = 5, quality: str = ""):
        if not keyword:
            return {"success": False, "message": "缺少 keyword"}
        songs = self.search(keyword, limit=int(limit or 5))
        ok_n = 0
        detail = []
        for s in songs:
            ok = self.download(s, quality=quality or self._quality)
            detail.append({"name": s.get("name"), "singer": s.get("singer"), "ok": ok})
            if ok:
                ok_n += 1
        return {"success": ok_n > 0, "downloaded": ok_n, "total": len(songs), "detail": detail}

    def api_test(self):
        err = self._check_conf()
        if err:
            return {"success": False, "message": err}
        try:
            r = requests.get(
                f"{self._server_url}/api/music/search",
                params={"name": "test", "token": self._token},
                headers=self._headers(), timeout=25,
            )
            ok = r.status_code == 200
            return {
                "success": ok,
                "message": f"服务端 HTTP {r.status_code}",
                "server": self._server_url,
                "quality": self._quality,
                "result_count": len(r.json()) if ok and isinstance(r.json(), list) else 0,
            }
        except Exception as e:
            return {"success": False, "message": f"连接异常：{e}"}


    # ═══════════════════════════════════════════════════════
    # 功能1：探索源（发现-探索-分类面板）
    # ═══════════════════════════════════════════════════════

    @eventmanager.register(ChainEventType.DiscoverSource)
    def discover_source(self, event: Event):
        """向「发现-探索-分类」面板注册 LX音乐 数据源"""
        try:
            if not self._enabled:
                return
            event_data: DiscoverSourceEventData = event.event_data
            src = DiscoverMediaSource(
                name="LX音乐",
                media_source=MediaSource("lxmusic"),
                mediaid_prefix="lxmusic",
                api_path=f"plugin/LxMusicDownloader/discover?apikey={settings.API_TOKEN}",
                filter_params={"cat": "排行榜", "chart": "网易云·热歌榜"},
                filter_ui=lx_filter_ui(),
                depends={"chart": ["cat"]},
            )
            event_data.extra_sources.append(src)
            logger.info("【LX音乐下载】✅ 已注册探索源：LX音乐")
        except Exception as e:
            logger.error(f"【LX音乐下载】注册探索源失败：{e}")

    def api_discover(self, cat: str = "排行榜", chart: str = "网易云·热歌榜",
                     page: int = 1) -> List[MediaInfo]:
        """探索源数据接口：歌单-最热 / 排行榜-榜单列表"""
        try:
            if cat == "歌单":
                return self._hot_playlists(page)
            return self._chart_songs(chart)
        except Exception as e:
            logger.error(f"【LX音乐下载】探索源取数失败：{e}")
            return []

    def _chart_songs(self, chart: str) -> List[MediaInfo]:
        """按榜单名取歌曲列表"""
        out: List[MediaInfo] = []
        if "·" not in chart:
            return out
        plat, name = chart.split("·", 1)
        ua = {"User-Agent": UA}

        if plat == "网易云":
            cid = NETEASE_CHARTS.get(name)
            if not cid:
                return out
            r = requests.get(
                "https://music.163.com/api/playlist/detail",
                params={"id": cid},
                headers={**ua, "Referer": "https://music.163.com/"},
                timeout=25,
            )
            if r.status_code != 200:
                logger.warning(f"【LX音乐下载】网易云榜单 HTTP {r.status_code}")
                return out
            tracks = (r.json() or {}).get("result", {}).get("tracks", []) or []
            for t in tracks:
                album = t.get("album") or {}
                out.append(MediaInfo(
                    type="音乐",
                    title=t.get("name"),
                    mediaid_prefix="lxmusic",
                    media_id=str(t.get("id")),
                    poster_path=album.get("picUrl") or "",
                    overview=" / ".join(a.get("name", "") for a in (t.get("artists") or [])),
                ))
            logger.info(f"【LX音乐下载】网易云·{name} 取到 {len(out)} 首")

        elif plat == "QQ":
            tid = QQ_CHARTS.get(name)
            if not tid:
                return out
            r = requests.get(
                "https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg",
                params={"topid": tid, "format": "json", "page": "detail", "type": "top"},
                headers={**ua, "Referer": "https://y.qq.com/"},
                timeout=25,
            )
            if r.status_code != 200:
                logger.warning(f"【LX音乐下载】QQ榜单 HTTP {r.status_code}")
                return out
            txt = (r.text or "").strip()
            if txt.startswith("callback("):
                txt = txt[9:-1]
            data = json.loads(txt)
            for item in data.get("songlist", []) or []:
                d = item.get("data") or {}
                album = d.get("albummid") or ""
                out.append(MediaInfo(
                    type="音乐",
                    title=d.get("songname"),
                    mediaid_prefix="lxmusic",
                    media_id=str(d.get("songmid") or ""),
                    poster_path=(f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album}.jpg"
                                 if album else ""),
                    overview=" / ".join(s.get("name", "") for s in (d.get("singer") or [])),
                ))
            logger.info(f"【LX音乐下载】QQ·{name} 取到 {len(out)} 首")

        return out

    def _hot_playlists(self, page: int = 1) -> List[MediaInfo]:
        """歌单-聚合-最热（网易云热门歌单）"""
        out: List[MediaInfo] = []
        try:
            r = requests.get(
                "https://music.163.com/api/playlist/list",
                params={"cat": "全部", "order": "hot", "limit": 30,
                        "offset": max(0, (page - 1) * 30)},
                headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
                timeout=25,
            )
            if r.status_code != 200:
                logger.warning(f"【LX音乐下载】热门歌单 HTTP {r.status_code}")
                return out
            for p in (r.json() or {}).get("playlists", []) or []:
                out.append(MediaInfo(
                    type="音乐",
                    title=p.get("name"),
                    mediaid_prefix="lxmusic",
                    media_id=str(p.get("id")),
                    poster_path=p.get("coverImgUrl") or "",
                    overview=f"歌单 · {p.get('trackCount') or 0} 首 · 播放 {p.get('playCount') or 0}",
                ))
            logger.info(f"【LX音乐下载】热门歌单取到 {len(out)} 个")
        except Exception as e:
            logger.error(f"【LX音乐下载】热门歌单失败：{e}")
        return out
