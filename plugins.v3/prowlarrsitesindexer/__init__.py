"""
Prowlarr 多站索引器插件 —— 把 Prowlarr 里所有索引器接入 MoviePilot 搜索/浏览。

设计思路（与 AniBT 插件同款形态，但一次覆盖全部站点）：
- 不逐站写解析器，统一走 Prowlarr 的 Torznab 接口：
    http://<prowlarr>/<indexer_id>/api?apikey=KEY&t=search&q=<关键词>
  返回标准 Torznab RSS，字段统一（title/size/pubDate/prowlarrindexer/torznab:attr）。
- enclosure 直接用 Prowlarr 的 <link>（代理下载，不吃各站 cookie/Cloudflare），
  这是相比自研单站插件最大的优势。
- site_name 取 <prowlarrindexer> 的文本，这样每个站的结果在界面上可区分。

必须遵守的两个坑（血泪教训）：
1. TorrentInfo.pubdate 只能给字符串，给 datetime 会让 /site/resource/{id} 报 500
   （pydantic response_model 校验 string_type 失败）。
2. patch 必须同时覆盖 SearchChain 的三个入口 + TorrentsChain 的 browse/async_browse，
   且订阅侧不能因为 sites 非空就 return。
"""
import re
import asyncio
import threading
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils
from app.core.context import TorrentInfo  # 必须是 core.context（dataclass，有 to_dict）；app.schemas 的是 pydantic 版，会害 Context.to_dict() 崩

# Torznab 命名空间属性
_ATTR_RE = re.compile(r'<torznab:attr\s+name="([^"]+)"\s+value="([^"]*)"', re.I)
_ITEM_RE = re.compile(r'<item>(.*?)</item>', re.S | re.I)


_SURR_RE = re.compile(r'[\ud800-\udfff]')


def _clean(s) -> str:
    """清洗孤立代理项等非法字符 —— 否则 FastAPI JSON 序列化会抛
    UnicodeEncodeError: surrogates not allowed，整个响应 500。"""
    if not s:
        return ""
    s = str(s)
    try:
        return _SURR_RE.sub("", s).encode("utf-8", "ignore").decode("utf-8", "ignore")
    except Exception:
        return ""


