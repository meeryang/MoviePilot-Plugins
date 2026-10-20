# MoviePilot Plugins · 虾虾

自制的 [MoviePilot](https://github.com/jxxghp/MoviePilot) **v3** 插件集合。

> 全部插件均在 `jxxghp/moviepilot-v3:latest`（v3.1.8）上实测通过。

## 插件列表

| 插件 | 说明 |
|---|---|
| **YtsIndexer** | 让 YTS（yts.gg）参与搜索与浏览。用 JSON API 而非 RSS，7.7 万部电影 |
| **ProwlarrSitesIndexer** | 把 Prowlarr 里的全部索引器通过 Torznab 统一接入，一个插件覆盖多站 |
| **ImdbSearch** | 按关键词搜 IMDb，再用 IMDb ID 回站点精确找资源 |
| **LxMusicDownloader** | 调用 lx-sync-server 搜索并下载音乐，支持订阅歌手、音质可选（默认 320k） |
| **AniBTIndexer** | AniBT（anibt.net）RSS 搜索 + 浏览链补丁 |
| **MikanIndexer** | MiKan（mikanani.me）浏览链补丁（搜索交给内置解析器） |
| **NyaaIndexer** | Nyaa（nyaa.si）浏览链补丁（搜索交给内置蜘蛛） |

## 安装

在 MoviePilot 中：

1. **设置 → 插件市场 → 添加插件仓库**，填入：
   ```
   https://github.com/meeryang/MoviePilot-Plugins
   ```
2. 刷新插件市场，安装所需插件
3. 在插件设置里填写你自己的地址与密钥

或直接改配置项 `PLUGIN_MARKET`（多个仓库用英文逗号分隔）。

## 重要说明

### v3 专属

本仓库只提供 `package.v3.json` + `plugins.v3/`，**面向 MoviePilot v3**。
v2 用户请勿使用。

### 各插件的前置条件

- **YtsIndexer**：需要能访问 `yts.gg`（官方 API 只在裸域名可用，`yts.torrentsbay.org` 被 Cloudflare 全站拦截）
- **ProwlarrSitesIndexer**：需要本地已有 Prowlarr，并在插件里填地址与 API Key
- **AniBTIndexer / MikanIndexer / NyaaIndexer**：只需网络可达对应站点
- **LxMusicDownloader**：
  - 需要 [lx-sync-server](https://github.com/xcq0607/lxserver)（`xcq0607/lxserver` 镜像）
  - 在 lx-sync-server 的 **Token 管理**里生成 `lx_tk_*` 令牌
  - ⚠️ 服务端 `config.js` 中 `user.enablePublicNonAdminServerCache` 必须为 `true`，
    否则下载返回 403「非管理员服务器缓存已被禁用」
  - 音质说明：实际码率由音源直链决定。插件会按所选音质向服务端声明，
    但酷我公开接口通常只提供 128k mp3；真无损需音源会员或自行实现源脚本

## 注意事项

- 插件代码里的服务端地址默认值均写作 `127.0.0.1`，**请改成你自己的实际地址**
- 所有密钥（Prowlarr API Key、lx-sync-server Token 等）请在插件设置页填写，不要写进代码
- 本仓库代码按「自己用着顺手」的标准编写，不保证对所有站点环境通用

## 目录结构

```
├── package.v3.json          # v3 插件索引
├── plugins.v3/
│   ├── anibtindexer/__init__.py
│   ├── imdbsearch/__init__.py
│   ├── lxmusicdownloader/__init__.py
│   ├── mikanindexer/__init__.py
│   ├── nyaaindexer/__init__.py
│   ├── prowlarrsitesindexer/__init__.py
│   └── ytsindexer/__init__.py
└── icons/
```

## License

MIT
