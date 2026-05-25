import { createSignal, createMemo, For, Show, onMount, onCleanup } from 'solid-js';
import './App.css';

const API_BASE = "http://localhost:8088";
const WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"];

type Mode = "paper" | "live";
type ConnState = "connecting" | "live" | "reconnecting";

interface PositionDetail {
  symbol: string; mode: string; side: string; qty: number; avg_price: number;
  status: string; sl_price: number | null; tp_levels: any;
}
interface Order {
  exchange_order_id: string; symbol: string; side: string; order_type: string;
  qty: number; filled_qty: number; avg_fill_price: number; status: string; created_at: number;
}
interface Fill {
  exchange_order_id: string; symbol: string; side: string;
  price: number; qty: number; fee: number; ts: number;
}
interface Account {
  mode: string; initial_balance: number; realized_pnl: number; unrealized_pnl: number;
  balance: number; equity: number;
  open_positions: { symbol: string; side: string; qty: number; avg_price: number; mark_price: number | null; unrealized_pnl: number | null }[];
}
interface Risk {
  available: boolean; daily_count?: number; daily_pnl?: number; consecutive_losses?: number;
  kill_switch?: boolean; kill_switch_reason?: string | null; peak_balance?: number;
}
interface Gate {
  mode: string; live_enabled: boolean; ack_ok: boolean; halt_file: boolean; live_orders_allowed: boolean;
}

const fmt = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const signClass = (n: number | null | undefined) => (n == null ? "" : n >= 0 ? "pos" : "neg");

async function getJSON<T>(path: string, fallback: T): Promise<T> {
  try {
    const r = await fetch(`${API_BASE}${path}`);
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return await r.json();
  } catch (e) {
    console.error("[API]", path, e);
    return fallback;
  }
}

