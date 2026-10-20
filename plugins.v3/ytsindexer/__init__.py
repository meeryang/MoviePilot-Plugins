"""
YTS 索引器插件 —— 让 MoviePilot 能搜索 YTS（yts.torrentsbay.org / yts.gg）电影资源。

站点说明：
- 用户指定的站点 https://yts.torrentsbay.org/ 当前被 Cloudflare 人机验证全站拦截
  （首页/API/RSS 全部 403 + "Just a moment..."），MoviePilot 的 RequestUtils 无法通过。
- 该站是 YTS 镜像；YTS 官方 API 在 https://yts.gg/api/v2/list_movies.json 可用，
  内容与其镜像站一致（同一批 YTS/YIFY 资源，约 7.7 万部电影）。
- 因此插件实际走 yts.gg 的公开 API，站点名仍标注为 YTS。
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlencode

from app.chain.search import SearchChain
from app.chain.torrents import TorrentsChain
from app.core.context import MediaInfo, TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils


class YtsIndexer(_PluginBase):
    # 插件元信息
    plugin_name = "YTS索引器"
    plugin_desc = "让 YTS（yts.torrentsbay.org / yts.gg）参与 MoviePilot 搜索与浏览"
    plugin_icon = "https://yts.gg/favicon.ico"
    plugin_version = "1.0.0"
    plugin_author = "Hermes"
    author_url = "https://github.com/"
    plugin_config_prefix = "ytsindexer_"
    plugin_order = 30
    auth_level = 1

    # 配置项
    _enabled: bool = False
    _proxy: bool = True
    _site_name: str = "YTS"
    _api_base: str = "https://yts.gg/api/v2/list_movies.json"
    _site_url: str = "https://yts.torrentsbay.org"
    _patch_search: bool = True
    _limit: int = 50

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled", self._enabled))
            self._proxy = bool(config.get("proxy", self._proxy))
            self._site_name = config.get("site_name") or self._site_name
            self._api_base = config.get("api_base") or self._api_base
            self._site_url = config.get("site_url") or self._site_url
            self._patch_search = bool(config.get("patch_search", self._patch_search))
            self._limit = int(config.get("limit") or self._limit)
        if self._enabled:
            self._apply_patch()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        return []

    def get_api(self) -> List[dict]:
        return [{
            "path": "/search",
            "endpoint": self.api_search,
            "methods": ["GET"],
            "summary": "搜索 YTS",
            "description": "按关键词搜索 YTS 电影资源",
        }]

    def get_form(self) -> Tuple[List[dict], dict]:
        return ([{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSwitch", "props": {"model": "proxy", "label": "使用代理"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VSwitch", "props": {"model": "patch_search", "label": "参与搜索链"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VTextField", "props": {"model": "site_name", "label": "站点名称"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{
                    "component": "VTextField", "props": {"model": "site_url", "label": "站点地址"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [{
                    "component": "VTextField", "props": {"model": "api_base", "label": "API 地址"}}]}]},
                {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{
                    "component": "VTextField", "props": {"model": "limit", "label": "单次返回上限"}}]}]},
            ],
        }], {
            "enabled": False,
            "proxy": True,
            "patch_search": True,
            "site_name": "YTS",
            "site_url": "https://yts.torrentsbay.org",
            "api_base": "https://yts.gg/api/v2/list_movies.json",
            "limit": 50,
        })

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        self._remove_patch()

    # ---------------- 核心：请求 + 解析 ----------------

    def _proxies(self):
        if not self._proxy:
            return None
        try:
            from app.core.config import settings
            p = getattr(settings, "PROXY_HOST", None)
            if p:
                return {"http": p, "https": p}
        except Exception:
            pass
        return {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}

    def _fetch(self, keyword: Optional[str] = None) -> list:
        params = {"limit": self._limit, "sort_by": "date_added"}
        if keyword:
            params["query_term"] = keyword
        url = f"{self._api_base}?{urlencode(params)}"
        try:
            # 注意：RequestUtils 没有 proxy 参数，只有 proxies
            res = RequestUtils(ua=self._UA, proxies=self._proxies(), timeout=25).get_res(url)
            if not res:
                logger.warning(f"【{self.plugin_name}】API 无响应: {url}")
                return []
            if res.status_code != 200:
                logger.warning(f"【{self.plugin_name}】API 状态码 {res.status_code}")
                return []
            data = res.json() or {}
            if data.get("status") != "ok":
                logger.warning(f"【{self.plugin_name}】API 返回异常: {str(data)[:120]}")
                return []
            movies = (data.get("data") or {}).get("movies") or []
            logger.info(f"【{self.plugin_name}】搜索「{keyword or '(最新)'}」命中 {len(movies)} 部电影")
            return movies
        except Exception as e:
            logger.error(f"【{self.plugin_name}】请求失败: {e}")
            return []

    @staticmethod
    def _to_bytes(size_text: str, size_bytes) -> int:
        if isinstance(size_bytes, (int, float)) and size_bytes > 0:
            return int(size_bytes)
        if not size_text:
            return 0
        m = re.match(r"([\d.]+)\s*([KMGTP]?)B", str(size_text).strip().upper())
        if not m:
            return 0
        val, unit = float(m.group(1)), m.group(2)
        mul = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
               "T": 1024 ** 4, "P": 1024 ** 5}.get(unit, 1)
        return int(val * mul)

    def _parse(self, movies: list) -> List[TorrentInfo]:
        results: List[TorrentInfo] = []
        for mv in movies:
            title_long = mv.get("title_long") or f"{mv.get('title')} ({mv.get('year')})"
            page_url = mv.get("url") or self._site_url
            summary = mv.get("summary") or ""
            # 标题带上画质/编码，便于 MoviePilot 优先级判断
            torrents = mv.get("torrents") or []
            for t in torrents:
                quality = t.get("quality") or ""
                ttype = t.get("type") or ""
                codec = t.get("video_codec") or ""
                tags = " ".join(x for x in (quality, ttype, codec) if x)
                title = f"{title_long} [{tags}]" if tags else title_long
                # 种子直链
                enclosure = t.get("url") or ""
                if not enclosure and t.get("hash"):
                    enclosure = f"https://yts.gg/torrent/download/{t.get('hash')}"
                if not enclosure:
                    continue
                # 发布时间
                pubdate = None
                ts = t.get("date_uploaded_unix")
                if isinstance(ts, (int, float)) and ts > 0:
                    try:
                        pubdate = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pubdate = None
                results.append(TorrentInfo(
                    site_name=self._site_name,
                    title=title,
                    description=summary or title,
                    enclosure=enclosure,
                    page_url=page_url,
                    size=self._to_bytes(t.get("size"), t.get("size_bytes")),
                    pubdate=pubdate,
                    seeders=int(t.get("seeds") or 0),
                    peers=int(t.get("peers") or 0),
                    grabs=0,
                    downloadvolumefactor=0.0,
                    uploadvolumefactor=1.0,
                    category="电影",
                    labels=[quality, ttype, codec] if quality else [],
                ))
        return results

    def search(self, keyword: Optional[str] = None) -> List[TorrentInfo]:
        return self._parse(self._fetch(keyword))

    def browse(self) -> List[TorrentInfo]:
        return self._parse(self._fetch(None))

    def api_search(self, keyword: str = "", page: int = 0):
        items = self.search(keyword) if keyword else self.browse()
        return [{
            "title": i.title,
            "size": i.size,
            "enclosure": i.enclosure,
            "page_url": i.page_url,
            "pubdate": i.pubdate.strftime("%Y-%m-%d %H:%M:%S") if i.pubdate else "",
            "description": (i.description or "")[:200],
            "seeders": i.seeders,
            "peers": i.peers,
        } for i in items]

    # ---------------- 补丁 ----------------

    def _is_target(self, domain: Optional[str]) -> bool:
        if not domain:
            return False
        d = str(domain).lower()
        return ("yts" in d) or ("torrentsbay" in d)

    def _apply_patch(self):
        plugin = self

        # ① 搜索链（三个入口）
        if self._patch_search and not getattr(SearchChain, "__yts_search_patched__", False):
            prev_sync = SearchChain._SearchChain__search_all_sites
            prev_async = SearchChain._SearchChain__async_search_all_sites

            def patched_sync(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
                res = list(prev_sync(chain_self, keyword, mediainfo, sites, page, area) or [])
                # 关键：不要在 sites 非空时短路 —— 自定义索引器永远不在 sites 列表里，
                # 短路会导致订阅永远搜不到本插件的结果。
                if not plugin._enabled:
                    return res
                try:
                    return res + plugin.search(keyword)
                except Exception as e:
                    logger.error(f"【{plugin.plugin_name}】搜索补丁异常: {e}")
                    return res

            async def patched_async(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
                res = list(await prev_async(chain_self, keyword, mediainfo, sites, page, area) or [])
                if not plugin._enabled:
                    return res
                try:
                    return res + await asyncio.to_thread(plugin.search, keyword)
                except Exception as e:
                    logger.error(f"【{plugin.plugin_name}】异步搜索补丁异常: {e}")
                    return res

            patched_sync.__yts_patched__ = True
            patched_async.__yts_patched__ = True
            SearchChain._SearchChain__search_all_sites = patched_sync
            SearchChain._SearchChain__async_search_all_sites = patched_async
            SearchChain.__yts_search_patched__ = True
            logger.info(f"【{self.plugin_name}】搜索链补丁已应用")

        # ② 浏览链
        if not getattr(TorrentsChain, "__yts_browse_patched__", False):
            prev_browse = TorrentsChain.browse
            prev_async_browse = TorrentsChain.async_browse

            def patched_browse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
                if plugin._enabled and plugin._is_target(domain):
                    try:
                        return plugin.search(keyword) if keyword else plugin.browse()
                    except Exception as e:
                        logger.error(f"【{plugin.plugin_name}】浏览补丁异常: {e}")
                return prev_browse(chain_self, domain, keyword, cat, page, **kwargs)

            async def patched_async_browse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
                if plugin._enabled and plugin._is_target(domain):
                    try:
                        return await asyncio.to_thread(plugin.browse)
                    except Exception as e:
                        logger.error(f"【{plugin.plugin_name}】异步浏览补丁异常: {e}")
                return await prev_async_browse(chain_self, domain, keyword, cat, page, **kwargs)

            patched_browse.__yts_patched__ = True
            patched_async_browse.__yts_patched__ = True
            TorrentsChain.browse = patched_browse
            TorrentsChain.async_browse = patched_async_browse
            TorrentsChain.__yts_browse_patched__ = True
            logger.info(f"【{self.plugin_name}】浏览链补丁已应用")

    def _remove_patch(self):
        try:
            if getattr(SearchChain, "__yts_search_patched__", False):
                for attr in ("_SearchChain__search_all_sites", "_SearchChain__async_search_all_sites"):
                    fn = getattr(SearchChain, attr, None)
                    if fn is not None and getattr(fn, "__yts_patched__", False):
                        # 无法取回原函数时保留现有实现，仅清 marker
                        pass
                SearchChain.__yts_search_patched__ = False
            if getattr(TorrentsChain, "__yts_browse_patched__", False):
                TorrentsChain.__yts_browse_patched__ = False
            logger.info(f"【{self.plugin_name}】补丁已标记移除")
        except Exception as e:
            logger.error(f"【{self.plugin_name}】移除补丁失败: {e}")
