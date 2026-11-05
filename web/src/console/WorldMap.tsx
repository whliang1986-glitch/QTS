// 全球情报地图（UI-04 的布局部分）：本地打包的 Natural Earth 1:110m 陆地轮廓（world-atlas，公共领域数据，ISC 包），
// 用 d3-geo 投影成 SVG，不联网加载任何瓦片或字体。
//
// 只画有定位依据的事件；没有定位的事件由调用方放进“未定位事件”列表（规范 4.3）。
// 动效只是 CSS 的脉冲，减少动效时关闭（console.css）。
import { geoGraticule10, geoNaturalEarth1, geoPath } from "d3-geo";
import { useMemo } from "react";
import { feature } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import land110m from "world-atlas/land-110m.json";
import type { IntelEvent, Severity } from "./types";

const WIDTH = 960;
const HEIGHT = 470;

const SEVERITY_CLASS: Record<Severity, string> = {
  CRITICAL: "hot-critical",
  HIGH: "hot-high",
  MEDIUM: "hot-medium",
  LOW: "hot-low",
};

export function WorldMap({ events, selected, onSelect }: {
  events: IntelEvent[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const { land, graticule, projection } = useMemo(() => {
    const topology = land110m as unknown as Topology<{ land: GeometryCollection }>;
    const shape = feature(topology, topology.objects.land);
    const proj = geoNaturalEarth1().fitSize([WIDTH, HEIGHT], { type: "Sphere" });
    const path = geoPath(proj);
    return { land: path(shape) ?? "", graticule: path(geoGraticule10()) ?? "", projection: proj };
  }, []);

  const located = events.filter((e) => e.location !== null);
  return (
    <svg className="world" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="group" aria-label="全球情报地图">
      <path className="world-grid" d={graticule} />
      <path className="world-land" d={land} />
      {located.map((e) => {
        const loc = e.location;
        if (loc === null) return null;
        const point = projection([loc.lon, loc.lat]);
        if (point === null) return null;
        const [x, y] = point;
        const active = e.id === selected;
        return (
          <g key={e.id} className={`hot ${SEVERITY_CLASS[e.severity]}${active ? " hot-active" : ""}`}
            transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`}>
            <circle className="hot-pulse" r={14} />
            <circle className="hot-core" r={4.5} />
            <circle className="hot-hit" r={16} tabIndex={0} role="button"
              aria-label={`${e.region}：${e.title}（定位置信度 ${Math.round(loc.confidence * 100)}%）`}
              onClick={() => onSelect(e.id)}
              onKeyDown={(k) => { if (k.key === "Enter" || k.key === " ") { k.preventDefault(); onSelect(e.id); } }} />
          </g>
        );
      })}
    </svg>
  );
}
