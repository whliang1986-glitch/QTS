// DEMO 入口（UI-02）：只有 demo.html 引用，生产构建（index.html）不打包。不请求任何接口。
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../console/console.css";
import { Console } from "../console/Console";
import { demoClock, demoData } from "./data";

const root = document.getElementById("root");
if (root === null) throw new Error("缺少 #root");
createRoot(root).render(
  <StrictMode>
    <Console data={demoData()} clock={demoClock()} />
  </StrictMode>,
);
