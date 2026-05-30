import { useState, useCallback, useRef, useEffect } from "react";
import type { Position, Order, Fill, Pnl, Health, VenueHealth, TradeEvent, AppMode, ToastMessage } from "@/types";

const INITIAL_POSITIONS: Position[] = [
  { symbol: "BTCUSDT", mode: "paper", side: "LONG", qty: 0.05, avg_price: 67250.0, status: "OPEN", last_event_ts: Date.now() - 3600000 },
  { symbol: "SOLUSDT", mode: "paper", side: "LONG", qty: 2.5, avg_price: 165.3, status: "OPEN", last_event_ts: Date.now() - 7200000 },
];

const INITIAL_ORDERS: Order[] = [
  { exchange_order_id: "ord_1234567890", mode: "paper", symbol: "BTCUSDT", side: "BUY", order_type: "LIMIT", qty: 0.02, filled_qty: 0.005, avg_fill_price: 68100, status: "PARTIALLY_FILLED", created_at: Date.now() - 600000 },
  { exchange_order_id: "ord_0987654321", mode: "paper", symbol: "ETHUSDT", side: "SELL", order_type: "LIMIT", qty: 0.5, filled_qty: 0, avg_fill_price: 0, status: "NEW", created_at: Date.now() - 300000 },
];

const INITIAL_FILLS: Fill[] = [
  { exchange_order_id: "ord_1234567890", symbol: "BTCUSDT", side: "BUY", mode: "paper", price: 68100, qty: 0.005, fee: 0.01362, ts: Date.now() - 580000 },
  { exchange_order_id: "ord_5555555555", symbol: "SOLUSDT", side: "BUY", mode: "paper", price: 165.3, qty: 2.5, fee: 0.08265, ts: Date.now() - 7100000 },
  { exchange_order_id: "ord_7777777777", symbol: "BTCUSDT", side: "BUY", mode: "paper", price: 67250, qty: 0.05, fee: 0.1345, ts: Date.now() - 3500000 },
];

const INITIAL_PNLS: Record<string, Pnl> = {
  paper: { mode: "paper", fills: 3, total_fees: 0.23077, total_qty: 2.555, realized_pnl: -0.23077 },
  live: { mode: "live", fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 },
};

export function useTradingEngine(mode: AppMode) {
  const [positions, setPositions] = useState<Position[]>(INITIAL_POSITIONS);
  const [orders, setOrders] = useState<Order[]>(INITIAL_ORDERS);
  const [fills, setFills] = useState<Fill[]>(INITIAL_FILLS);
  const [pnls, setPnls] = useState<Record<string, Pnl>>(INITIAL_PNLS);
  const [events, setEvents] = useState<TradeEvent[]>([]);
  const [health, setHealth] = useState<Health>({ status: "ok", db: "connected", last_event_ts: Date.now(), mode: "paper" });
  const [venueHealth] = useState<VenueHealth | null>({
    ok: true,
    details: {
      pnl: 0,
      maintenance_margin: 12.45,
      available_wallet_balance: 987.55,
      total_wallet_balance: 1000,
      total_initial_margin: 12.45,
      available_balance_cross: 987.55,
      margin_ratio_cross: 0.012,
      total_account_equity: 1000,
      updated_at: Date.now(),
    }
  });
  const [accountBalance, setAccountBalance] = useState(1000);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const [connState, setConnState] = useState<"connecting" | "live" | "reconnecting">("live");
  const [lastEventAge, setLastEventAge] = useState<number | null>(0);

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((text: string, type: "success" | "danger" = "success") => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast({ text, type });
    toastTimerRef.current = setTimeout(() => setToast(null), 3000) as unknown as ReturnType<typeof setTimeout> | null;
  }, []);

  const triggerFlash = useCallback((id: string, type: "positions" | "orders" | "fills", color: "green" | "red") => {
    if (type === "positions") {
      setPositions(prev => prev.map(p => p.symbol === id ? { ...p, flash: color } : p));
      setTimeout(() => {
        setPositions(prev => prev.map(p => p.symbol === id ? { ...p, flash: null } : p));
      }, 1200);
    } else if (type === "orders") {
      setOrders(prev => prev.map(o => o.exchange_order_id === id ? { ...o, flash: color } : o));
      setTimeout(() => {
        setOrders(prev => prev.map(o => o.exchange_order_id === id ? { ...o, flash: null } : o));
      }, 1200);
    } else if (type === "fills") {
      setFills(prev => prev.map(f => f.exchange_order_id === id ? { ...f, flash: color } : f));
      setTimeout(() => {
        setFills(prev => prev.map(f => f.exchange_order_id === id ? { ...f, flash: null } : f));
      }, 1200);
    }
  }, []);

  // Simulate SSE events
  useEffect(() => {
    const eventTypes = ["ORDER_CREATED", "ORDER_FILLED", "POSITION_OPENED", "POSITION_CLOSED", "FUNDING_APPLIED"];
    const symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"];
    const sides = ["BUY", "SELL"];

    const interval = setInterval(() => {
      if (Math.random() > 0.85) {
        const eventType = eventTypes[Math.floor(Math.random() * eventTypes.length)];
        const symbol = symbols[Math.floor(Math.random() * symbols.length)];
        const side = sides[Math.floor(Math.random() * sides.length)];
        const ts = Math.floor(Date.now() / 1000);

        const event: TradeEvent = {
          event_type: eventType,
          payload: { mode, symbol, side, quantity: Math.random() * 0.1, price: 68000 + Math.random() * 1000 },
          ts,
        };

        setEvents(prev => [event, ...prev].slice(0, 50));
        setHealth(prev => prev ? { ...prev, last_event_ts: ts * 1000 } : { status: "ok", last_event_ts: ts * 1000 });

        if (eventType === "ORDER_CREATED") {
          const newOrder: Order = {
            exchange_order_id: `ord_${Math.random().toString(36).substring(2, 12)}`,
            mode,
            symbol,
            side,
            order_type: "MARKET",
            qty: Number((Math.random() * 0.5).toFixed(4)),
            filled_qty: 0,
            avg_fill_price: 0,
            status: "NEW",
            created_at: ts * 1000,
          };
          setOrders(prev => [newOrder, ...prev].slice(0, 25));
          triggerFlash(newOrder.exchange_order_id, "orders", "green");
          showToast(`Order Created: ${newOrder.side} ${newOrder.qty} ${newOrder.symbol}`, "success");
        }
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [mode, triggerFlash, showToast]);

  // Update age counter
  useEffect(() => {
    const interval = setInterval(() => {
      if (health?.last_event_ts) {
        const age = Date.now() - health.last_event_ts;
        setLastEventAge(age > 0 ? age : 0);
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [health]);

  const currentPnl = pnls[mode] || { mode, fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 };

  return {
    positions,
    setPositions,
    orders,
    setOrders,
    fills,
    setFills,
    pnl: currentPnl,
    setPnls,
    events,
    health,
    venueHealth,
    accountBalance,
    setAccountBalance,
    toast,
    showToast,
    connState,
    setConnState,
    lastEventAge,
    triggerFlash,
  };
}
