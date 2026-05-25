// Read-only client for the dashboard API + realtime SSE.

export type Mode = "all" | "live" | "paper";

export interface Position {
  symbol: string; mode: string; side: string; qty: number;
  avg_price: number; status: string; last_event_ts: number;
}
export interface Order {
  exchange_order_id: string; mode: string; symbol: string; side: string;
  order_type: string; qty: number; filled_qty: number; avg_fill_price: number;
  status: string; created_at: number;
}
export interface Fill {
  exchange_order_id: string; symbol: string; side: string; mode: string;
  price: number; qty: number; fee: number; ts: number;
}
export type PnlByMode = Record<string, { mode: string; fills: number; total_fees: number; total_qty: number }>;
export interface Health { status: string; db?: string; last_event_age_ms?: number | null; mode?: string; }

const q = (mode: Mode) => (mode === "all" ? "" : `?mode=${mode}`);
const j = async <T,>(url: string): Promise<T> => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
};

export const getHealth = () => j<Health>("/api/health");
export const getPositions = (m: Mode) => j<Position[]>(`/api/positions${q(m)}`);
export const getOrders = (m: Mode) => j<Order[]>(`/api/orders${q(m)}${q(m) ? "&" : "?"}limit=25`);
export const getFills = (m: Mode) => j<Fill[]>(`/api/fills${q(m)}${q(m) ? "&" : "?"}limit=25`);
export const getPnl = (m: Mode) => j<PnlByMode>(`/api/pnl${q(m)}`);

export type ConnState = "connecting" | "live" | "reconnecting";

// Subscribe to realtime bot events. Returns a disposer.
export function streamEvents(onEvent: (e: any) => void, onState: (s: ConnState) => void): () => void {
  onState("connecting");
  const es = new EventSource("/api/stream");
  es.onopen = () => onState("live");
  es.onerror = () => onState("reconnecting"); // EventSource auto-reconnects
  es.onmessage = (m) => {
    try { onEvent(JSON.parse(m.data)); } catch { /* ignore heartbeats */ }
  };
  return () => es.close();
}