function App() {
  const [mode, setMode] = createSignal<Mode>("paper");
  const [events, setEvents] = createSignal<any[]>([]);
  const [conn, setConn] = createSignal<ConnState>("connecting");
  const [lastEventTs, setLastEventTs] = createSignal<number | null>(null);

  const [account, setAccount] = createSignal<Account | null>(null);
  const [positions, setPositions] = createSignal<PositionDetail[]>([]);
  const [orders, setOrders] = createSignal<Order[]>([]);
  const [fills, setFills] = createSignal<Fill[]>([]);
  const [risk, setRisk] = createSignal<Risk | null>(null);
  const [gate, setGate] = createSignal<Gate | null>(null);
  const [ltp, setLtp] = createSignal<Record<string, number | null>>({});

  const markBySymbol = createMemo(() => {
    const m: Record<string, number | null> = {};
    account()?.open_positions.forEach(p => { m[p.symbol] = p.mark_price; });
    return m;
  });
  const upnlBySymbol = createMemo(() => {
    const m: Record<string, number | null> = {};
    account()?.open_positions.forEach(p => { m[p.symbol] = p.unrealized_pnl; });
    return m;
  });

  async function refresh() {
    const m = mode();
    const [acc, pos, ord, fil, rsk, gt] = await Promise.all([
      getJSON<Account | null>(`/account?mode=${m}`, null),
      getJSON<PositionDetail[]>(`/positions/detail?mode=${m}`, []),
      getJSON<Order[]>(`/orders?mode=${m}&limit=25`, []),
      getJSON<Fill[]>(`/fills?mode=${m}&limit=25`, []),
      getJSON<Risk | null>(`/risk`, null),
      getJSON<Gate | null>(`/gate`, null),
    ]);
    setAccount(acc); setPositions(pos); setOrders(ord); setFills(fil); setRisk(rsk); setGate(gt);
  }

  async function refreshLtp() {
    setLtp(await getJSON<Record<string, number | null>>(`/ltp?symbols=${WATCHLIST.join(",")}`, {}));
  }

  onMount(() => {
    refresh(); refreshLtp();
    const t1 = setInterval(refresh, 5000);
    const t2 = setInterval(refreshLtp, 4000);

    const es = new EventSource(`${API_BASE}/events/stream`);
    es.onopen = () => setConn("live");
    es.onerror = () => setConn("reconnecting");
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        setEvents(prev => [data, ...prev].slice(0, 80));
        setLastEventTs(data.ts || (data.timestamp ? data.timestamp * 1000 : Date.now()));
        const et = (data.event_type || "").toUpperCase();
        if (/ORDER|TRADE|POSITION|FILL|LIQUID|FUNDING|KILL/.test(et)) refresh();
      } catch { /* heartbeat */ }
    };
    onCleanup(() => { clearInterval(t1); clearInterval(t2); es.close(); });
  });

  const visibleEvents = createMemo(() =>
    events().filter(e => !e.payload?.mode || e.payload.mode === mode()));

  const eventAge = () => lastEventTs() ? `${Math.round((Date.now() - lastEventTs()!) / 1000)}s ago` : "—";
  const fmtTime = (e: any) => {
    const v = e.ts || (e.timestamp ? e.timestamp * 1000 : null);
    return v ? new Date(v).toLocaleTimeString() : "—";
  };
  const tpStr = (tp: any) => {
    if (!tp) return "—";
    try {
      const arr = Array.isArray(tp) ? tp : JSON.parse(tp);
      return arr.map((t: any) => fmt(Number(t.price), 4)).join(" / ");
    } catch { return "—"; }
  };

  return (
    <div class="app-container">
      <header class="app-header">
        <div class="brand-section">
          <div class="logo"><h2>CRYPTO TRADER</h2></div>
          <span class="version-badge">v4 · binance→coindcx</span>
        </div>
        <div class="controls-section">
          <div class="mode-selector">
            <button class={`mode-tab ${mode() === 'paper' ? 'active-paper' : ''}`} onClick={() => { setMode("paper"); refresh(); }}>PAPER</button>
            <button class={`mode-tab ${mode() === 'live' ? 'active-live' : ''}`} onClick={() => { setMode("live"); refresh(); }}>LIVE</button>
          </div>
          <div class="connection-status">
            <span class={`status-dot ${conn()}`} />
            <span class="status-text">{conn()}</span>
            <span class="age-divider">·</span>
            <span class="last-seen">bot {eventAge()}</span>
          </div>
        </div>
      </header>

      {/* Risk / live-gate health banner */}
      <Show when={risk() || gate()}>
        <div class="metrics-grid" style="grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); margin-bottom: 1rem;">
          <div class="metric-card" classList={{ 'flash-red': risk()?.kill_switch }}>
            <span class="metric-label">Kill Switch</span>
            <span class={`metric-value ${risk()?.kill_switch ? 'neg' : 'pos'}`} style="font-size:1.1rem;">
              {risk()?.kill_switch ? `TRIPPED` : 'CLEAR'}
            </span>
            <Show when={risk()?.kill_switch}><span class="muted">{risk()?.kill_switch_reason}</span></Show>
          </div>
          <div class="metric-card">
            <span class="metric-label">Live Orders</span>
            <span class={`metric-value ${gate()?.live_orders_allowed ? 'neg' : 'pos'}`} style="font-size:1.1rem;">
              {gate()?.live_orders_allowed ? 'ENABLED' : 'BLOCKED'}
            </span>
            <span class="muted">
              env:{gate()?.live_enabled ? 'on' : 'off'} ack:{gate()?.ack_ok ? 'ok' : 'no'} halt:{gate()?.halt_file ? 'yes' : 'no'}
            </span>
          </div>
          <div class="metric-card">
            <span class="metric-label">Daily Trades</span>
            <span class="metric-value" style="font-size:1.1rem;">{risk()?.daily_count ?? '—'}</span>
          </div>
          <div class="metric-card">
            <span class="metric-label">Consec. Losses</span>
            <span class="metric-value" style="font-size:1.1rem;">{risk()?.consecutive_losses ?? '—'}</span>
          </div>
        </div>
      </Show>

      {/* LTP ticker */}
      <div class="metrics-grid" style="grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); margin-bottom: 1rem;">
        <For each={WATCHLIST}>
          {(s) => (
            <div class="metric-card">
              <span class="metric-label">{s}</span>
              <span class="metric-value" style="font-size:1.3rem;">{fmt(ltp()[s], 4)}</span>
            </div>
          )}
        </For>
      </div>

      {/* Account metrics */}
      <div class="metrics-grid">
        <div class="metric-card pnl-card">
          <div class="card-glow" />
          <span class="metric-label">Equity</span>
          <span class="metric-value">{fmt(account()?.equity)}</span>
        </div>
        <div class="metric-card">
          <span class="metric-label">Balance</span>
          <span class="metric-value">{fmt(account()?.balance)}</span>
        </div>
        <div class="metric-card">
          <span class="metric-label">Realized PnL</span>
          <span class={`metric-value ${signClass(account()?.realized_pnl)}`}>{fmt(account()?.realized_pnl)}</span>
        </div>
        <div class="metric-card">
          <span class="metric-label">Unrealized PnL</span>
          <span class={`metric-value ${signClass(account()?.unrealized_pnl)}`}>{fmt(account()?.unrealized_pnl)}</span>
        </div>
        <div class="metric-card">
          <span class="metric-label">Open Positions</span>
          <span class="metric-value">{positions().filter(p => Number(p.qty) > 0).length}</span>
        </div>
      </div>

      <div class="dashboard-layout">
        <div class="main-column">
          {/* Positions */}
          <div class="content-panel">
            <div class="panel-header">
              <h3>Open Positions</h3>
              <span class="count-tag">{positions().filter(p => Number(p.qty) > 0).length}</span>
            </div>
            <div class="table-container">
              <table>
                <thead><tr>
                  <th>Symbol</th><th>Side</th><th>Qty</th><th>Avg</th><th>Mark</th><th>uPnL</th><th>SL</th><th>TP</th>
                </tr></thead>
                <tbody>
                  <For each={positions().filter(p => Number(p.qty) > 0)}>
                    {(p) => (
                      <tr>
                        <td class="bold">{p.symbol}</td>
                        <td><span class={`side-pill ${p.side?.toLowerCase()}`}>{p.side}</span></td>
                        <td>{fmt(p.qty, 4)}</td>
                        <td>{fmt(p.avg_price, 4)}</td>
                        <td>{fmt(markBySymbol()[p.symbol], 4)}</td>
                        <td class={signClass(upnlBySymbol()[p.symbol])}>{fmt(upnlBySymbol()[p.symbol])}</td>
                        <td class="muted">{fmt(p.sl_price, 4)}</td>
                        <td class="muted">{tpStr(p.tp_levels)}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
              <Show when={positions().filter(p => Number(p.qty) > 0).length === 0}>
                <div class="empty-state">Flat — no open positions.</div>
              </Show>
            </div>
          </div>

          {/* Orders */}
          <div class="content-panel">
            <div class="panel-header"><h3>Recent Orders</h3><span class="count-tag">{orders().length}</span></div>
            <div class="table-container">
              <table>
                <thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Qty</th><th>Filled</th><th>Avg Fill</th><th>Status</th></tr></thead>
                <tbody>
                  <For each={orders()}>
                    {(o) => (
                      <tr>
                        <td class="bold">{o.symbol}</td>
                        <td><span class={`side-pill ${o.side?.toLowerCase()}`}>{o.side}</span></td>
                        <td class="muted">{o.order_type}</td>
                        <td>{fmt(o.qty, 4)}</td>
                        <td>{fmt(o.filled_qty, 4)}</td>
                        <td>{fmt(o.avg_fill_price, 4)}</td>
                        <td><span class={`order-status-badge status-${o.status?.toLowerCase()}`}>{o.status}</span></td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
              <Show when={orders().length === 0}><div class="empty-state">No recent orders.</div></Show>
            </div>
          </div>
        </div>

        <div class="side-column">
          {/* Fills */}
          <div class="content-panel">
            <div class="panel-header"><h3>Recent Fills</h3><span class="count-tag">{fills().length}</span></div>
            <div class="table-container">
              <table>
                <thead><tr><th>Symbol</th><th>Side</th><th>Price</th><th>Qty</th><th>Fee</th></tr></thead>
                <tbody>
                  <For each={fills()}>
                    {(f) => (
                      <tr>
                        <td class="bold">{f.symbol}</td>
                        <td><span class={`side-pill ${String(f.side).toLowerCase()}`}>{f.side}</span></td>
                        <td>{fmt(f.price, 4)}</td>
                        <td>{fmt(f.qty, 4)}</td>
                        <td class="fee-text">{fmt(f.fee, 4)}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
              <Show when={fills().length === 0}><div class="empty-state">No fills yet.</div></Show>
            </div>
          </div>

          {/* Live events */}
          <div class="content-panel">
            <div class="panel-header"><h3>Live Events</h3><span class="count-tag">{visibleEvents().length}</span></div>
            <div class="terminal-logs">
              <Show when={visibleEvents().length} fallback={<div class="terminal-empty muted">Waiting for events…</div>}>
                <For each={visibleEvents()}>
                  {(e) => (
                    <div class="terminal-row">
                      <span class="term-time">[{fmtTime(e)}]</span>{" "}
                      <span class="term-type">{e.event_type || 'Event'}</span>
                      <pre class="term-payload">{JSON.stringify(e.payload ?? e, (k, v) =>
                        (k === 'ts' || k === 'timestamp' || k === 'event_type' || k === 'namespace') ? undefined : v, 2)}</pre>
                    </div>
                  )}
                </For>
              </Show>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
