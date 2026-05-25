import { createResource, createSignal, onCleanup } from "solid-js";
import { type ConnState, type Mode, getFills, getHealth, getPnl, getPositions, streamEvents } from "./api";
import { ActivityFeed, ConnDot, ModeToggle, PnlCards, PositionsTable } from "./components";

export default function App() {
  const [mode, setMode] = createSignal<Mode>("all");
  const [tick, setTick] = createSignal(0);
  const [conn, setConn] = createSignal<ConnState>("connecting");

  // Resources refetch whenever (mode, tick) changes; SSE events bump `tick`.
  const key = () => ({ mode: mode(), tick: tick() });
  const [positions] = createResource(key, (k) => getPositions(k.mode));
  const [pnl] = createResource(key, (k) => getPnl(k.mode));
  const [fills] = createResource(key, (k) => getFills(k.mode));
  const [health] = createResource(tick, () => getHealth());

  // Realtime: coalesce bursty events into at most one refetch per 300ms.
  let timer: ReturnType<typeof setTimeout> | undefined;
  const bump = () => {
    if (timer) return;
    timer = setTimeout(() => { timer = undefined; setTick((t) => t + 1); }, 300);
  };
  const dispose = streamEvents(bump, setConn);
  const hb = setInterval(() => setTick((t) => t + 1), 5000); // health/heartbeat refresh
  onCleanup(() => { dispose(); clearInterval(hb); if (timer) clearTimeout(timer); });

  return (
    <div class="app">
      <header class="topbar">
        <div class="brand">crypto-trader <span class="muted">dashboard</span></div>
        <ModeToggle mode={mode} setMode={setMode} />
        <ConnDot state={conn} ageMs={() => health()?.last_event_age_ms} />
      </header>
      <main>
        <PnlCards pnl={pnl} />
        <PositionsTable positions={positions} />
        <ActivityFeed fills={fills} />
      </main>
      <footer class="muted">Read-only · realtime via SSE · mode: {health()?.mode ?? "?"}</footer>
    </div>
  );
}
