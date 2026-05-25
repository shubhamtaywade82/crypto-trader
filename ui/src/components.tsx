import { For, Show, type Accessor } from "solid-js";
import type { Position, Fill, PnlByMode, Mode, ConnState } from "./api";

const fmt = (n: number, d = 2) => (n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const ago = (ms?: number | null) => (ms == null ? "—" : ms < 1000 ? "now" : `${Math.round(ms / 1000)}s ago`);

export function ModeToggle(props: { mode: Accessor<Mode>; setMode: (m: Mode) => void }) {
  const opts: Mode[] = ["all", "live", "paper"];
  return (
    <div class="toggle">
      <For each={opts}>
        {(o) => (
          <button class={`toggle-btn ${props.mode() === o ? "active" : ""}`} onClick={() => props.setMode(o)}>
            {o}
          </button>
        )}
      </For>
    </div>
  );
}

export function ConnDot(props: { state: Accessor<ConnState>; ageMs: Accessor<number | null | undefined> }) {
  const cls = () => (props.state() === "live" ? "ok" : props.state() === "connecting" ? "warn" : "err");
  return (
    <div class="conn">
      <span class={`dot ${cls()}`} />
      <span>{props.state()}</span>
      <span class="muted">· bot {ago(props.ageMs())}</span>
    </div>
  );
}

export function PnlCards(props: { pnl: Accessor<PnlByMode | undefined> }) {
  const entries = () => Object.values(props.pnl() ?? {});
  return (
    <div class="cards">
      <Show when={entries().length} fallback={<div class="card muted">No fills yet</div>}>
        <For each={entries()}>
          {(p) => (
            <div class="card">
              <div class="card-tag">{p.mode}</div>
              <div class="card-row"><span>Fills</span><b>{p.fills}</b></div>
              <div class="card-row"><span>Fees</span><b>{fmt(p.total_fees, 4)}</b></div>
              <div class="card-row"><span>Volume</span><b>{fmt(p.total_qty, 4)}</b></div>
            </div>
          )}
        </For>
      </Show>
    </div>
  );
}

export function PositionsTable(props: { positions: Accessor<Position[] | undefined> }) {
  return (
    <div class="panel">
      <h2>Open Positions</h2>
      <Show when={(props.positions() ?? []).length} fallback={<p class="muted">Flat — no open positions.</p>}>
        <table>
          <thead><tr><th>Symbol</th><th>Mode</th><th>Side</th><th>Qty</th><th>Avg Price</th><th>Status</th></tr></thead>
          <tbody>
            <For each={props.positions()}>
              {(p) => (
                <tr>
                  <td>{p.symbol}</td>
                  <td><span class={`pill ${p.mode}`}>{p.mode}</span></td>
                  <td class={p.side === "LONG" ? "long" : "short"}>{p.side}</td>
                  <td>{fmt(p.qty, 4)}</td>
                  <td>{fmt(p.avg_price)}</td>
                  <td>{p.status}</td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </div>
  );
}

export function ActivityFeed(props: { fills: Accessor<Fill[] | undefined> }) {
  return (
    <div class="panel">
      <h2>Recent Fills</h2>
      <Show when={(props.fills() ?? []).length} fallback={<p class="muted">No fills yet.</p>}>
        <table>
          <thead><tr><th>Symbol</th><th>Mode</th><th>Side</th><th>Price</th><th>Qty</th><th>Fee</th></tr></thead>
          <tbody>
            <For each={props.fills()}>
              {(f) => (
                <tr>
                  <td>{f.symbol}</td>
                  <td><span class={`pill ${f.mode}`}>{f.mode}</span></td>
                  <td class={String(f.side).toUpperCase().includes("L") || f.side === "buy" ? "long" : "short"}>{f.side}</td>
                  <td>{fmt(f.price)}</td>
                  <td>{fmt(f.qty, 4)}</td>
                  <td>{fmt(f.fee, 4)}</td>
                </tr>
              )}
            </For>
          </tbody>
        </table>
      </Show>
    </div>
  );
}
