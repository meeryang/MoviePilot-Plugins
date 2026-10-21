"""
LX 音乐下载器 —— 调用 lx-sync-server 的 API 搜索并下载音乐

MoviePilot 本身没有「音乐订阅」概念，本插件补上：
1. 订阅歌手 / 关键词，定时检查并自动下载新歌
2. 手动搜索、批量下载（Web UI + API）
3. 音质可选（默认 320k 高音质，可切 flac 无损 / 128k 标准）

实测 API（lx-sync-server v2.1.2，2026-10）：
  搜索    GET  /api/music/search?name=<关键词>&source=wy&token=<tk>   （source 可指定音源）
  直链    GET  https://music.163.com/song/media/outer/url?id=<songmid>.mp3
  下载    POST /api/music/cache/download?token=<tk>   body={songInfo,url,quality,downloadSource,sourceName,cacheLyric,embedLyric}
  检查    GET  /api/music/cache/check?source=&songmid=&name=&singer=&quality=&token=<tk>

注意：
- token 名形如 lx_tk_xxx，在 lx-sync-server 的「Token 管理」里生成
- 服务端 config.js 里 `user.enablePublicNonAdminServerCache` 必须为 true，
  否则下载会 403「非管理员服务器缓存已被禁用」
- 音质由源直链决定，`quality` 参数只用于服务端缓存命名与去重
"""
import os
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
# 本插件不使用酷我（kw）接口：酷我公开接口只给 128k 且随时可能失效
URL_TEMPLATES = {
    "wy": [
        "https://music.163.com/song/media/outer/url?id={mid}.mp3",
    ],
}

# 下载音源优先级：酷狗 → QQ → 网易云。仅排除 kw（酷我，用户要求）
SEARCH_SOURCES = ["kg", "tx", "wy"]