def _tag(xml: str, name: str) -> str:
    m = re.search(r'<%s[^>]*>(.*?)</%s>' % (name, name), xml, re.S | re.I)
    if not m:
        return ""
    v = m.group(1).strip()
    v = re.sub(r'^<!\[CDATA\[(.*?)\]\]>$', r'\1', v, flags=re.S)
    v = (v.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&apos;", "'").strip())
    return _clean(v)


def _attrs(xml: str) -> Dict[str, str]:
    return {k.lower(): v for k, v in _ATTR_RE.findall(xml)}


def _to_pubdate(raw: str) -> str:
    """RFC2822 → 'YYYY-MM-DD HH:MM:SS' 字符串（绝不能返回 datetime）"""
    if not raw:
        return ""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(raw)
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return ""


class ProwlarrSitesIndexer(_PluginBase):
    # 插件元数据
    plugin_name = "Prowlarr多站索引器"
    plugin_desc = "把 Prowlarr 中的所有索引器通过 Torznab 接口接入 MoviePilot 搜索与浏览"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/prowlarr.png"
    plugin_version = "1.0.0"
    plugin_author = "虾虾"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_config_prefix = "prowlarrsitesindexer_"
    plugin_order = 6
    auth_level = 1

    # 运行态
    _enabled: bool = False
    _prowlarr_url: str = ""
    _apikey: str = ""
    _timeout: int = 20
    _max_workers: int = 6
    _only_enabled: bool = True
    _site_names: List[str] = []
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._prowlarr_url = (config.get("prowlarr_url") or "").rstrip("/")
        self._apikey = config.get("apikey") or ""
        self._timeout = int(config.get("timeout") or 20)
        self._max_workers = int(config.get("max_workers") or 6)
        self._only_enabled = bool(config.get("only_enabled", True))

        if self._enabled and self._prowlarr_url and self._apikey:
            self._apply_patch()

    # ────────────── Prowlarr 基础 ──────────────
    def _indexers(self) -> List[dict]:
        """拉取 Prowlarr 索引器清单"""
        try:
            url = f"{self._prowlarr_url}/api/v1/indexer"
            res = RequestUtils(timeout=15).get_res(url, params={"apikey": self._apikey})
            if not res or res.status_code != 200:
                logger.warning(f"【Prowlarr多站】索引器清单获取失败: {getattr(res, 'status_code', '无响应')}")
                return []
            data = res.json() or []
            out = []
            for i in data:
                if self._only_enabled and not i.get("enable"):
                    continue
                cats = ((i.get("capabilities") or {}).get("categories") or [])
                # 从 fields 里取 baseUrl 并解析出域名（浏览/站点映射要用）
                base = ""
                for f in (i.get("fields") or []):
                    if (f.get("name") or "").lower() in ("baseurl", "base_url", "url") and f.get("value"):
                        base = str(f.get("value"))
                        break
                domain = ""
                if base:
                    try:
                        from urllib.parse import urlparse
                        domain = (urlparse(base).netloc or "").lower().replace("www.", "")
                    except Exception:
                        domain = ""
                out.append({
                    "id": i.get("id"),
                    "name": i.get("name") or f"Prowlarr-{i.get('id')}",
                    "categories": [c.get("id") for c in cats if c.get("id")],
                    "base": base,
                    "domain": domain,
                })
            return out
        except Exception as e:
            logger.error(f"【Prowlarr多站】索引器清单异常: {e}")
            return []

    def _torznab(self, indexer: dict, keyword: Optional[str], page: int = 0) -> List[TorrentInfo]:
        """单站 Torznab 搜索"""
        iid = indexer.get("id")
        site = indexer.get("name")
        params = {"apikey": self._apikey, "t": "search", "limit": 100}
        if keyword:
            params["q"] = keyword
        if page:
            params["offset"] = page * 100
        try:
            res = RequestUtils(timeout=self._timeout).get_res(
                f"{self._prowlarr_url}/{iid}/api", params=params)
            if not res or res.status_code != 200:
                return []
            items = _ITEM_RE.findall(res.text or "")
            results = []
            for it in items:
                title = _tag(it, "title")
                if not title:
                    continue
                a = _attrs(it)
                enclosure = _tag(it, "link") or _tag(it, "guid")
                if not enclosure:
                    m = re.search(r'<enclosure[^>]+url="([^"]+)"', it, re.I)
                    enclosure = m.group(1).replace("&amp;", "&") if m else ""
                if not enclosure:
                    continue

                def _num(k, default=0):
                    try:
                        return int(float(a.get(k) or default))
                    except Exception:
                        return default

                try:
                    size = int(float(_tag(it, "size") or 0))
                except Exception:
                    size = 0

                cats = re.findall(r'<category>(\d+)</category>', it)
                is_tv = any(c.startswith("5") for c in cats)

                results.append(TorrentInfo(
                    site_name=_clean(site),
                    title=_clean(title),
                    description=_clean(_tag(it, "description") or title),
                    enclosure=_clean(enclosure),
                    page_url=_clean(_tag(it, "comments") or _tag(it, "guid")),
                    size=size,
                    pubdate=_to_pubdate(_tag(it, "pubDate")),
                    seeders=_num("seeders", 1),
                    peers=_num("peers", 1),
                    grabs=0,
                    downloadvolumefactor=float(a.get("downloadvolumefactor") or 0),
                    uploadvolumefactor=float(a.get("uploadvolumefactor") or 1),
                    category="电视剧" if is_tv else "电影",
                    labels=[_clean(site)],
                ))
            return results
        except Exception as e:
            logger.debug(f"【Prowlarr多站】{site} 搜索异常: {e}")
            return []

    def _search_all(self, keyword: Optional[str], page: int = 0) -> List[TorrentInfo]:
        """并发搜索所有索引器"""
        idxs = self._indexers()
        if not idxs:
            return []
        out: List[TorrentInfo] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futs = [ex.submit(self._torznab, i, keyword, page) for i in idxs]
            for f in futs:
                try:
                    out.extend(f.result(timeout=self._timeout + 15))
                except Exception:
                    continue
        # 去重（同站点同标题同大小）
        seen, uniq = set(), []
        for t in out:
            k = (t.site_name, t.title, t.size)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(t)
        logger.info(f"【Prowlarr多站】搜索「{keyword or '(最新)'}」{len(idxs)} 站 → {len(uniq)} 条")
        return uniq

    # ────────────── 补丁：搜索链 + 浏览链 ──────────────
    def _apply_patch(self):
        plugin = self

        try:
            from app.chain.search import SearchChain
        except Exception as e:
            logger.error(f"【Prowlarr多站】导入 SearchChain 失败: {e}")
            return

        if getattr(SearchChain, "__pls_search_patched__", False):
            logger.info("【Prowlarr多站】搜索链补丁已存在，跳过")
            return

        def _is_self(sites) -> bool:
            """与 AniBT 一致：一律参与。根因（TorrentInfo 导错）已修，
            追加模式不会破坏 Context 组装。"""
            return True

        # 1. 同步
        prev_sync = getattr(SearchChain, "_SearchChain__search_all_sites")

        def patched_sync(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
            # 必须先跑原始链路：媒体识别、进度、其他站都在里面，
            # 直接 return 自己的结果会让 process() 拿不到 mediainfo，
            # 进而把裸 TorrentInfo 抛给期望 Context 的接口 → 500。
            results = list(prev_sync(chain_self, keyword, mediainfo, sites, page, area) or [])
            if plugin._enabled:
                try:
                    extra = plugin._filter_by_sites(plugin._search_all(keyword, page), sites)
                    if extra:
                        return results + extra
                except Exception as e:
                    logger.error(f"【Prowlarr多站】同步搜索异常: {e}")
            return results

        setattr(SearchChain, "_SearchChain__search_all_sites", patched_sync)
        patched_sync.__pls_patched__ = True

        # 2. 异步
        prev_async = getattr(SearchChain, "_SearchChain__async_search_all_sites")

        async def patched_async(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
            results = list(await prev_async(chain_self, keyword, mediainfo, sites, page, area) or [])
            if plugin._enabled:
                try:
                    extra = await asyncio.to_thread(
                        lambda: plugin._filter_by_sites(plugin._search_all(keyword, page), sites))
                    if extra:
                        return results + extra
                except Exception as e:
                    logger.error(f"【Prowlarr多站】异步搜索异常: {e}")
            return results

        setattr(SearchChain, "_SearchChain__async_search_all_sites", patched_async)
        patched_async.__pls_patched__ = True

        # 3. 流式（搜索页用）
        if hasattr(SearchChain, "_SearchChain__async_search_all_sites_stream"):
            prev_stream = getattr(SearchChain, "_SearchChain__async_search_all_sites_stream")

            async def patched_stream(chain_self, keyword, mediainfo=None, sites=None, page=0, area="title"):
                # 系统调用方是 event.pop("items", [])，所以这里 yield 的必须是 **dict 事件**，
                # 直接 yield TorrentInfo 会报 'TorrentInfo' object has no attribute 'pop'
                async for event in prev_stream(chain_self, keyword, mediainfo, sites, page, area):
                    yield event
                if plugin._enabled:
                    try:
                        extra = plugin._filter_by_sites(plugin._search_all(keyword, page), sites)
                        if extra:
                            from app.core.context import Context as _Ctx
                            from app.core.meta import MetaInfo as _MetaInfo
                            batch = [
                                _Ctx(
                                    meta_info=_MetaInfo(title=t.title, subtitle=t.description),
                                    torrent_info=t,
                                    resource_source="search",
                                )
                                for t in extra
                            ]
                            yield {
                                "type": "append",
                                "stage": "prowlarr",
                                "text": f"Prowlarr 多站补充 {len(batch)} 个资源",
                                "items": [c.to_dict() for c in batch],
                                "total_items": len(batch),
                            }
                    except Exception as e:
                        logger.error(f"【Prowlarr多站】流式搜索异常: {e}")

            setattr(SearchChain, "_SearchChain__async_search_all_sites_stream", patched_stream)
            patched_stream.__pls_patched__ = True

        SearchChain.__pls_search_patched__ = True
        logger.info("【Prowlarr多站】搜索链补丁已应用")

        # 浏览链
        try:
            from app.chain.torrents import TorrentsChain
        except Exception as e:
            logger.error(f"【Prowlarr多站】导入 TorrentsChain 失败: {e}")
            return

        if getattr(TorrentsChain, "__pls_browse_patched__", False):
            logger.info("【Prowlarr多站】浏览链补丁已存在，跳过")
            return

        prev_browse = TorrentsChain.browse

        def patched_browse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
            if plugin._enabled and plugin._match_domain(domain):
                try:
                    i = plugin._find_by_domain(domain)
                    if i:
                        return plugin._torznab(i, keyword, page)
                except Exception as e:
                    logger.error(f"【Prowlarr多站】浏览异常: {e}")
            return prev_browse(chain_self, domain, keyword, cat, page, **kwargs)

        TorrentsChain.browse = patched_browse
        patched_browse.__pls_patched__ = True

        prev_abrowse = TorrentsChain.async_browse

        async def patched_abrowse(chain_self, domain, keyword=None, cat=None, page=0, **kwargs):
            if plugin._enabled and plugin._match_domain(domain):
                try:
                    i = plugin._find_by_domain(domain)
                    if i:
                        return plugin._torznab(i, keyword, page)
                except Exception as e:
                    logger.error(f"【Prowlarr多站】异步浏览异常: {e}")
            return await prev_abrowse(chain_self, domain, keyword, cat, page, **kwargs)

        TorrentsChain.async_browse = patched_abrowse
        patched_abrowse.__pls_patched__ = True

        TorrentsChain.__pls_browse_patched__ = True
        logger.info("【Prowlarr多站】浏览链补丁已应用")

    # ────────────── 站点映射辅助 ──────────────
    _cached_domains: Dict[str, dict] = {}

    def _indexers_names(self) -> List[str]:
        return [i["name"] for i in self._indexers()]

    def _allowed_names(self, sites) -> Optional[set]:
        """sites（site id 列表）→ 允许的索引器名集合。
        返回 None 表示不过滤；任何异常也返回 None（宁可多返回，不可搜不到）。"""
        if not sites:
            return None
        try:
            ids = [int(s) for s in sites]
        except Exception:
            return None
        try:
            from app.db.site_oper import SiteOper
            oper = SiteOper()
            my_domains = {i["domain"].lower() for i in self._indexers() if i.get("domain")}
            if not my_domains:
                return None
            names = set()
            for sid in ids:
                try:
                    s = oper.get(sid)
                except Exception:
                    s = None
                if not s:
                    continue
                dom = (getattr(s, "domain", "") or "").lower()
                if dom in my_domains:
                    names.add(s.name)
            return names
        except Exception as e:
            logger.debug(f"【Prowlarr多站】站点过滤失败，改为全部返回: {e}")
            return None

    def _filter_by_sites(self, items: list, sites) -> list:
        allowed = self._allowed_names(sites)
        if allowed is None:
            # None：sites 为空或映射异常 → 不过滤
            return items
        if not allowed:
            # 空集合：用户勾的站里没有本插件接管的，一条都不该追加
            return []
        return [t for t in items if t.site_name in allowed]

    _site_ids_cache = None

    def _my_site_ids(self) -> set:
        """把本插件接管站的域名映射成 MoviePilot 的 site id"""
        if self._site_ids_cache is not None:
            return self._site_ids_cache
        ids = set()
        try:
            domains = {i["domain"] for i in self._indexers() if i.get("domain")}
            base = self._mp_base()
            if base and domains:
                res = RequestUtils(timeout=15).get_res(base + "/api/v1/site/", params={"apikey": self._mp_apikey()})
                if res and res.status_code == 200:
                    for s in (res.json() or []):
                        if (s.get("domain") or "").lower() in domains:
                            ids.add(int(s.get("id")))
        except Exception as e:
            logger.debug(f"【Prowlarr多站】站点 id 映射失败: {e}")
        self._site_ids_cache = ids
        return ids

    def _mp_base(self) -> str:
        return (getattr(self, "_mp_url", "") or "").rstrip("/")

    def _mp_apikey(self) -> str:
        return getattr(self, "_mp_key", "") or ""

    def _refresh_domains(self):
        """缓存 域名 → 索引器 的映射"""
        m = {}
        for i in self._indexers():
            d = (i.get("domain") or "").lower()
            if d:
                m[d] = i
        self._cached_domains = m
        self._site_ids_cache = None

    def _match_domain(self, domain: str) -> bool:
        if not domain:
            return False
        d = domain.lower().replace("www.", "")
        if not self._cached_domains:
            self._refresh_domains()
        return d in self._cached_domains

    def _find_by_domain(self, domain: str) -> Optional[dict]:
        if not domain:
            return None
        d = domain.lower().replace("www.", "")
        if not self._cached_domains:
            self._refresh_domains()
        return self._cached_domains.get(d)

    # ────────────── 对外接口 ──────────────
    def api_search(self, keyword: str = "", page: int = 0):
        """手动测试接口: /api/v1/plugin/ProwlarrSitesIndexer/search?keyword=xxx"""
        if not self._enabled:
            return []
        return self._search_all(keyword or None, int(page or 0))

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/search",
            "endpoint": self.api_search,
            "methods": ["GET"],
            "summary": "搜索 Prowlarr 所有索引器",
        }, {
            "path": "/indexers",
            "endpoint": self.list_indexers,
            "methods": ["GET"],
            "summary": "列出 Prowlarr 索引器",
        }]

    def list_indexers(self) -> List[dict]:
        return self._indexers()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [{
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{
                        "component": "VSwitch",
                        "props": {"model": "only_enabled", "label": "仅包含已启用的索引器"},
                    }],
                }],
            }, {
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VTextField",
                        "props": {"model": "prowlarr_url", "label": "Prowlarr 地址",
                                  "placeholder": "http://127.0.0.1:8138"},
                    }],
                }],
            }, {
                "component": "VRow",
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{
                        "component": "VTextField",
                        "props": {"model": "apikey", "label": "Prowlarr API Key"},
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 6, "md": 3},
                    "content": [{
                        "component": "VTextField",
                        "props": {"model": "timeout", "label": "超时(秒)", "type": "number"},
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 6, "md": 3},
                    "content": [{
                        "component": "VTextField",
                        "props": {"model": "max_workers", "label": "并发数", "type": "number"},
                    }],
                }],
            }],
        }], {
            "enabled": False,
            "only_enabled": True,
            "prowlarr_url": "http://127.0.0.1:8138",
            "apikey": "",
            "timeout": 20,
            "max_workers": 6,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        pass
