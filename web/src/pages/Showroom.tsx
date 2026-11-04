// 组件陈列室（S-13）：全部组件、全部状态的静态示例，用于设计把关与视觉回归截图。
// 只有示例数据，不请求接口；页面顶部明确标注“示例”。
import type { Theme } from "../prefs";
import {
  Button, Empty, Icon, ModeBadge, Notice, PnL, Sparkline, StatCard, Tag, ThemeToggle, statusTone,
} from "../ui";

const SAMPLE_CURVE = [100, 101.2, 100.6, 102.4, 103.1, 102.2, 104.8, 105.3, 104.9, 106.7, 107.2, 106.1, 108.4];
const LONG_EN = "Stop new risk (persistent HALT; protective stops stay in place)";

export function Showroom({ theme, setTheme }: { theme: Theme; setTheme: (t: Theme) => void }) {
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">QTS · 组件陈列室</span>
        <Tag tone="info">示例数据，不连接服务器</Tag>
        <span className="spacer" />
        <ThemeToggle theme={theme} onChange={setTheme} />
      </header>
      <main className="showroom" data-testid="showroom">
        <section>
          <h2>模式标识（三态，颜色之外必有文字）</h2>
          <div className="row section"><ModeBadge mode="PAPER" sample /><ModeBadge mode="HALT" sample /><ModeBadge mode="UNKNOWN" sample /></div>
          <div className="section halt-band" role="presentation"><Icon name="stop" />已停止新增风险（HALT）：人工急停。恢复须走恢复流程。</div>
          <div className="stale"><Icon name="clock" />数据可能已过期，服务器仍在运行。（超过 10 秒没有刷新）</div>
        </section>

        <section>
          <h2>按钮</h2>
          <div className="row section">
            <Button kind="primary">主要操作</Button>
            <Button>次要操作</Button>
            <Button kind="ghost">文字按钮</Button>
            <Button kind="danger-outline" icon="alert">撤销保护性止损……</Button>
            <Button kind="danger">确认执行（高危，展开后）</Button>
            <Button kind="primary" disabled>不可用</Button>
            <Button small icon="logout">小按钮</Button>
          </div>
        </section>

        <section>
          <h2>标签</h2>
          <div className="row section">
            <Tag tone="ok">正常</Tag><Tag tone="warn">警告</Tag><Tag tone="danger">危险</Tag>
            <Tag tone="info">信息</Tag><Tag>中性</Tag>
            {["FILLED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN", "REJECTED", "CANCELLED"].map((s) => (
              <Tag key={s} tone={statusTone(s)}>{s}</Tag>
            ))}
          </div>
        </section>

        <section>
          <h2>数字卡片</h2>
          <div className="cards section">
            <StatCard label="净值（USD）" value="125,430.18" sub={<PnL value="1243.50" />} />
            <StatCard label="现金（已结算 / 未结算）" value="40,120.00 / 2,300.00" />
            <StatCard label="当日盈亏" value="−312.40" sub={<PnL value="-312.40" />} />
            <StatCard label="持仓数" value="6" />
            <StatCard label="暂无数据（骨架屏）" value={null} />
          </div>
          <div className="row section"><span>盈亏：</span><PnL value="532.10" /><PnL value="-87.25" /><PnL value="0" /><PnL value="" /></div>
        </section>

        <section className="grid-2">
          <div className="card">
            <h2>净值曲线</h2>
            <Sparkline points={SAMPLE_CURVE} label="示例净值曲线" />
          </div>
          <div className="card"><Sparkline points={[]} label="无数据" /></div>
        </section>

        <section>
          <h2>提示条</h2>
          <div className="stack section">
            <Notice tone="info" title="执行中……">正在等待 tradingd 回答。</Notice>
            <Notice tone="ok" title="已执行">撤销开仓与减仓单完成：3 笔已撤。</Notice>
            <Notice tone="warn" title="命令已发出但结果未知" action={<Button kind="primary" small>用同一编号重试（web-…）</Button>}>
              结果未知：命令可能已经执行。请用同一编号重试，tradingd 会返回原结果，不会执行两次。
            </Notice>
            <Notice tone="danger" title="tradingd 不可用，命令未发出">服务器恢复后可以重试；交易核心不受客户端影响。</Notice>
          </div>
        </section>

        <section>
          <h2>表格</h2>
          <div className="table-wrap section">
            <table>
              <thead><tr><th scope="col">标的</th><th scope="col">方向</th><th scope="col" className="num">数量</th>
                <th scope="col" className="num">价格</th><th scope="col">状态</th></tr></thead>
              <tbody>
                <tr><td>SPY</td><td>买入</td><td className="num">10</td><td className="num">512.34</td><td><Tag tone="ok">FILLED</Tag></td></tr>
                <tr><td>QQQ</td><td>卖出</td><td className="num">4</td><td className="num">441.07</td><td><Tag tone="info">SUBMITTED</Tag></td></tr>
                <tr><td>IEF</td><td>卖出</td><td className="num">25</td><td className="num">94.12</td><td><Tag tone="warn">UNKNOWN</Tag></td></tr>
              </tbody>
            </table>
          </div>
          <div className="card section"><Empty title="还没有订单">策略或急停产生的订单会出现在这里</Empty></div>
        </section>

        <section>
          <h2>急停控件（静态外观）</h2>
          <div className="danger-zone section">
            <div className="controls">
              <div className="control"><h3><Icon name="stop" />停止新增风险（HALT）</h3>
                <p className="what">持久化 HALT：拒绝一切新增风险。不撤已有订单，也不撤保护性止损。</p>
                <div className="actions"><Button>停止新增风险（HALT）……</Button></div></div>
              <div className="control high"><h3><Icon name="alert" />撤销保护性止损<span className="tag danger">高危</span></h3>
                <p className="what">撤掉全部保护性止损：之后持仓没有止损保护。</p>
                <div className="actions"><Button kind="danger-outline">撤销保护性止损……</Button></div></div>
              <div className="control high open"><h3><Icon name="alert" />紧急平仓<span className="tag danger">高危</span></h3>
                <p className="what">展开后：核对预览，输入确认短语，再点实心红的确认按钮。</p>
                <input className="input" aria-label="示例确认短语" defaultValue="平仓 p-1" />
                <div className="actions"><Button kind="danger">确认执行</Button><Button kind="ghost">取消</Button></div></div>
            </div>
          </div>
        </section>

        <section>
          <h2>长文案压测（英文）</h2>
          <div className="row section"><Button kind="danger-outline">{LONG_EN}</Button><Tag tone="warn">Data may be stale; the server keeps running</Tag></div>
        </section>
      </main>
    </div>
  );
}