# 队列并发数与音质回落顺序
QUEUE_CONCURRENCY = 3
QUALITY_FALLBACK = ["320k", "128k"]     # 首选 320k，拿不到自动回落 128k
QUEUE_WAIT_SEC = 75                     # 每档音质等待队列跑完的时间

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class LxMusicDownloader(_PluginBase):
    # 插件元信息
    plugin_name = "LX音乐下载"
    plugin_desc = "音乐专辑订阅联动：订阅-音乐新增专辑时按网易云/QQ 搜索整张专辑并经 LX音乐下载；支持音质选择"
    plugin_icon = "https://gitee.com/makimayang/obsidian/raw/main/icon.png"
    plugin_version = "3.0"
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

    # 类级单例：避免每次 reload 都新开后台线程导致重复轮询
    _bg_thread = None
    _inst = None

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
        # 每次下载完成后是否清空 cache/_open（公开用户缓存目录）
        self._clear_open_cache = bool(config.get("clear_open_cache", True))
        # 自动清理：删除小于该值（KB）的文件，0 表示关闭
        try:
            self._min_size_kb = int(config.get("min_size_kb") or 500)
        except Exception:
            self._min_size_kb = 500
        # 宿主机音乐根目录在 MoviePilot 容器内的映射路径
        self._local_music_dir = (config.get("local_music_dir") or "/media/downloads/music").strip()
        # LX音乐用户名（决定文件落到哪个用户的音乐库目录）
        self._lx_user = (config.get("lx_user") or "admin").strip() or "admin"
        # LX音乐管理员密码（用于清理低音质重复文件，可留空）
        self._lx_admin_pwd = (config.get("lx_admin_pwd") or "").strip()
        # 轮询间隔（分钟），默认 30 分钟
        try:
            self._interval = max(1, int(config.get("interval") or 30))
        except Exception:
            self._interval = 30

        need_loop = self._enabled and (self._subscribes.strip() or self._music_sub)
        if need_loop:
            self.save_data("done", self._done)
            cls = LxMusicDownloader
            cls._inst = self          # 后台线程始终取最新实例的配置
            logger.info(
                f"【{self.plugin_name}】后台检查已排程 | 关键词订阅：{bool(self._subscribes.strip())} "
                f"| 音乐专辑订阅：{self._music_sub} | 音质：{self._quality} "
                f"| 轮询间隔：{self._interval} 分钟"
            )
            import threading
            if cls._bg_thread is not None and cls._bg_thread.is_alive():
                logger.info(f"【{self.plugin_name}】后台轮询线程已在运行，复用（不重复启动）")
            else:
                t = threading.Thread(target=self._loop, daemon=True, name="lxmusic-subscribe")
                t.start()
                cls._bg_thread = t
                logger.info(f"【{self.plugin_name}】后台轮询线程已启动")

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
                "path": "/run_once",
                "endpoint": self.api_run_once,
                "methods": ["GET", "POST"],
                "summary": "运行一次",
                "description": "立即执行一次音乐订阅轮询（等同定时任务手动触发）",
            },
            {
                "path": "/clear_open",
                "endpoint": self.api_clear_open,
                "methods": ["GET", "POST"],
                "summary": "清空 cache/_open",
                "description": "删除公开用户缓存目录 cache/_open 下的所有文件",
            },
            {
                "path": "/clean",
                "endpoint": self.api_clean,
                "methods": ["GET", "POST"],
                "summary": "清理小文件",
                "description": "删除音乐库/缓存里小于设定阈值（KB）的文件",
            },
            {
                "path": "/promote",
                "endpoint": self.api_promote,
                "methods": ["GET", "POST"],
                "summary": "搬移缓存到音乐库",
                "description": "把缓存目录里的歌搬进 /server/music（本地音乐可见），同歌只留最高音质",
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
        """插件配置表单：按 12 列网格整齐排版
        行1 基本开关(3) | 行2 连接(2) | 行3 音乐订阅(3)
        行4 关键词订阅(2) | 行5 轮询与清理(3) | 行6 身份与路径(3) | 行7 提示
        """

        def col(md, *content):
            return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": list(content)}

        def switch(model, label, hint=None):
            props = {"model": model, "label": label, "color": "primary", "hide-details": "auto"}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {"component": "VSwitch", "props": props}

        def field(model, label, placeholder=None, ftype="text", hint=None):
            props = {"model": model, "label": label, "variant": "outlined", "density": "comfortable"}
            if placeholder:
                props["placeholder"] = placeholder
            if ftype != "text":
                props["type"] = ftype
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {"component": "VTextField", "props": props}

        def select(model, label, items, hint=None):
            props = {"model": model, "label": label, "variant": "outlined",
                     "density": "comfortable", "items": items}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {"component": "VSelect", "props": props}

        rows = []

        # 行1：基本开关
        rows.append({"component": "VRow", "content": [
            col(4, switch("enabled", "启用插件")),
            col(4, switch("notify", "下载完成发送通知")),
            col(4, switch("only_new", "仅下载未下载过的")),
        ]})

        # 行2：连接
        rows.append({"component": "VRow", "content": [
            col(8, field("server_url", "LX音乐服务端地址", "http://127.0.0.1:8142")),
            col(4, field("token", "API Token", "lx_tk_xxxxxxxx", "password")),
        ]})

        # 行3：音乐订阅
        rows.append({"component": "VRow", "content": [
            col(4, switch("music_sub", "联动 MoviePilot 音乐订阅", "订阅里的专辑自动下载到 LX音乐")),
            col(4, select("quality", "下载音质",
                          [{"title": "FLAC 无损", "value": "flac"},
                           {"title": "320k 高音质（推荐）", "value": "320k"},
                           {"title": "128k 标准", "value": "128k"}])),
            col(4, select("album_order", "专辑搜索源顺序",
                          [{"title": "网易云 → QQ（推荐）", "value": "netease,qq"},
                           {"title": "QQ → 网易云", "value": "qq,netease"},
                           {"title": "仅网易云", "value": "netease"},
                           {"title": "仅 QQ", "value": "qq"}])),
        ]})

        # 行4：关键词订阅
        rows.append({"component": "VRow", "content": [
            col(8, {"component": "VTextarea", "props": {
                "model": "subscribes", "label": "订阅歌手 / 关键词（每行一个）",
                "rows": 3, "variant": "outlined", "density": "comfortable",
                "placeholder": "周杰伦\n林俊杰\n五月天",
            }}),
            col(4, field("limit", "每个订阅最多下载", "10", "number")),
        ]})

        # 行5：轮询与清理
        rows.append({"component": "VRow", "content": [
            col(4, field("interval", "轮询间隔（分钟）", "30", "number", "默认 30 分钟一轮")),
            col(4, field("min_size_kb", "自动删除小于（KB）", "500", "number", "填 0 表示关闭")),
            col(4, switch("clear_open_cache", "下载后清空 cache/_open",
                          "每次下载完成删除公开用户缓存目录")),
        ]})

        # 行6：身份与路径
        rows.append({"component": "VRow", "content": [
            col(4, field("lx_user", "LX音乐用户名", "admin", "text",
                         "须与浏览器登录账号一致")),
            col(4, field("lx_admin_pwd", "LX音乐管理员密码", "可选", "password",
                         "填了才会自动删除低音质文件")),
            col(4, field("local_music_dir", "音乐根目录（容器内）",
                         "/media/downloads/music", "text", "MoviePilot 内的映射路径")),
        ]})

        # 行7：提示
        rows.append({"component": "VRow", "content": [
            col(12, {"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "density": "comfortable",
                "text": ("下载流程：搜索专辑 → 进专辑取曲目 → 提交 lxserver 队列缓存 → "
                         "搬进音乐库 /server/music → 清理小文件与 cache/_open。"
                         "音源仅用 酷狗 / QQ / 网易云，不使用酷我。"
                         "前置条件：lx-sync-server 的 config.js 中 "
                         "user.enablePublicNonAdminServerCache 需为 true。"),
            }}),
        ]})

        return [{"component": "VForm", "content": rows}], {
            "enabled": False,
            "notify": True,
            "only_new": True,
            "server_url": "http://127.0.0.1:8142",
            "token": "",
            "quality": "320k",
            "music_sub": True,
            "album_order": "netease,qq",
            "subscribes": "",
            "limit": 10,
            "interval": 30,
            "clear_open_cache": True,
            "min_size_kb": 500,
            "local_music_dir": "/media/downloads/music",
            "lx_user": "admin",
            "lx_admin_pwd": "",
        }

    def get_page(self) -> List[dict]:
        """插件详情页：提供「运行一次」立即触发轮询"""
        onclick = (
            "function(e) {"
            "  window.MoviePilotAPI.post('plugin/LxMusicDownloader/run_once', {})"
            "    .then(function(r) {"
            "      if (!r || r.success === false) {"
            "        alert(r && r.message ? r.message : '运行失败，请查看日志'); return; }"
            "      alert(r.message || '已执行一次轮询');"
            "    })"
            "    .catch(function(err) { console.error(err); alert('运行失败，请查看日志'); });"
            "}"
        )
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "flat",
                                    "block": True,
                                    "prepend-icon": "mdi-play-circle-outline",
                                    "onclick": onclick,
                                },
                                "text": "运行一次",
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 8},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "text": (
                                        f"定时轮询间隔 {getattr(self, '_interval', 30)} 分钟。"
                                        f"点「运行一次」会立刻扫描 MoviePilot 音乐订阅里的新专辑并提交下载。"
                                    ),
                                },
                            }
                        ],
                    },
                ],
            }
        ]

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
        """请求头。带上 x-user-name + x-user-token 才会以该用户身份操作，
        文件才会落到该用户的音乐库目录（否则一律落到公开的 _open 目录，前端登录后看不见）。"""
        h = {"User-Agent": UA, "Referer": "https://music.163.com/"}
        if self._token:
            h["x-user-token"] = self._token
        user = (getattr(self, "_lx_user", "") or "admin").strip()
        h["x-user-name"] = user
        return h

    def _check_conf(self) -> Optional[str]:
        if not self._server_url:
            return "未配置服务端地址"
        if not self._token:
            return "未配置 API Token"
        return None

    def search(self, keyword: str, limit: int = 20, source: Optional[str] = None) -> List[dict]:
        """搜索音乐，返回歌曲列表。source 可指定音源（默认 wy，永不用 kw）"""
        err = self._check_conf()
        if err:
            logger.error(f"【{self.plugin_name}】{err}")
            return []
        params = {"name": keyword, "token": self._token}
        src = (source or SEARCH_SOURCES[0]).lower()
        if src and src != "kw":
            params["source"] = src
        try:
            r = requests.get(
                f"{self._server_url}/api/music/search",
                params=params,
                headers=self._headers(),
                timeout=25,
            )
            if r.status_code != 200:
                logger.error(f"【{self.plugin_name}】搜索失败 HTTP {r.status_code}: {r.text[:120]}")
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            # 双保险：kw（酷我）一律剔除
            data = [x for x in data if (x.get("source") or "").lower() != "kw"]
            return data[:limit]
        except Exception as e:
            logger.error(f"【{self.plugin_name}】搜索异常：{e}")
            return []

    @staticmethod
    def _singer_parts(s: str) -> set:
        """把歌手字段拆成集合：'Jimin/Loco' -> {'jimin','loco'}"""
        s = (s or "").lower()
        s = re.sub(r"[（(\[【].*?[）)\]】]", " ", s)   # 去掉括号内容
        for ch in ["、", "，", ",", "/", "&", "feat.", "ft.", "with", "\u3001"]:
            s = s.replace(ch, "|")
        parts = {p.strip() for p in s.split("|") if p.strip()}
        out = set()
        for p in parts:
            p = re.sub(r"\s+", "", p)
            if p:
                out.add(p)
        return out

    def _singer_match(self, cand_singer: str, target_artist: str) -> bool:
        """严格判定：候选项的歌手必须真的等于目标歌手（不接受 Jiminy≈Jimin 这种）"""
        cand = self._singer_parts(cand_singer)
        targ = self._singer_parts(target_artist)
        if not targ or not cand:
            return False
        # 任一部分完全相等才算命中（避免子串误判）
        return bool(cand & targ)

    def _switch_to_netease(self, song: dict) -> dict:
        """把非网易云的歌曲换成网易云对应版本 —— 本插件不碰酷我直链"""
        src_now = (song.get("source") or "").lower()
        if src_now == "wy":
            return song
        name = song.get("name") or ""
        singer = song.get("singer") or ""
        kw = f"{name} {singer}".strip()
        if not kw:
            return song
        cands = self.search(kw, limit=6, source="wy")
        if not cands:
            logger.warning(f"【{self.plugin_name}】网易云无对应版本：{kw}")
            return song
        nname = self._norm(name)
        best = None
        for c in cands:
            if self._norm(c.get("name") or "") != nname:
                continue
            if self._singer_match(c.get("singer") or "", singer):
                best = c
                break
        if not best:
            logger.warning(f"【{self.plugin_name}】网易云无同曲同歌手版本（拒绝翻唱）：{kw}")
            return song
        logger.info(f"【{self.plugin_name}】音源切换 → 网易云：{name}（{best.get('singer')}）")
        return best

    def _resolve_url(self, song: dict) -> Optional[str]:
        """取歌曲外部直链。只走网易云，绝不使用酷我（kw）接口"""
        source = (song.get("source") or "").lower()
        if source == "kw":
            logger.warning(f"【{self.plugin_name}】拒绝使用酷我音源：{song.get('name')}")
            return None
        songmid = str(song.get("songmid") or "")
        if not songmid:
            return None
        order = ([source] if source in URL_TEMPLATES else []) + [x for x in URL_SOURCE_ORDER if x != source]
        for src in order:
            for tpl in URL_TEMPLATES.get(src) or []:
                url = tpl.replace("{mid}", songmid)
                try:
                    # stream=True：只校验响应头，不把整个音频拉下来
                    r = requests.get(url, headers=self._headers(), timeout=25,
                                     allow_redirects=True, stream=True)
                    ct = (r.headers.get("Content-Type") or "").lower()
                    final = str(r.url)
                    r.close()
                    if r.status_code == 200 and "audio" in ct:
                        logger.info(f"【{self.plugin_name}】直链就绪（{src}）：{final[:90]}")
                        return final
                    logger.debug(f"【{self.plugin_name}】直链不可用 {src} {songmid}: HTTP {r.status_code} {ct[:24]}")
                except Exception as e:
                    logger.debug(f"【{self.plugin_name}】取直链异常 {url[:60]}: {e}")
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
        source = (song.get("source") or "").lower()
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

        # 非网易云的一律先换成网易云版本（不走酷我）
        song = self._switch_to_netease(song)
        source = (song.get("source") or "").lower()
        songmid = str(song.get("songmid") or "")

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
            "sourceName": source or "wy",
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
            # 始终使用最新实例（reload 后配置才会生效，且不会出现多线程并行）
            inst = LxMusicDownloader._inst or self
            try:
                if not (inst._enabled and inst._music_sub):
                    time.sleep(60)
                    continue
                inst.music_subscribe_check()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】音乐订阅检查异常：{e}")
            # 关键词订阅较重，每 24 轮一次
            try:
                if inst._enabled and inst._subscribes.strip() and keyword_tick % 24 == 0:
                    inst.subscribe_check()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】关键词订阅异常：{e}")
            # 每轮顺手清理一次：过小的文件 + 清空 cache/_open
            try:
                inst.clean_small_files()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】定时清理小文件异常：{e}")
            try:
                inst.clear_open_cache()
            except Exception as e:
                logger.error(f"【{self.plugin_name}】定时清空 cache/_open 异常：{e}")
            keyword_tick += 1
            _mins = max(1, getattr(inst, "_interval", 30))
            logger.info(f"【{self.plugin_name}】本轮完成，{_mins} 分钟后再次轮询")
            time.sleep(_mins * 60)

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


    def _mb_artist(self, mbid: str) -> str:
        """用 MusicBrainz MBID 反查专辑艺术家，失败返回空串"""
        if not mbid:
            return ""
        try:
            r = requests.get(
                f"https://musicbrainz.org/ws/2/release-group/{mbid}",
                params={"fmt": "json", "inc": "artist-credits"},
                headers={"User-Agent": "MoviePilot-LxMusic/2.0 (https://github.com/meeryang)"},
                timeout=20,
            )
            if r.status_code != 200:
                return ""
            names = []
            for c in ((r.json() or {}).get("artist-credit") or []):
                nm = (c.get("artist") or {}).get("name")
                if nm:
                    names.append(nm)
            artist = "/".join(names)
            if artist:
                logger.info(f"【LX音乐下载】MusicBrainz 反查艺术家：{mbid[:8]}… → {artist}")
            return artist
        except Exception as e:
            logger.warning(f"【LX音乐下载】MusicBrainz 反查失败：{e}")
            return ""

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

    # 这些关键词代表不是原曲，直接跳过
    _BAD_WORDS = [
        "伴奏", "ktv", "instrumental", "纯音乐", "off vocal", "offvocal",
        "カラオケ", "karaoke", "piano version", "钢琴版", "remix", "混音",
        "翻自", "cover by", "live版", "现场版", "demo",
    ]

    def _is_real_track(self, name: str) -> bool:
        """判断曲名是否像正片曲目（过滤伴奏/KTV/钢琴版等）"""
        n = (name or "").lower().replace(" ", "")
        for w in self._BAD_WORDS:
            if w.replace(" ", "") in n:
                return False
        return True

    def _album_match(self, cand: str, want: str) -> bool:
        """专辑名匹配：先精确，再容忍包含关系（订阅名可能带歌手）"""
        a, b = self._norm(cand or ""), self._norm(want or "")
        if not a or not b:
            return False
        if a == b:
            return True
        return a in b or b in a

    def album_songs(self, album_id: str, source: str) -> Optional[dict]:
        """进专辑：调 /api/music/albumSongs 拿整张曲目
        返回 {list:[songInfo...], total, name, artistName, source}，失败返回 None
        """
        if not album_id:
            return None
        try:
            r = requests.get(
                f"{self._server_url}/api/music/albumSongs",
                params={"id": str(album_id), "source": source, "token": self._token},
                headers=self._headers(), timeout=45,
            )
            if r.status_code != 200:
                logger.warning(f"【{self.plugin_name}】albumSongs({source}/{album_id}) HTTP {r.status_code}: {r.text[:80]}")
                return None
            d = r.json()
            if not isinstance(d, dict) or not (d.get("list") or []):
                return None
            # 剔除酷我来源 + 伴奏/纯音乐版本
            d["list"] = [x for x in (d.get("list") or [])
                         if (x.get("source") or source).lower() != "kw"
                         and self._is_real_track(x.get("name") or "")]
            return d if d["list"] else None
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】albumSongs 异常：{e}")
            return None

    def find_album_via_lx(self, album_name: str, artist: str = "",
                          expect_tracks: Optional[int] = None):
        """按前端真实流程：先搜专辑名 → 再进专辑拿曲目。
        返回 (source, album_id, 专辑详情 dict) 或 (None, None, None)
        """
        if not album_name:
            return None, None, None
        for src in SEARCH_SOURCES:
            kw = f"{album_name} {artist}".strip()
            if artist and self._norm(artist) in self._norm(album_name):
                kw = album_name
            songs = self.search(kw, limit=20, source=src)
            if not songs:
                logger.info(f"【{self.plugin_name}】{src} 搜「{kw}」无结果")
                continue
            # 从歌曲结果里找 albumName 匹配的专辑
            seen = set()
            for s in songs:
                if not self._album_match(s.get("albumName") or "", album_name):
                    continue
                if artist and not self._singer_match(s.get("singer") or "", artist):
                    continue
                aid = s.get("albumId") or s.get("albumMid")
                if not aid or str(aid) in seen:
                    continue
                seen.add(str(aid))
                detail = self.album_songs(str(aid), src)
                if not detail:
                    continue
                total = detail.get("total") or len(detail.get("list") or [])
                if not self._tracks_ok(detail["list"], expect_tracks):
                    logger.info(f"【{self.plugin_name}】{src}/{aid} 曲目数 {total} 与预期 {expect_tracks} 不符，继续找")
                    continue
                logger.info(f"【{self.plugin_name}】进专辑成功：{detail.get('name')} - "
                            f"{detail.get('artistName')}（{src}，{total} 首）")
                return src, str(aid), detail
        return None, None, None

    def _find_song_info(self, track: dict) -> Optional[dict]:
        """按曲名+歌手在 kg/tx 找可下载的 songInfo。
        严格校验同名同歌手，且 source 不得为 kw（酷我）。
        """
        name = track.get("name") or ""
        artist = track.get("artist") or ""
        kw = f"{name} {artist}".strip()
        if not kw:
            return None
        for src in SEARCH_SOURCES:
            for s in self.search(kw, limit=6, source=src):
                if not s.get("songmid"):
                    continue
                if (s.get("source") or "").lower() == "kw":
                    continue
                if self._norm(s.get("name") or "") != self._norm(name):
                    continue
                if not self._is_real_track(s.get("name") or ""):
                    continue
                if not self._singer_match(s.get("singer") or "", artist):
                    continue
                return s
        return None

    def download_album(self, album_name: str, expect_tracks: Optional[int] = None,
                       artist: str = "") -> Tuple[bool, str]:
        """下载整张专辑，走 LX音乐 前端的真实流程：
        ① 调用接口搜索专辑名称
        ② 进入专辑（拿整张曲目）
        ③ 全部提交到服务端缓存队列
        音源仅用 kg/tx/wy，不使用酷我。
        """
        err = self._check_conf()
        if err:
            return False, err
        if not album_name:
            return False, "专辑名为空"

        # ① + ②：搜专辑名 → 进专辑拿曲目
        src_tag, album_id, detail = self.find_album_via_lx(album_name, artist, expect_tracks)

        if detail:
            songs = detail.get("list") or []
            real_name = detail.get("name") or album_name
            real_artist = detail.get("artistName") or artist
            logger.info(f"【{self.plugin_name}】专辑「{real_name}」- {real_artist}"
                        f"（{src_tag}/{album_id}）共 {len(songs)} 首，开始缓存到服务器")
            # ③：整张一次性提交队列
            n, qmsg = self.queue_download_with_fallback(songs, self._quality)
            # 搬进音乐库 /server/music（本地音乐可见），并只保留最高音质
            moved, lowq, pmsg = self.promote_to_music()
            cnum, cmsg = self.clean_small_files()
            onum, omsg = self.clear_open_cache()
            msg = (f"专辑「{real_name}」（{src_tag}）共 {len(songs)} 首："
                   f"{qmsg}；{pmsg}；{cmsg}；{omsg}")
            logger.info(f"【{self.plugin_name}】{'✅' if (n or moved) else '⚠️'} {msg}")
            return (n > 0 or moved > 0), msg

        # 兜底：lxserver 专辑接口没命中时，回退到网易云/QQ 专辑 API + 逐曲匹配
        logger.info(f"【{self.plugin_name}】lxserver 专辑接口未命中「{album_name}」，回退逐曲匹配")
        src2, real2, tracks = self.find_album(album_name, expect_tracks)
        if not tracks:
            return False, f"专辑「{album_name}」在 kg/tx/wy 均未找到"
        picked, missing = [], []
        for t in tracks:
            song = self._find_song_info(t)
            if song:
                picked.append(song)
            else:
                missing.append(f"{t['name']} - {t['artist']}")
            time.sleep(0.4)
        if not picked:
            return False, f"专辑「{real2}」共 {len(tracks)} 首，全部无可用音源"
        n, qmsg = self.queue_download_with_fallback(picked, self._quality)
        moved, lowq, pmsg = self.promote_to_music()
        cnum, cmsg = self.clean_small_files()
        onum, omsg = self.clear_open_cache()
        msg = (f"专辑「{real2}」（{src2}·逐曲）共 {len(tracks)} 首："
               f"{qmsg}；{pmsg}；{cmsg}；{omsg}，未匹配 {len(missing)} 首")
        logger.info(f"【{self.plugin_name}】{'✅' if n else '⚠️'} {msg}")
        return (n > 0), msg

    def queue_download(self, songs: List[dict], quality: str) -> Tuple[int, str]:
        """把一批歌曲一次性提交到 lxserver 下载队列。
        直链由服务端 resolver 自行解析（六音音源支持 kg/tx），插件不需要也不使用酷我。
        返回 (提交成功的任务数, 说明)
        """
        err = self._check_conf()
        if err:
            return 0, err
        # 已在队列里的 songKey，避免重复提交
        busy = set(self._queue_status_map().keys())
        tasks, skipped = [], 0
        for s in songs:
            if not s or not s.get("songmid"):
                continue
            src = (s.get("source") or "").lower()
            if src == "kw":          # 硬性排除酷我
                continue
            key = f"{src}_{s.get('songmid')}_{quality}"
            if key in busy:          # 同一首同一音质已在队列/已缓存，跳过
                skipped += 1
                continue
            tasks.append({
                "username": (getattr(self, "_lx_user", "") or "admin"),
                "songInfo": s,
                "quality": quality,
                "requestedQuality": quality,
                "cacheLyric": True,
                "embedLyric": True,
            })
        if not tasks:
            return 0, f"没有可提交的曲目（已跳过 {skipped} 首重复）"
        try:
            r = requests.post(
                f"{self._server_url}/api/music/cache/queue",
                params={"token": self._token},
                json={"tasks": tasks, "concurrency": QUEUE_CONCURRENCY},
                headers={"Content-Type": "application/json", **self._headers()},
                timeout=120,
            )
            if r.status_code != 200:
                return 0, f"提交队列失败 HTTP {r.status_code}: {r.text[:100]}"
            data = (r.json() or {}).get("data") or []
            logger.info(f"【{self.plugin_name}】已提交 {len(data)} 首到服务端下载队列（音质 {quality}）")
            return len(data), f"已提交 {len(data)} 首到服务端缓存队列（音质 {quality}）"
        except Exception as e:
            return 0, f"提交队列异常：{e}"

    def _queue_status_map(self) -> dict:
        """返回 {songKey: status}，songKey 形如 kg_370383681_320k"""
        try:
            r = requests.get(f"{self._server_url}/api/music/cache/queue",
                             params={"token": self._token}, headers=self._headers(), timeout=30)
            if r.status_code != 200:
                return {}
            out = {}
            for t in ((r.json() or {}).get("data") or []):
                key = t.get("songKey") or ""
                if key:
                    out[key] = (t.get("status") or "").lower()
            return out
        except Exception:
            return {}

    def _cache_snapshot(self) -> set:
        """返回已落地的 {(归一化歌名, 归一化歌手, 音质)}，缓存目录和音乐库都算"""
        snap = set()
        for folder in ("cache", "music"):
            for it in self.cache_list(folder):
                snap.add((self._norm(it.get("name") or ""),
                          self._norm(it.get("singer") or ""),
                          str(it.get("quality") or "").lower()))
        return snap

    def _cache_exists(self, song: dict, quality: str, snap: set = None) -> bool:
        """判断这首歌该音质是否已落地（用文件列表，比 cache/check 可靠）"""
        key = (self._norm(song.get("name") or ""),
               self._norm(song.get("singer") or ""),
               str(quality or "").lower())
        if snap is None:
            snap = self._cache_snapshot()
        return key in snap

    # 音质高低排序（越大越高）
    _QUAL_RANK = {"128k": 1, "192k": 2, "320k": 3, "flac": 4,
                  "flac24bit": 5, "hires": 6, "atmos": 7, "master": 8}

    def cache_list(self, folder: str = "cache") -> List[dict]:
        """列出服务端缓存目录里的文件"""
        try:
            r = requests.get(f"{self._server_url}/api/music/cache/list",
                             params={"token": self._token}, headers=self._headers(), timeout=40)
            if r.status_code != 200:
                return []
            items = (r.json() or {}).get("data") or []
            return [x for x in items if (x.get("folder") or "cache") == folder]
        except Exception as e:
            logger.warning(f"【{self.plugin_name}】读缓存列表失败：{e}")
            return []

    def clear_open_cache(self) -> Tuple[int, str]:
        """清空 cache/_open（公开用户缓存目录）。
        只删音频/歌词等实体文件，保留 cache_index.json 让服务端重建索引。
        """
        if not getattr(self, "_clear_open_cache", True):
            return 0, "未启用清理缓存 _open"
        base = (getattr(self, "_local_music_dir", "") or "/media/downloads/music").rstrip("/")
        d = f"{base}/cache/_open"
        try:
            if not os.path.isdir(d):
                return 0, "cache/_open 目录不存在"
            removed, failed, freed = 0, 0, 0
            for fn in os.listdir(d):
                if fn == "cache_index.json":      # 保留索引文件
                    continue
                p = os.path.join(d, fn)
                if not os.path.isfile(p):
                    continue
                try:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    removed += 1
                    freed += sz
                except Exception as e:
                    failed += 1
                    logger.warning(f"【{self.plugin_name}】删除 {fn} 失败：{e}")
            if removed:
                # 让服务端重建索引，避免索引里残留已删条目
                try:
                    requests.post(f"{self._server_url}/api/music/cache/sync",
                                  params={"token": self._token}, json={},
                                  headers={"Content-Type": "application/json", **self._headers()},
                                  timeout=180)
                except Exception:
                    pass
                mb = freed / 1024 / 1024
                logger.info(f"【{self.plugin_name}】已清空 cache/_open：{removed} 个文件，释放 {mb:.1f}MB")
                return removed, f"清空 cache/_open：删 {removed} 个文件，释放 {mb:.1f}MB"
            return 0, "cache/_open 已是空的" + (f"（{failed} 个删除失败）" if failed else "")
        except Exception as e:
            logger.error(f"【{self.plugin_name}】清空 cache/_open 异常：{e}")
            return 0, f"清空 cache/_open 失败：{e}"

    def clean_small_files(self) -> Tuple[int, str]:
        """删除音乐库/缓存目录里小于阈值的文件（默认 <500KB）。
        优先用服务端 API 删除（会同步索引）；没有管理员密码时退化为直接删文件。
        返回 (删除数量, 说明)
        """
        try:
            kb = int(getattr(self, "_min_size_kb", 500) or 0)
        except Exception:
            kb = 500
        if kb <= 0:
            return 0, "未启用（阈值 0）"
        limit = kb * 1024

        items = []
        for folder in ("music", "cache"):
            items.extend(self.cache_list(folder))
        small = [x for x in items if int(x.get("size") or 0) < limit]
        if not small:
            return 0, f"没有小于 {kb}KB 的文件"

        names = [x.get("filename") for x in small if x.get("filename")]
        logger.info(f"【{self.plugin_name}】发现 {len(names)} 个小于 {kb}KB 的文件，开始清理")

        # 方式一：服务端 API（需要管理员密码，会同步索引）
        if self._lx_admin_pwd:
            try:
                r = requests.post(
                    f"{self._server_url}/api/music/cache/remove",
                    params={"token": self._token},
                    json={"filenames": names},
                    headers={"Content-Type": "application/json",
                             "x-frontend-auth": self._lx_admin_pwd, **self._headers()},
                    timeout=180,
                )
                if r.status_code == 200:
                    logger.info(f"【{self.plugin_name}】已通过 API 清理 {len(names)} 个小文件（<{kb}KB）")
                    return len(names), f"已删除 {len(names)} 个小于 {kb}KB 的文件"
                logger.warning(f"【{self.plugin_name}】API 清理失败 HTTP {r.status_code}，改用直接删除")
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】API 清理异常 {e}，改用直接删除")

        # 方式二：直接删文件（无需密码），删完触发索引同步
        base = (getattr(self, "_local_music_dir", "") or "/media/downloads/music").rstrip("/")
        user = (getattr(self, "_lx_user", "") or "admin").strip()
        removed = 0
        for x in small:
            fn = x.get("filename")
            folder = x.get("folder") or "cache"
            if not fn:
                continue
            p = f"{base}/{folder}/{user}/{fn}"
            try:
                import os as _os
                if _os.path.exists(p):
                    _os.remove(p)
                    removed += 1
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】删除失败 {fn}: {e}")
        if removed:
            try:  # 让服务端重建索引
                requests.post(f"{self._server_url}/api/music/cache/sync",
                              params={"token": self._token}, json={},
                              headers={"Content-Type": "application/json", **self._headers()},
                              timeout=180)
            except Exception:
                pass
            logger.info(f"【{self.plugin_name}】已直接删除 {removed} 个小文件（<{kb}KB）并同步索引")
            return removed, f"已删除 {removed} 个小于 {kb}KB 的文件"
        return 0, f"发现 {len(names)} 个小文件但删除失败（路径可能不匹配）"

    def promote_to_music(self) -> Tuple[int, int, str]:
        """把缓存目录里的歌搬进音乐库（/server/music，即「本地音乐」），
        同一首歌只保留最高音质，低音质版本不搬。
        返回 (搬移成功的数量, 被跳过的低音质数量, 说明)
        """
        items = self.cache_list("cache")
        if not items:
            return 0, 0, "缓存目录没有待搬移的文件"

        # 按 歌名+歌手 分组
        groups = {}
        for it in items:
            fn = it.get("filename")
            if not fn:
                continue
            key = (self._norm(it.get("name") or ""), self._norm(it.get("singer") or ""))
            groups.setdefault(key, []).append(it)

        to_move, low_quality = [], []
        for key, lst in groups.items():
            lst.sort(key=lambda x: self._QUAL_RANK.get(str(x.get("quality") or "").lower(), 0),
                     reverse=True)
            to_move.append(lst[0]["filename"])
            for x in lst[1:]:
                low_quality.append(x["filename"])

        moved = 0
        try:
            r = requests.post(f"{self._server_url}/api/music/cache/move",
                              params={"token": self._token},
                              json={"filenames": to_move},
                              headers={"Content-Type": "application/json", **self._headers()},
                              timeout=180)
            if r.status_code == 200:
                moved = int((r.json() or {}).get("successCount") or 0)
                logger.info(f"【{self.plugin_name}】已搬移 {moved} 首到音乐库 /server/music")
            else:
                logger.warning(f"【{self.plugin_name}】搬移失败 HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.error(f"【{self.plugin_name}】搬移异常：{e}")

        # 低音质版本：有管理员密码才删，否则留在缓存不动
        if low_quality and self._lx_admin_pwd:
            try:
                requests.post(f"{self._server_url}/api/music/cache/remove",
                              params={"token": self._token},
                              json={"filenames": low_quality},
                              headers={"Content-Type": "application/json",
                                       "x-frontend-auth": self._lx_admin_pwd,
                                       **self._headers()},
                              timeout=120)
                logger.info(f"【{self.plugin_name}】已清理 {len(low_quality)} 个低音质重复文件")
            except Exception as e:
                logger.warning(f"【{self.plugin_name}】清理低音质失败：{e}")

        msg = f"搬移 {moved} 首到音乐库"
        if low_quality:
            msg += f"，跳过 {len(low_quality)} 个低音质重复" + ("（已清理）" if self._lx_admin_pwd else "（留缓存）")
        return moved, len(low_quality), msg

    def queue_download_with_fallback(self, songs: List[dict],
                                     quality: Optional[str] = None) -> Tuple[int, str]:
        """整批提交队列；某档音质失败的任务自动降到下一档重提。
        返回 (最终成功数, 说明)
        """
        base_q = (quality or self._quality or "320k").lower()
        order = [base_q] + [q for q in QUALITY_FALLBACK if q != base_q]
        remaining = list(songs)
        notes, ok_total = [], 0

        for qi, q in enumerate(order):
            if not remaining:
                break
            n, msg = self.queue_download(remaining, q)
            if n == 0:
                notes.append(f"{q}: {msg}")
                continue
            notes.append(f"{q}: 提交 {n} 首")
            # 等这批跑完
            time.sleep(QUEUE_WAIT_SEC)
            # 关键：队列状态常报 error 但文件其实已落地，以实际文件列表为准
            smap = self._queue_status_map()
            snap = self._cache_snapshot()
            still = []
            for s in remaining:
                if self._cache_exists(s, q, snap):
                    ok_total += 1           # 真落地了
                    continue
                key = f"{(s.get('source') or '').lower()}_{s.get('songmid')}_{q}"
                st = smap.get(key, "")
                if st in ("finished", "exists"):
                    ok_total += 1
                else:
                    still.append(s)          # 确实没有 → 换下一档再试
            remaining = still
            if remaining:
                logger.info(f"【{self.plugin_name}】{q} 有 {len(remaining)} 首未成功，准备降档重试")

        tail = (f"，成功 {ok_total} 首，其余 {len(remaining)} 首无可用源"
                if remaining else f"，{ok_total} 首全部成功")
        return ok_total, " | ".join(notes) + tail

    def queue_progress(self) -> dict:
        """读取服务端队列进度，返回 {finished, error, downloading, total}"""
        try:
            r = requests.get(f"{self._server_url}/api/music/cache/queue",
                             params={"token": self._token}, headers=self._headers(), timeout=30)
            if r.status_code != 200:
                return {}
            tasks = (r.json() or {}).get("data") or []
            cnt = {"finished": 0, "error": 0, "downloading": 0, "waiting": 0, "total": len(tasks)}
            for t in tasks:
                st = (t.get("status") or "").lower()
                if st in ("finished", "exists"):
                    cnt["finished"] += 1
                elif st in ("error", "failed"):
                    cnt["error"] += 1
                elif st in ("downloading",):
                    cnt["downloading"] += 1
                else:
                    cnt["waiting"] += 1
            return cnt
        except Exception:
            return {}

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
            albums.append({
                "id": int(sid), "name": nm,
                "total_tracks": getattr(s, "total_tracks", None),
                "media_source": getattr(s, "media_source", None),
                "media_id": getattr(s, "media_id", None),
                "year": getattr(s, "year", None),
            })

        state = self.get_data("music_subs") or {}
        baseline = state.get("baseline")
        processed = state.get("processed") or {}

        # 首次运行（或基线被清空）只建基线，不回溯下载历史订阅
        if not baseline:
            self.save_data("music_subs", {"baseline": [a["id"] for a in albums],
                                          "processed": processed})
            logger.info(f"【LX音乐下载】音乐专辑订阅基线已建立：{len(albums)} 条（历史订阅不回溯）")
            return

        base_set = set(baseline)
        targets = [a for a in albums
                   if a["id"] not in base_set and str(a["id"]) not in processed]
        if not targets:
            return

        logger.info(f"【LX音乐下载】发现 {len(targets)} 个新增音乐专辑订阅")
        for a in targets:
            # 用 MBID 反查艺术家，拼进关键词大幅提高命中率
            artist = ""
            if (a.get("media_source") or "").lower() == "musicbrainz" and a.get("media_id"):
                artist = self._mb_artist(a["media_id"])
                time.sleep(1.2)  # MusicBrainz 限速 1 req/s
            try:
                ok, msg = self.download_album(a["name"], a.get("total_tracks"), artist=artist)
            except Exception as e:
                ok, msg = False, f"异常：{e}"
            processed[str(a["id"])] = {
                "name": a["name"], "artist": artist, "keyword": keyword,
                "ok": ok, "msg": msg,
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

    def api_clear_open(self):
        """手动清空 cache/_open"""
        try:
            n, msg = self.clear_open_cache()
            return {"success": True, "message": msg, "removed": n}
        except Exception as e:
            return {"success": False, "message": f"清理失败：{e}"}

    def api_clean(self):
        """手动清理小于阈值的文件"""
        try:
            n, msg = self.clean_small_files()
            return {"success": True, "message": msg, "removed": n,
                    "threshold_kb": getattr(self, "_min_size_kb", 500)}
        except Exception as e:
            logger.error(f"【{self.plugin_name}】清理异常：{e}")
            return {"success": False, "message": f"清理失败：{e}"}

    def api_promote(self):
        """手动把缓存搬进音乐库，并按歌曲去重只留最高音质"""
        try:
            moved, lowq, msg = self.promote_to_music()
            return {"success": True, "message": msg, "moved": moved, "low_quality_skipped": lowq}
        except Exception as e:
            logger.error(f"【{self.plugin_name}】搬移异常：{e}")
            return {"success": False, "message": f"搬移失败：{e}"}

    def api_run_once(self):
        """「运行一次」：立即执行一次音乐订阅轮询"""
        try:
            before = (self.get_data("music_subs") or {}).get("processed") or {}
            self.music_subscribe_check()
            after = (self.get_data("music_subs") or {}).get("processed") or {}
            new_keys = [k for k in after if k not in before]
            if new_keys:
                details = []
                for k in new_keys:
                    item = after.get(k) or {}
                    details.append(f"{item.get('name')}：{item.get('msg')}")
                msg = f"已处理 {len(new_keys)} 个新专辑订阅 → " + "；".join(details)
                logger.info(f"【{self.plugin_name}】手动轮询完成：{msg}")
                return {"success": True, "message": msg[:600], "new": len(new_keys)}
            exist = (self.get_data("music_subs") or {}).get("baseline")
            tip = "（首次运行已建立基线，之后的新订阅才会自动下载）" if not exist else ""
            logger.info(f"【{self.plugin_name}】手动轮询完成：没有新订阅{tip}")
            return {"success": True, "message": f"轮询完成，没有新的音乐专辑订阅{tip}", "new": 0}
        except Exception as e:
            logger.error(f"【{self.plugin_name}】手动轮询异常：{e}")
            return {"success": False, "message": f"运行失败：{e}"}

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
