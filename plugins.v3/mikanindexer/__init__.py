# -*- coding: utf-8 -*-
"""
MiKan Indexer Plugin for MoviePilot

通过 MiKan 公开 RSS 接口实现站点搜索：
    https://mikanani.me/RSS/Search?searchstr=<关键词>

原理：MoviePilot 的搜索走 SearchChain.__search_all_sites（同步/异步），
本插件对该方法打补丁，在原始结果之后追加 MiKan 的结果，
因此可以在「搜索」「订阅」里像普通站点一样使用 MiKan，无需真实索引器。

Version: 1.0.0
"""
import re
import asyncio
from typing import List, Dict, Optional, Tuple, Any, Callable
from urllib.parse import quote

from app.chain.search import SearchChain
from app.core.context import MediaInfo, TorrentInfo
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils


class MikanIndexer(_PluginBase):
    """MiKan 索引器：用蜜柑计划公开 RSS 提供搜索/浏览能力"""

    plugin_name = "MiKan索引器"
    plugin_desc = "通过 MiKan 公开 RSS 实现搜索，让 MiKan 像普通站点一样可搜索/可订阅"
    plugin_icon = ""
    plugin_version = "1.0.0"
    plugin_author = "hermes"
    author_url = "https://mikanani.me"
    plugin_config_prefix = "mikanindexer_"
    plugin_order = 20
    auth_level = 1

    # 运行时状态
    _enabled: bool = False
    _proxy: bool = False
    _notify: bool = False
    _rss_base: str = "https://mikanani.me/RSS/Classic"
    _site_name: str = "MiKan"
    _limit: int = 100
    _recent: list = []
    _original_sync: Optional[Callable] = None
    _original_async: Optional[Callable] = None
    _original_browse: Optional[Callable] = None
    _original_async_browse: Optional[Callable] = None

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._proxy = bool(config.get("proxy", False))
        self._notify = bool(config.get("notify", False))
        self._rss_base = (config.get("rss_base") or "https://mikanani.me/RSS/Classic").strip()
        self._search_base = (config.get("search_base") or "https://mikanani.me/RSS/Search?searchstr=").strip()
        self._patch_search = bool(config.get("patch_search", False))
        self._site_name = (config.get("site_name") or "MiKan").strip()
        self._site_domain = (config.get("site_domain") or "mikanani.me").strip().lower()
        try:
            self._limit = max(1, min(100, int(config.get("limit") or 100)))
        except (TypeError, ValueError):
            self._limit = 100

        if self._enabled:
            self._apply_patch()
            logger.info(f"【{self.plugin_name}】已启用，RSS={self._rss_base}")
        else:
            self._remove_patch()
            logger.info(f"【{self.plugin_name}】未启用")

    # ------------------------------------------------------------------
    # RSS 搜索实现
    # ------------------------------------------------------------------
    def _fetch_rss(self, keyword: str) -> str:
        """请求 MiKan RSS 接口；keyword 为空时返回最新列表（浏览用）"""
        if keyword:
            url = f"{self._search_base}{quote(keyword)}"
        else:
            url = self._rss_base
        try:
            kwargs = {
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
                "timeout": 20,
            }
            if self._proxy:
                p = self._get_system_proxy()
                if p:
                    kwargs["proxies"] = {"http": p, "https": p}
            res = RequestUtils(**kwargs).get_res(url)
            if res is not None and res.status_code == 200:
                return res.text or ""
            logger.warning(f"【{self.plugin_name}】RSS 状态异常: {getattr(res, 'status_code', None)} url={url}")
        except Exception as e:
            logger.error(f"【{self.plugin_name}】RSS 请求异常: {e}")
        return ""

    @staticmethod
    def _get_system_proxy() -> Optional[str]:
        """读取 MoviePilot 系统代理配置"""
        try:
            from app.core.config import settings
            for attr in ("PROXY", "proxy"):
                p = getattr(settings, attr, None)
                if p:
                    return str(p)
        except Exception:
            pass
        try:
            from app.helper.proxy import ProxyHelper
            p = ProxyHelper().get_proxy()
            if p:
                return str(p)
        except Exception:
            pass
        return None


    @staticmethod
    def _tag(raw: str, tag: str) -> str:
        """从 item 片段里取标签内容"""
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, re.S)
        if not m:
            return ""
        val = m.group(1).strip()
        # 去掉 CDATA
        val = re.sub(r"^<!\[CDATA\[|\]\]>$", "", val).strip()
        return val

    @staticmethod
    @staticmethod
    def _parse_episode_season(title: str) -> Tuple[Optional[int], int]:
        """从 MiKan 标题解析（集数, 季号）"""
        season = 1
        if re.search(r"第\s*[二2]\s*季|2nd\s*[Ss]eason|[Ss]eason\s*2", title):
            season = 2
        elif re.search(r"第\s*[三3]\s*季|3rd\s*[Ss]eason|[Ss]eason\s*3", title):
            season = 3
        else:
            m_s = re.search(r"\bS(\d{1,2})\b", title)
            if m_s:
                season = int(m_s.group(1))
        m = re.search(r"S(\d{1,2})\s*E(\d{1,4})", title, re.I)
        if m:
            return int(m.group(2)), int(m.group(1))
        m = re.search(r"[-\u2013\u2014]\s*(\d{1,4})(?:v\d)?(?=\s*[\[\(\u3010]|\s*$)", title)
        if m:
            return int(m.group(1)), season
        m = re.search(r"[\[\u3010](\d{1,4})(?:v\d)?[\]\u3011]", title)
        if m:
            return int(m.group(1)), season
        m = re.search(r"第\s*(\d{1,4})\s*[话話集]", title)
        if m:
            return int(m.group(1)), season
        m = re.search(r"\bE(\d{1,4})\b", title)
        if m:
            return int(m.group(1)), season
        return None, season

    @staticmethod
    def _apply_episode(title: str, episode: Optional[int], season: int) -> str:
        """把解析出的集数标准化成 SxxExx 写回标题"""
        if not episode:
            return title
        std = "S%02dE%02d" % (season, episode)
        if re.search(r"S\d{1,2}\s*E\d{1,4}", title, re.I):
            return re.sub(r"S\d{1,2}\s*E\d{1,4}", std, title, count=1, flags=re.I)
        m = re.search(r"[\[\u3010](\d{1,4})(?:v\d)?[\]\u3011]", title)
        if m and int(m.group(1)) == episode:
            return title[:m.start()] + std + title[m.end():]
        m = re.search(r"([-\u2013\u2014]\s*)(\d{1,4})(?:v\d)?(?=\s*[\[\(\u3010]|\s*$)", title)
        if m and int(m.group(2)) == episode:
            return title[:m.start(2)] + std + title[m.end(2):]
        return title + " " + std

    def _parse_items(self, xml_text: str) -> List[TorrentInfo]:
        """解析 MiKan RSS -> TorrentInfo 列表"""
        torrents: List[TorrentInfo] = []
        if not xml_text:
            return torrents

        for raw in re.findall(r"<item>(.*?)</item>", xml_text, re.S):
            title = self._tag(raw, "title")
            # MiKan 的种子直链在 enclosure url 上
            m_enc = re.search(r'<enclosure[^>]*url="([^"]+)"', raw)
            torrent_url = m_enc.group(1) if m_enc else ""
            if not title or not torrent_url:
                continue

            page_url = self._tag(raw, "link")
            # 大小：<contentLength>（字节）
            size = 0.0
            m_len = re.search(r"<contentLength>(\d+)</contentLength>", raw)
            if m_len:
                try:
                    size = float(m_len.group(1))
                except ValueError:
                    size = 0.0
            # 发布时间：<torrent> 内的 pubDate，ISO 格式
            pubdate = ""
            m_pd = re.search(r"<pubDate>([^<]+)</pubDate>", raw)
            if m_pd:
                pubdate = m_pd.group(1).strip()

            # 字幕组：标题里第一个 [...] 或 【...】
            group = ""
            m_grp = re.match(r"^\s*[\[【]([^\]】]+)[\]】]", title)
            if m_grp:
                group = m_grp.group(1).strip()

            # 番名：取 " / " 或 "" 前的部分（去掉字幕组前缀）
            anime = ""
            t2 = re.sub(r"^\s*[\[【][^\]】]+[\]】]\s*", "", title)
            anime = re.split(r"\s*/\s*|\\", t2)[0].strip()[:60]

            # 分辨率
            resolution = ""
            m_res = re.search(r"(2160p|1080p|720p|4k|8k)", title, re.I)
            if m_res:
                resolution = m_res.group(1).upper()

            # 集数与季号（MiKan 无独立字段，从标题解析）
            episode, season = self._parse_episode_season(title)

            desc_parts = [p for p in [
                anime,
                f"EP{episode}" if episode else "",
                resolution,
                group,
            ] if p]

            std_title = self._apply_episode(title, episode, season)
            torrents.append(TorrentInfo(
                site_name=self._site_name,
                title=std_title,
                description=" · ".join(desc_parts),
                seeders=1,
                peers=1,
                enclosure=torrent_url,
                page_url=page_url or "",
                size=size,
                pubdate=pubdate,
                downloadvolumefactor=0.0,
                uploadvolumefactor=1.0,
                category="动漫",
            ))
        return torrents

    def search(self, keyword: str) -> List[TorrentInfo]:
        """对外搜索入口"""
        if not self._enabled or not keyword:
            return []
        xml = self._fetch_rss(keyword)
        items = self._parse_items(xml)
        try:
            from datetime import datetime as _dt
            self._recent.insert(0, {"time": _dt.now().strftime("%m-%d %H:%M"),
                                   "keyword": keyword, "count": len(items)})
            self._recent = self._recent[:15]
        except Exception:
            pass
        if items:
            logger.info(f"【{self.plugin_name}】搜索「{keyword}」命中 {len(items)} 条")
        return items

    def browse(self) -> List[TorrentInfo]:
        """站点浏览入口：拉取 MiKan 最新种子"""
        if not self._enabled:
            return []
        xml = self._fetch_rss("")
        items = self._parse_items(xml)
        if items:
            logger.info(f"【{self.plugin_name}】浏览最新资源 {len(items)} 条")
        return items

    # ------------------------------------------------------------------
    # 搜索链补丁
    # ------------------------------------------------------------------
    def _apply_patch(self):
        """把 MiKan 结果挂到 SearchChain 的站点搜索上"""
        self._remove_patch()
        try:
            prev_sync = SearchChain._SearchChain__search_all_sites
            prev_async = SearchChain._SearchChain__async_search_all_sites
        except AttributeError as e:
            logger.error(f"【{self.plugin_name}】找不到搜索方法，兼容性异常: {e}")
            return

        self._original_sync = prev_sync
        self._original_async = prev_async
        plugin = self

        def patched_sync(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
            results = prev_sync(chain_self, keyword, mediainfo, sites, page, area)
            results = list(results) if results else []
            # 注意：MiKan 是插件而非站点，sites 列表里永远不含它。
            # 订阅流程会传 sites（全局站点列表），早前在此 return 导致订阅完全搜不到 MiKan。
            try:
                extra = plugin.search(keyword)
                if extra:
                    return results + extra
            except Exception as e:
                logger.error(f"【{plugin.plugin_name}】同步补丁异常: {e}")
            return results

        async def patched_async(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
            results = await prev_async(chain_self, keyword, mediainfo, sites, page, area)
            results = list(results) if results else []
            try:
                extra = await asyncio.to_thread(plugin.search, keyword)
                if extra:
                    return results + extra
            except Exception as e:
                logger.error(f"【{plugin.plugin_name}】异步补丁异常: {e}")
            return results

        patched_sync.__mikan_patched__ = True
        patched_async.__mikan_patched__ = True
        # MoviePilot 内置 MiKan 解析器工作正常，若再挂搜索补丁会导致结果翻倍重复，
        # 故搜索链补丁默认关闭（需要时把 _patch_search 配成 True）。
        if plugin._patch_search:
            SearchChain._SearchChain__search_all_sites = patched_sync
            SearchChain._SearchChain__async_search_all_sites = patched_async
            logger.info(f"【{self.plugin_name}】搜索链补丁已安装")
        else:
            logger.info(f"【{self.plugin_name}】搜索链补丁已跳过（内置解析器已可用，避免重复）")

        # ---- 浏览链补丁：站点管理里点「浏览」时改走 RSS ----
        try:
            from app.chain.torrents import TorrentsChain
            prev_browse = TorrentsChain.browse
            prev_async_browse = TorrentsChain.async_browse
            self._original_browse = prev_browse
            self._original_async_browse = prev_async_browse

            def _is_mikan(domain) -> bool:
                d = (domain or "").lower()
                return bool(d) and (plugin._site_domain in d or "mikan" in d)

            def patched_browse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
                if _is_mikan(domain):
                    try:
                        return plugin.search(keyword) if keyword else plugin.browse()
                    except Exception as e:
                        logger.error(f"【{plugin.plugin_name}】浏览补丁异常: {e}")
                return prev_browse(chain_self, domain, keyword, cat, page, **kwargs)

            async def patched_async_browse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
                if _is_mikan(domain):
                    try:
                        if keyword:
                            return await asyncio.to_thread(plugin.search, keyword)
                        return await asyncio.to_thread(plugin.browse)
                    except Exception as e:
                        logger.error(f"【{plugin.plugin_name}】异步浏览补丁异常: {e}")
                return await prev_async_browse(chain_self, domain, keyword, cat, page, **kwargs)

            patched_browse.__mikan_patched__ = True
            patched_async_browse.__mikan_patched__ = True
            TorrentsChain.browse = patched_browse
            TorrentsChain.async_browse = patched_async_browse
            logger.info(f"【{self.plugin_name}】浏览链补丁已安装")
        except Exception as e:
            logger.error(f"【{self.plugin_name}】浏览链补丁安装失败: {e}")

    def _remove_patch(self):
        """卸载补丁，恢复原始方法"""
        try:
            cur_sync = SearchChain._SearchChain__search_all_sites
            if self._original_sync is not None and getattr(cur_sync, "__mikan_patched__", False):
                SearchChain._SearchChain__search_all_sites = self._original_sync
        except Exception:
            pass
        try:
            cur_async = SearchChain._SearchChain__async_search_all_sites
            if self._original_async is not None and getattr(cur_async, "__mikan_patched__", False):
                SearchChain._SearchChain__async_search_all_sites = self._original_async
        except Exception:
            pass
        try:
            from app.chain.torrents import TorrentsChain
            cur_b = TorrentsChain.browse
            if self._original_browse is not None and getattr(cur_b, "__mikan_patched__", False):
                TorrentsChain.browse = self._original_browse
            cur_ab = TorrentsChain.async_browse
            if self._original_async_browse is not None and getattr(cur_ab, "__mikan_patched__", False):
                TorrentsChain.async_browse = self._original_async_browse
        except Exception:
            pass
        self._original_browse = None
        self._original_async_browse = None
        self._original_sync = None
        self._original_async = None

    # ------------------------------------------------------------------
    # MoviePilot 插件接口
    # ------------------------------------------------------------------
    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """提供一个手动测试搜索的接口"""
        return [
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["GET"],
                "summary": "测试 MiKan 搜索",
                "description": "用关键词测试 MiKan 索引器是否工作",
            }
        ]

    def api_search(self, keyword: str = "", apikey: str = ""):
        """手动测试接口: /api/v1/plugin/MikanIndexer/search?keyword=xxx"""
        if not keyword:
            return {"success": False, "message": "缺少 keyword 参数"}
        # 统一走 search()，这样插件页面的搜索统计也能记录
        items = self.search(keyword)
        return {
            "success": True,
            "count": len(items),
            "data": [
                {
                    "title": t.title,
                    "size": t.size,
                    "enclosure": t.enclosure,
                    "page_url": t.page_url,
                    "pubdate": t.pubdate,
                    "description": t.description,
                }
                for t in items[:20]
            ],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "enabled", "label": "启用插件"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "proxy", "label": "使用代理服务器"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{"component": "VSwitch", "props": {
                                    "model": "notify", "label": "发送通知"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VTextField", "props": {
                                    "model": "site_name",
                                    "label": "站点名称",
                                    "placeholder": "MiKan"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VTextField", "props": {
                                    "model": "rss_base",
                                    "label": "RSS 地址",
                                    "placeholder": "https://mikanani.me/RSS/Classic"}}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "VTextField", "props": {
                                    "model": "limit",
                                    "label": "单次最大结果数",
                                    "type": "number",
                                    "placeholder": "100"}}],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{"component": "VAlert", "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "说明：MiKan 无标准索引器，本插件通过其公开 RSS "
                                            "（/rss/magnets.xml?q=关键词）实现搜索，"
                                            "并挂载到 MoviePilot 的站点搜索链上。"
                                            "启用后可在搜索/订阅中直接使用。"}}],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "proxy": False,
            "notify": False,
            "site_name": "MiKan",
            "rss_base": "https://mikanani.me/RSS/Classic",
            "limit": 100,
        }

    def get_page(self) -> List[dict]:
        """插件页面：展示最近搜索记录"""
        rows = self._recent or []
        stats = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {
                        "type": "success" if self._enabled else "warning",
                        "variant": "tonal",
                        "text": (f"插件{'已启用' if self._enabled else '未启用'} · "
                                 f"搜索源 {self._site_name} · "
                                 f"单次上限 {self._limit} 条")}}]},
            ]},
        ]
        if not rows:
            stats.append({"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VAlert", "props": {
                        "type": "info", "variant": "tonal",
                        "text": "暂无搜索记录。在 MoviePilot 搜索或订阅时，MiKan 会自动参与。"}}]},
            ]})
        else:
            stats.append({"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12}, "content": [
                    {"component": "VTable", "props": {"density": "compact"},
                     "content": [
                         {"component": "thead", "content": [
                             {"component": "tr", "content": [
                                 {"component": "th", "content": [{"component": "span", "text": "时间"}]},
                                 {"component": "th", "content": [{"component": "span", "text": "关键词"}]},
                                 {"component": "th", "content": [{"component": "span", "text": "命中"}]},
                             ]}]},
                         {"component": "tbody", "content": [
                             {"component": "tr", "content": [
                                 {"component": "td", "content": [{"component": "span", "text": r.get("time", "")}]},
                                 {"component": "td", "content": [{"component": "span", "text": r.get("keyword", "")}]},
                                 {"component": "td", "content": [{"component": "span", "text": str(r.get("count", 0))}]},
                             ]} for r in rows
                         ]},
                     ]}]},
            ]})
        return stats

    def stop_service(self):
        self._remove_patch()
        logger.info(f"【{self.plugin_name}】已停止并卸载补丁")
