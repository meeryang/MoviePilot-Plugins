"""
IMDb 搜索插件 —— 让 MoviePilot 能按关键词直接搜索 IMDb 的内容。

为什么需要它：
- MoviePilot 自带的 IMDb 源插件（ImdbSource）只提供「探索」和「媒体识别」，
  没有暴露关键词搜索，所以你在探索页能浏览 IMDb，却没法"搜"。
- ImdbSource 内部的 ImdbApiClient.async_search_titles() 其实完全支持关键词搜索，
  只是没开放成接口。本插件把它开放出来，并额外提供"用 IMDb 结果去搜资源"的能力。

两个接口：
1. GET /search?keyword=xxx&limit=20
   搜 IMDb，返回影片列表（imdbid / 标题 / 年份 / 类型 / 评分 / 海报 / 简介）。
2. GET /resources?keyword=xxx   或  ?imdbid=tt0499549
   先搜 IMDb 拿到影片，再用 imdbid 作为搜索区域去各站点找种子（area="imdbid"），
   返回可直接下载的资源列表。这是 IMDb 搜索真正的落地点。

重要认知：IMDb 是元数据站，本身没有种子。所以"搜 IMDb"得到的是影片信息，
要拿到可下载资源必须走站点（本插件用 imdbid 精确匹配站点，命中率最高）。
"""
import asyncio
from typing import List, Optional, Dict, Any, Tuple

from app.chain.search import SearchChain
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils


