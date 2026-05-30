export interface Position {
  symbol: string;
  mode: string;
  side: string;
  qty: number;
  avg_price: number;
  status: string;
  last_event_ts: number;
  flash?: "green" | "red" | null;
}

export interface Order {
  exchange_order_id: string;
  mode: string;
  symbol: string;
  side: string;
  order_type: string;
  qty: number;
  filled_qty: number;
  avg_fill_price: number;
  status: string;
  created_at: number;
  flash?: "green" | "red" | null;
}

export interface Fill {
  exchange_order_id: string;
  symbol: string;
  side: string;
  mode: string;
  price: number;
  qty: number;
  fee: number;
  ts: number;
  flash?: "green" | "red" | null;
}

export interface Pnl {
  mode: string;
  fills: number;
  total_fees: number;
  total_qty: number;
  realized_pnl: number;
}

export interface Health {
  status: string;
  db?: string;
  last_event_ts?: number;
  last_event_age_ms?: number | null;
  mode?: string;
}

export interface VenueHealth {
  ok: boolean;
  details?: {
    pnl: number;
    maintenance_margin: number;
    available_wallet_balance: number;
    total_wallet_balance: number;
    total_initial_margin: number;
    available_balance_cross: number;
    margin_ratio_cross: number;
    total_account_equity: number;
    updated_at: number;
  };
  error?: string;
}

export type ConnState = "connecting" | "live" | "reconnecting";

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma?: number;
}

export interface WatchlistItem {
  symbol: string;
  price: number;
  prevPrice: number;
  change24h: number;
  sparkline: number[];
  bias: "BULLISH" | "BEARISH" | "NEUTRAL";
  flashState?: "up" | "down" | null;
}

export interface OrderBookEntry {
  price: number;
  qty: number;
  total: number;
}

export interface TradeEvent {
  event_type: string;
  payload: Record<string, unknown>;
  ts: number;
}

export type TabType = "positions" | "orders" | "fills" | "logs";
export type AppMode = "paper" | "live";
export type OrderSide = "buy" | "sell";
export type OrderType = "market" | "limit";

export interface ToastMessage {
  text: string;
  type: "success" | "danger";
}
