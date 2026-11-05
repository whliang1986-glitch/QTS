# 地图数据登记（UI-02）

| 数据 | 来源 | 许可 | 在仓库里的用法 |
|---|---|---|---|
| 世界陆地轮廓，1:110m | Natural Earth（naturalearthdata.com），经 npm 包 `world-atlas@2.0.2` 转成 TopoJSON | 数据：公共领域；`world-atlas` 包：ISC | `web/src/console/WorldMap.tsx` 导入 `world-atlas/land-110m.json`（约 56 KB），构建时打包进前端；运行时不联网加载 |
| 地图投影与绘制 | `d3-geo@3.1.1`（依赖 `d3-array`、`internmap`），`topojson-client@3.1.0`（依赖 `commander`） | ISC / MIT | 见 `THIRD_PARTY.md` |

规则：
- 不用需要联网、令牌或付费的地图服务（Mapbox、Google Maps 等）；
- 不加载外部瓦片、字体或图片；
- 换用其他地图数据前，先在这里登记来源和许可。
