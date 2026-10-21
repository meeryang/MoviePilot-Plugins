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
import re
import requests
from typing import List, Optional, Tuple

from app.log import logger
from app.plugins import _PluginBase



# 音质档位：value -> 说明
QUALITY_CHOICES = [
    {"title": "FLAC 无损（体积大、最慢）", "value": "flac"},
    {"title": "320k 高音质（推荐，默认）", "value": "320k"},
    {"title": "128k 标准（体积小）", "value": "128k"},
]

# 专辑搜索源顺序
ALBUM_ORDER_CHOICES = [
    {"title": "网易云 → QQ（推荐）", "value": "netease,qq"},
    {"title": "QQ → 网易云", "value": "qq,netease"},
    {"title": "仅网易云", "value": "netease"},
    {"title": "仅 QQ", "value": "qq"},
]

# 各音源取直链的 url 模板（{mid} 会替换成 songmid）
URL_TEMPLATES = {
    "kw": [
        "https://antiserver.kuwo.cn/anti.s?type=convert_url&format=mp3&response=url&rid=MUSIC_{mid}",
        "https://antiserver.kuwo.cn/anti.s?type=convert_url&format=aac&response=url&rid=MUSIC_{mid}",
    ],
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class LxMusicDownloader(_PluginBase):
    # 插件元信息
    plugin_name = "LX音乐下载"
    plugin_desc = "音乐专辑订阅联动：订阅-音乐新增专辑时按网易云/QQ 搜索整张专辑并经 LX音乐下载；支持音质选择"
    plugin_icon = "https://gitee.com/makimayang/obsidian/raw/main/icon.png"
    plugin_version = "2.0"
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
        # 音乐专辑订阅联动
        self._music_sub = bool(config.get("music_sub", True))
        # 专辑搜索源顺序，默认网易云优先
        self._album_order = (config.get("album_order") or "netease,qq").strip()

        need_loop = self._enabled and (self._subscribes.strip() or self._music_sub)
        if need_loop:
            self.save_data("done", self._done)
            logger.info(
                f"【{self.plugin_name}】后台检查已排程 | 关键词订阅：{bool(self._subscribes.strip())} "
                f"| 音乐专辑订阅：{self._music_sub} | 音质：{self._quality}"
            )
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
                "path": "/album_search",
                "endpoint": self.api_album_search,
                "methods": ["GET"],
                "summary": "专辑搜索测试",
                "description": "按专辑名依次调用网易云/QQ 搜索专辑并列出曲目",
            },
            {
                "path": "/music_subs",
                "endpoint": self.api_music_subs,
                "methods": ["GET"],
                "summary": "音乐订阅联动状态",
                "description": "查看已识别的音乐专辑订阅与处理记录",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "music_sub",
                                            "label": "音乐专辑订阅联动",
                                            "hint": "订阅-音乐 新增专辑时，按顺序用网易云/QQ 搜索整张专辑并逐曲下载",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "album_order",
                                            "label": "专辑搜索顺序",
                                            "items": ALBUM_ORDER_CHOICES,
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
                "music_sub": True,
                "album_order": "netease,qq",
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
        # 启动后先等 45 秒，避免和主程序启动抢资源
        time.sleep(45)
        keyword_tick = 0
        while True:
            try:
                if self._enabled and self._music_sub:
                    self.music_subscribe_check()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】音乐订阅检查异常：{e}")
            # 关键词订阅较重，6 小时一次
            try:
                if self._enabled and self._subscribes.strip() and keyword_tick % 24 == 0:
                    self.subscribe_check()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】关键词订阅异常：{e}")
            keyword_tick += 1
            time.sleep(15 * 60)  # 15 分钟一轮

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


    # ═══════════════════════════════════════════════════════════════
    # 音乐专辑订阅联动：MoviePilot 订阅-音乐 新增专辑 → 网易云/QQ 搜专辑 → 下载
    # ═══════════════════════════════════════════════════════════════

    def _h5(self) -> dict:
        """浏览器 UA，第三方接口必需"""
        return {"User-Agent": UA}

    @staticmethod
    def _norm(s: str) -> str:
        """归一化专辑名用于比对：去空格、括号内容、标点，转小写"""
        if not s:
            return ""
        t = str(s).lower()
        t = re.sub(r"[（(\[【].*?[）)\]】]", "", t)
        t = re.sub(r"[\s\-_·．.,，。!！?？'\"”“:：;；/\\|&+]", "", t)
        return t.strip()

    # ---------------- 网易云 ----------------

    def _album_search_netease(self, keyword: str) -> Optional[dict]:
        """网易云专辑搜索，返回最佳匹配 {id,name,artist,size}"""
        try:
            r = requests.get(
                "https://music.163.com/api/search/get",
                params={"s": keyword, "type": 10, "limit": 20, "offset": 0},
                headers={**self._h5(), "Referer": "https://music.163.com/",
                         "Cookie": "appver=2.0.2; os=pc"},
                timeout=25,
            )
            if r.status_code != 200:
                logger.warning(f"【LX音乐下载】网易云专辑搜索 HTTP {r.status_code}")
                return None
            albums = ((r.json() or {}).get("result") or {}).get("albums") or []
            if not albums:
                return None
            want = self._norm(keyword)
            best, best_score = None, 0.0
            for a in albums:
                name = a.get("name") or ""
                artist = ((a.get("artist") or {}).get("name")) or ""
                size = a.get("size") or 0
                nm = self._norm(name)
                if not nm or nm not in want:
                    continue
                score = 0.55 if nm == want else 0.40
                if artist:
                    if self._norm(artist) in want:
                        score += 0.30
                else:
                    score -= 0.15
                if size:
                    score += min(int(size), 30) / 300.0
                if score > best_score:
                    best_score, best = score, {
                        "id": a.get("id"), "name": name, "artist": artist, "size": size,
                    }
            if best and best_score >= 0.5:
                logger.info(f"【LX音乐下载】网易云命中专辑：{best['name']} / {best['artist']} ({best['size']} 首)")
                return best
            return None
        except Exception as e:
            logger.warning(f"【LX音乐下载】网易云专辑搜索异常：{e}")
            return None

    def _album_tracks_netease(self, album_id) -> List[dict]:
        """网易云专辑曲目列表 [{name, artist}]

        注意：/api/album/{id} 在无 Cookie 时返回 code=-462（反爬），
        必须带基础 Cookie，或改用 /api/v1/album/{id}。
        """
        hdr = {**self._h5(), "Referer": "https://music.163.com/",
               "Cookie": "appver=2.0.2; os=pc"}
        for url in (f"https://music.163.com/api/v1/album/{album_id}",
                    f"https://music.163.com/api/album/{album_id}"):
            try:
                r = requests.get(url, headers=hdr, timeout=25)
                if r.status_code != 200:
                    continue
                j = r.json() or {}
                songs = j.get("songs") or ((j.get("album") or {}).get("songs")) or []
                if not songs:
                    logger.warning(f"【LX音乐下载】网易云专辑 {album_id} 无曲目（code={j.get('code')}）")
                    continue
                out = []
                for s in songs:
                    ar = s.get("artists") or s.get("ar") or []
                    artists = "/".join(a.get("name", "") for a in ar if isinstance(a, dict))
                    out.append({"name": s.get("name") or "", "artist": artists})
                out = [x for x in out if x["name"]]
                if out:
                    return out
            except Exception as e:
                logger.warning(f"【LX音乐下载】网易云专辑详情异常：{e}")
        return []

    # ---------------- QQ 音乐 ----------------

    def _album_search_qq(self, keyword: str) -> Optional[dict]:
        """QQ 音乐专辑搜索，返回最佳匹配 {mid,name,artist,size}"""
        try:
            r = requests.get(
                "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                params={"w": keyword, "t": "8", "format": "json", "p": 1, "n": 20},
                headers={**self._h5(), "Referer": "https://y.qq.com/"},
                timeout=25,
            )
            if r.status_code != 200:
                logger.warning(f"【LX音乐下载】QQ专辑搜索 HTTP {r.status_code}")
                return None
            txt = (r.text or "").strip()
            if txt.startswith("callback("):
                txt = txt[9:-1]
            data = json.loads(txt)
            albums = ((((data.get("data") or {}).get("album") or {}).get("list"))) or []
            if not albums:
                return None
            want = self._norm(keyword)
            best, best_score = None, 0.0
            for a in albums:
                name = a.get("albumName") or ""
                # 歌手字段：顶层 singerName 最可靠，其次 singer_list[].name（不是 singerName）
                artist = a.get("singerName") or ""
                if not artist:
                    sl = a.get("singer_list") or []
                    artist = "/".join(s.get("name", "") for s in sl if isinstance(s, dict))
                size = a.get("song_count") or 0
                nm = self._norm(name)
                if not nm or nm not in want:
                    continue
                score = 0.55 if nm == want else 0.40
                if artist:
                    if self._norm(artist) in want:
                        score += 0.30
                else:
                    score -= 0.15
                if size:
                    score += min(int(size), 30) / 300.0
                if score > best_score:
                    best_score, best = score, {
                        "mid": a.get("albumMID"), "name": name, "artist": artist, "size": size,
                    }
            if best and best_score >= 0.5:
                logger.info(f"【LX音乐下载】QQ 命中专辑：{best['name']} / {best['artist']}")
                return best
            return None
        except Exception as e:
            logger.warning(f"【LX音乐下载】QQ专辑搜索异常：{e}")
            return None

    def _album_tracks_qq(self, album_mid: str) -> List[dict]:
        """QQ 专辑曲目列表 [{name, artist}]"""
        try:
            r = requests.get(
                "https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg",
                params={"albummid": album_mid, "format": "json"},
                headers={**self._h5(), "Referer": "https://y.qq.com/"},
                timeout=25,
            )
            if r.status_code != 200:
                return []
            txt = (r.text or "").strip()
            if txt.startswith("callback("):
                txt = txt[9:-1]
            data = json.loads(txt)
            lst = (data.get("data") or {}).get("list") or []
            out = []
            for s in lst:
                singers = s.get("singer") or []
                artist = "/".join((x.get("name") or x.get("singerName") or "")
                                  for x in singers if isinstance(x, dict))
                out.append({"name": s.get("songname") or "", "artist": artist})
            return [x for x in out if x["name"]]
        except Exception as e:
            logger.warning(f"【LX音乐下载】QQ专辑详情异常：{e}")
            return []

    # ---------------- 主流程 ----------------

    def find_album(self, album_name: str, expect_tracks: Optional[int] = None
                   ) -> Tuple[Optional[str], str, List[dict]]:
        """依次按配置顺序搜索专辑，返回 (来源, 专辑名, 曲目列表)"""
        order = [x.strip().lower() for x in (self._album_order or "netease,qq").split(",") if x.strip()]
        if not order:
            order = ["netease", "qq"]
        for src in order:
            if src == "netease":
                hit = self._album_search_netease(album_name)
                if hit and hit.get("id"):
                    tracks = self._album_tracks_netease(hit["id"])
                    if tracks and self._tracks_ok(tracks, expect_tracks):
                        return "netease", hit.get("name") or album_name, tracks
            elif src == "qq":
                hit = self._album_search_qq(album_name)
                if hit and hit.get("mid"):
                    tracks = self._album_tracks_qq(hit["mid"])
                    if tracks and self._tracks_ok(tracks, expect_tracks):
                        return "qq", hit.get("name") or album_name, tracks
        return None, album_name, []

    @staticmethod
    def _tracks_ok(tracks: List[dict], expect: Optional[int]) -> bool:
        """有预期曲目数时，允许 ±2 的误差，否则放行"""
        if not expect or expect <= 0:
            return True
        return abs(len(tracks) - int(expect)) <= 2

    def download_album(self, album_name: str, expect_tracks: Optional[int] = None
                       ) -> Tuple[bool, str]:
        """搜索整张专辑并逐曲下载，返回 (是否成功, 说明)"""
        err = self._check_conf()
        if err:
            return False, err
        src, real_name, tracks = self.find_album(album_name, expect_tracks)
        if not tracks:
            return False, f"网易云/QQ 均未找到专辑「{album_name}」"
        logger.info(f"【LX音乐下载】专辑「{real_name}」来自 {src}，共 {len(tracks)} 首，开始下载（音质 {self._quality}）")
        ok_cnt, skip_cnt = 0, 0
        for t in tracks:
            kw = f"{t['name']} {t['artist']}".strip()
            songs = self.search(kw, limit=3)
            if not songs:
                logger.warning(f"【LX音乐下载】LX服务端未搜到：{kw}")
                skip_cnt += 1
                time.sleep(1)
                continue
            # 优先同名曲目
            pick = None
            for s in songs:
                if self._norm(s.get("name") or "") == self._norm(t["name"]):
                    pick = s
                    break
            pick = pick or songs[0]
            if self.download(pick):
                ok_cnt += 1
            else:
                skip_cnt += 1
            time.sleep(1)
        msg = f"专辑「{real_name}」（{src}）共 {len(tracks)} 首，成功 {ok_cnt}，跳过/失败 {skip_cnt}"
        logger.info(f"【LX音乐下载】✅ {msg}")
        return (ok_cnt > 0), msg

    def music_subscribe_check(self):
        """扫描 MoviePilot 音乐专辑订阅，新订阅的自动整张下载"""
        try:
            from app.db.oper.subscribe import SubscribeOper
        except Exception as e:
            logger.error(f"【LX音乐下载】无法导入 SubscribeOper：{e}")
            return
        try:
            subs = SubscribeOper().list() or []
        except Exception as e:
            logger.error(f"【LX音乐下载】读取订阅失败：{e}")
            return

        albums = []
        for s in subs:
            if (getattr(s, "type", None) or "") != "音乐":
                continue
            mt = getattr(s, "music_type", None)
            if mt and mt != "album":
                continue
            sid = getattr(s, "id", None)
            nm = getattr(s, "name", None)
            if sid is None or not nm:
                continue
            albums.append({"id": int(sid), "name": nm,
                           "total_tracks": getattr(s, "total_tracks", None)})

        state = self.get_data("music_subs") or {}
        baseline = state.get("baseline")
        processed = state.get("processed") or {}

        # 首次运行只建基线，不回溯下载历史订阅
        if baseline is None:
            self.save_data("music_subs", {"baseline": [a["id"] for a in albums], "processed": {}})
            logger.info(f"【LX音乐下载】音乐专辑订阅基线已建立：{len(albums)} 条（历史订阅不回溯）")
            return

        base_set = set(baseline)
        targets = [a for a in albums
                   if a["id"] not in base_set and str(a["id"]) not in processed]
        if not targets:
            return

        logger.info(f"【LX音乐下载】发现 {len(targets)} 个新增音乐专辑订阅")
        for a in targets:
            try:
                ok, msg = self.download_album(a["name"], a.get("total_tracks"))
            except Exception as e:
                ok, msg = False, f"异常：{e}"
            processed[str(a["id"])] = {
                "name": a["name"], "ok": ok, "msg": msg,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.save_data("music_subs", {"baseline": baseline, "processed": processed})
            if self._notify:
                try:
                    self.post_message(
                        title=f"{'✅' if ok else '⚠️'} LX音乐 · 专辑订阅",
                        text=f"{msg}\n音质：{self._quality}",
                    )
                except Exception:
                    pass
            time.sleep(2)

    # ---------------- API 端点实现 ----------------

    def api_album_search(self, keyword: str = "", download: bool = False):
        """专辑搜索测试：列出网易云/QQ 命中的曲目"""
        if not keyword:
            return {"success": False, "message": "请提供 keyword（专辑名，可带歌手）"}
        try:
            src, real, tracks = self.find_album(keyword)
        except Exception as e:
            return {"success": False, "message": f"搜索异常：{e}"}
        if not tracks:
            return {"success": False, "message": "网易云/QQ 均未找到该专辑",
                    "order": self._album_order}
        result = {
            "success": True,
            "source": src,
            "album": real,
            "track_count": len(tracks),
            "quality": self._quality,
            "tracks": tracks[:50],
        }
        if download:
            ok, msg = self.download_album(keyword)
            result["download"] = {"ok": ok, "message": msg}
        return result

    def api_music_subs(self):
        """查看音乐订阅联动状态"""
        state = self.get_data("music_subs") or {}
        processed = state.get("processed") or {}
        return {
            "enabled": self._enabled,
            "music_subscribe": self._music_sub,
            "quality": self._quality,
            "album_order": self._album_order,
            "baseline_count": len(state.get("baseline") or []),
            "processed_count": len(processed),
            "processed": processed,
        }