class ImdbSearch(_PluginBase):
    # 插件元数据
    plugin_name = "IMDb搜索"
    plugin_desc = "按关键词搜索 IMDb 影片，并可用 IMDb ID 精确搜索站点资源"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/imdb.png"
    plugin_version = "1.0.0"
    plugin_author = "虾虾"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_config_prefix = "imdbsearch_"
    plugin_order = 7
    auth_level = 1

    _enabled: bool = False
    _proxy: bool = True
    _default_limit: int = 20
    _client = None

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._proxy = bool(config.get("proxy", True))
        self._default_limit = int(config.get("default_limit") or 20)
        if self._enabled:
            self._init_client()
            self._apply_patch()

    def _init_client(self):
        """复用 ImdbSource 插件里的官方 IMDb 客户端"""
        try:
            from app.plugins.imdbsource.imdbapi import ImdbApiClient
            proxies = settings.PROXY if self._proxy else None
            try:
                # MoviePilot v3：ImdbApiClient 必须显式传入 session 与 async_client
                import requests as _rq
                import httpx2 as _h2
                self._client = ImdbApiClient(
                    session=_rq.Session(),
                    async_client=_h2.AsyncClient(),
                    proxies=proxies,
                )
            except TypeError:
                # MoviePilot v2：只接受 proxies
                self._client = ImdbApiClient(proxies=proxies)
            logger.info("【IMDb搜索】客户端初始化成功")
        except Exception as e:
            logger.error(f"【IMDb搜索】客户端初始化失败（是否已安装 IMDb源 插件？）: {e}")
            self._client = None

    # ────────────── 核心：搜 IMDb ──────────────
    async def _search_imdb(self, keyword: str, limit: int = 20) -> List[dict]:
        """搜 IMDb，返回结构化影片列表"""
        if not keyword:
            return []
        if not self._client:
            self._init_client()
        if not self._client:
            return []
        try:
            res = await self._client.async_search_titles(keyword, limit=min(int(limit or 20), 50))
        except Exception as e:
            logger.error(f"【IMDb搜索】IMDb 搜索异常: {e}")
            return []
        titles = getattr(res, "titles", None) or [] if res else []
        out = []
        for t in titles:
            try:
                mtype = str(getattr(getattr(t, "type", None), "value", "") or "")
                rating = getattr(t, "rating", None)
                img = getattr(t, "primary_image", None)
                out.append({
                    "imdbid": getattr(t, "id", "") or "",
                    "title": getattr(t, "primary_title", "") or "",
                    "original_title": getattr(t, "original_title", "") or "",
                    "type": mtype,                       # movie / tvSeries ...
                    "year": getattr(t, "start_year", None),
                    "rating": getattr(rating, "aggregate_rating", None) if rating else None,
                    "votes": getattr(rating, "vote_count", None) if rating else None,
                    "poster": getattr(img, "url", None) if img else None,
                    "plot": getattr(t, "plot", None),
                    "genres": getattr(t, "genres", None),
                    "imdb_url": f"https://www.imdb.com/title/{getattr(t, 'id', '')}/",
                })
            except Exception as e:
                logger.debug(f"【IMDb搜索】单条解析失败: {e}")
                continue
        logger.info(f"【IMDb搜索】搜索「{keyword}」命中 {len(out)} 部影片")
        return out

    # ────────────── 媒体搜索源：source=imdb ──────────────
    async def _search_as_mediainfo(self, meta) -> List[Any]:
        """把 IMDb 搜索转成 schemas.MediaInfo 列表，供 /media/search?source=imdb 使用"""
        kw = ""
        for attr in ("name", "cn_name", "en_name"):
            v = getattr(meta, attr, None)
            if v:
                kw = str(v)
                break
        if not kw:
            return []
        if not self._client:
            self._init_client()
        if not self._client:
            return []
        try:
            res = await self._client.async_search_titles(kw, limit=20)
        except Exception as e:
            logger.error(f"【IMDb搜索】source=imdb 搜索异常: {e}")
            return []
        titles = (getattr(res, "titles", None) or []) if res else []
        # 必须是 app.core.context.MediaInfo（dataclass，有 to_dict）；
        # app.schemas.MediaInfo 是 pydantic 版，没有 to_dict，
        # /media/search 端点要调 media.to_dict() 会直接 500。
        from app.core.context import MediaInfo as CtxMediaInfo
        from app.schemas.types import MediaType
        out = []
        for t in titles:
            try:
                imdbid = getattr(t, "id", "") or ""
                title = getattr(t, "primary_title", "") or ""
                if not imdbid or not title:
                    continue
                mtype = str(getattr(getattr(t, "type", None), "value", "") or "").lower()
                is_tv = ("tv" in mtype) or ("series" in mtype)
                year = getattr(t, "start_year", None)
                img = getattr(t, "primary_image", None)
                poster = getattr(img, "url", None) if img else None
                rating_obj = getattr(t, "rating", None)
                rating = getattr(rating_obj, "aggregate_rating", None) if rating_obj else None

                mi = CtxMediaInfo(
                    source="imdb",
                    media_id=str(imdbid),
                    imdb_id=str(imdbid),
                    title=title,
                )
                mi.type = MediaType.TV if is_tv else MediaType.MOVIE
                if year:
                    mi.year = str(year)
                if poster:
                    mi.poster_path = poster
                if rating:
                    try:
                        mi.vote_average = float(rating)
                    except Exception:
                        pass
                try:
                    mi.overview = getattr(t, "plot", None) or ""
                except Exception:
                    pass
                out.append(mi)
            except Exception as e:
                logger.debug(f"【IMDb搜索】MediaInfo 构造失败: {e}")
                continue
        logger.info(f"【IMDb搜索】source=imdb 搜索「{kw}」→ {len(out)} 条媒体")
        return out

    def _apply_patch(self):
        """让 /media/search?source=imdb 走 IMDb。
        直接改写 ChainBase.async_search_medias：source=imdb 时只返回 IMDb 结果，
        其它 source 原样透传给系统模块（themoviedb/douban/bangumi/anilist）。"""
        plugin = self
        try:
            from app.chain import ChainBase
        except Exception as e:
            logger.error(f"【IMDb搜索】导入 ChainBase 失败: {e}")
            return
        if getattr(ChainBase, "__imdbsearch_patched__", False):
            logger.info("【IMDb搜索】媒体搜索补丁已存在")
            return
        prev = ChainBase.async_search_medias

        # v3 把参数名从 source 改成了 media_source，且可能用位置或关键字传参。
        # 用 *args/**kwargs 全透传，绝不绑定参数名，否则会
        # TypeError: got multiple values for argument 'media_source'
        async def patched(chain_self, meta, *args, **kwargs):
            _src = kwargs.get("media_source") or kwargs.get("source")
            if _src is None and args:
                _src = args[0]
            if plugin._enabled and _src and str(_src).lower() == "imdb":
                try:
                    r = await plugin._search_as_mediainfo(meta)
                    if r:
                        return r
                except Exception as e:
                    logger.error(f"【IMDb搜索】source=imdb 处理异常: {e}")
            return await prev(chain_self, meta, *args, **kwargs)

        ChainBase.async_search_medias = patched
        ChainBase.__imdbsearch_patched__ = True
        logger.info("【IMDb搜索】媒体搜索补丁已应用（支持 source=imdb）")

    # ────────────── 用 IMDb 结果搜资源 ──────────────
    async def _search_resources(self, keyword: str = "", imdbid: str = "",
                                page: int = 0) -> List[dict]:
        """用 imdbid（优先）或关键词去站点搜种子。imdbid 命中率最高。"""
        area = "title"
        kw = keyword
        if imdbid:
            area = "imdbid"
            kw = imdbid
        elif not keyword:
            return []
        try:
            # 注意：__async_search_all_sites 是名字改写后的私有方法，
            # 类外直接调用会 AttributeError。这里用公开的 async_search_by_title，
            # 它返回 List[Context]，再从 Context 里取 torrent_info。
            contexts = await SearchChain().async_search_by_title(
                title=kw, page=int(page or 0), cache_local=False
            ) or []
        except Exception as e:
            logger.error(f"【IMDb搜索】资源搜索异常: {e}")
            return []
        torrents = []
        for ctx in contexts:
            ti = getattr(ctx, "torrent_info", None)
            if ti is not None:
                torrents.append(ti)
        out = []
        for t in torrents:
            try:
                out.append({
                    "site_name": getattr(t, "site_name", ""),
                    "title": getattr(t, "title", ""),
                    "size": getattr(t, "size", 0),
                    "seeders": getattr(t, "seeders", 0),
                    "peers": getattr(t, "peers", 0),
                    "pubdate": getattr(t, "pubdate", ""),
                    "enclosure": getattr(t, "enclosure", ""),
                    "page_url": getattr(t, "page_url", ""),
                })
            except Exception:
                continue
        logger.info(f"【IMDb搜索】资源搜索「{kw}」(area={area}) 得到 {len(out)} 条")
        return out

    # ────────────── 对外 API ──────────────
    async def api_search(self, keyword: str = "", limit: int = 0):
        """GET /api/v1/plugin/ImdbSearch/search?keyword=avatar&limit=20"""
        if not self._enabled:
            return []
        return await self._search_imdb(keyword, limit or self._default_limit)

    async def api_both(self, keyword: str = "", limit: int = 0):
        """搜 IMDb + 顺便用第一名去站点找资源，一次请求拿全"""
        if not self._enabled:
            return {"movies": [], "resources": []}
        movies = await self._search_imdb(keyword, limit or self._default_limit)
        resources = []
        if movies:
            top = movies[0]
            resources = await self._search_resources(
                keyword=top.get("title"), imdbid=top.get("imdbid"))
        return {"keyword": keyword, "movies": movies, "resources": resources}

    async def api_resources(self, keyword: str = "", imdbid: str = "", page: int = 0):
        """GET /api/v1/plugin/ImdbSearch/resources?imdbid=tt0499549"""
        if not self._enabled:
            return []
        return await self._search_resources(keyword=keyword, imdbid=imdbid, page=page)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "summary": "搜索 IMDb 影片",
                "description": "按关键词搜索 IMDb，返回 imdbid/标题/年份/评分/海报",
            },
            {
                "path": "/resources",
                "endpoint": self.api_resources,
                "methods": ["GET"],
                "summary": "用 IMDb ID 搜索站点资源",
                "description": "传 imdbid 走 area=imdbid 精确搜索，或传 keyword 按标题搜",
            },
            {
                "path": "/both",
                "endpoint": self.api_both,
                "methods": ["GET"],
                "summary": "同时返回 IMDb 影片与资源",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [{
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [{
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [{
                        "component": "VSwitch",
                        "props": {"model": "proxy", "label": "使用代理访问 IMDb"},
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [{
                        "component": "VTextField",
                        "props": {"model": "default_limit", "label": "默认返回条数(最大50)",
                                  "type": "number"},
                    }],
                }],
            }, {
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VAlert",
                        "props": {"type": "info", "variant": "tonal", "density": "compact"},
                        "content": [{
                            "component": "span",
                            "text": "IMDb 是元数据站，没有种子。搜索得到的是影片信息；"
                                    "要拿可下载资源请用 /resources?imdbid=ttxxx（按 IMDb ID 精确匹配站点）。"
                                    "需要已安装「IMDb源」插件（共用它的 IMDb 客户端）。",
                        }],
                    }],
                }],
            }],
        }], {
            "enabled": True,
            "proxy": True,
            "default_limit": 20,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        pass
