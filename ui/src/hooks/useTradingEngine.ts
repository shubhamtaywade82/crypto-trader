import { useState, useCallback, useRef, useEffect } from "react";
import type { Position, Order, Fill, Pnl, Health, VenueHealth, TradeEvent, AppMode, ToastMessage, ConnState } from "@/types";

// Dynamic API endpoints resolution
const isDev = typeof window !== "undefined" && (
  window.location.port === "3030" || 
  window.location.port === "5173" || 
  window.location.port === "3000"
);
const API_BASE = isDev ? "http://localhost:8088" : "";
const API_PREFIX = isDev ? "" : "/api";
const STREAM_URL = isDev ? "http://localhost:8088/events/stream" : "/api/stream";

const getUrl = (path: string) => `${API_BASE}${API_PREFIX}${path}`;

export function useTradingEngine(mode: AppMode) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [pnl, setPnl] = useState<Pnl>({ mode, fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 });
  const [events, setEvents] = useState<TradeEvent[]>([]);
  const [health, setHealth] = useState<Health>({ status: "disconnected" });
  const [venueHealth, setVenueHealth] = useState<VenueHealth | null>(null);
  const [accountBalance, setAccountBalance] = useState<number>(1000);
  const [accountEquity, setAccountEquity] = useState<number>(1000);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const [connState, setConnState] = useState<ConnState>("connecting");
  const [lastEventAge, setLastEventAge] = useState<number | null>(null);
  const [gate, setGate] = useState<any>(null);
  const [risk, setRisk] = useState<any>(null);
  const [watchlistSymbols, setWatchlistSymbols] = useState<string[]>([]);

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sseRef = useRef<EventSource | null>(null);

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

  const syncState = useCallback(async () => {
    console.log(`[Dashboard] Re-syncing state for mode: ${mode}`);
    try {
      const [positionsRes, ordersRes, fillsRes, pnlRes, healthRes, accountRes, gateRes, riskRes, watchlistRes] = await Promise.all([
        fetch(getUrl(`/positions?mode=${mode}`)).then(r => r.json() as Promise<Position[]>),
        fetch(getUrl(`/orders?mode=${mode}&limit=25`)).then(r => r.json() as Promise<Order[]>),
        fetch(getUrl(`/fills?mode=${mode}&limit=25`)).then(r => r.json() as Promise<Fill[]>),
        fetch(getUrl(`/pnl?mode=${mode}`)).then(r => r.json() as Promise<any>),
        fetch(getUrl("/health")).then(r => r.json() as Promise<Health>),
        fetch(getUrl(`/account?mode=${mode}`)).then(r => r.json() as Promise<any>),
        fetch(getUrl("/gate")).then(r => r.json() as Promise<any>).catch(() => null),
        fetch(getUrl("/risk")).then(r => r.json() as Promise<any>).catch(() => null),
        fetch(getUrl("/watchlist")).then(r => r.json() as Promise<any>).catch(() => null),
      ]);

      // If live mode, fetch venue snapshots
      let venueRes = null;
      if (mode === "live") {
        venueRes = await fetch(getUrl("/venue/account")).then(r => r.json() as Promise<any>).catch(() => null);
        try {
          const vhRes = await fetch(getUrl("/venue/health")).then(r => r.json() as Promise<VenueHealth>);
          setVenueHealth(vhRes);
        } catch (e) {
          console.warn("Failed to fetch venue health:", e);
        }
      } else {
        setVenueHealth(null);
      }

      setGate(gateRes);
      setRisk(riskRes);
      if (watchlistRes && Array.isArray(watchlistRes.watchlist)) {
        setWatchlistSymbols(watchlistRes.watchlist);
      }

      // Set positions and merge venue ones if in live mode
      let finalPositions: Position[] = [];
      if (Array.isArray(positionsRes)) {
        finalPositions = [...positionsRes.filter(p => Number(p.qty || 0) > 0)];
      }

      if (mode === "live" && venueRes && venueRes.ok && venueRes.positions) {
        const venuePosMap = venueRes.positions;
        Object.keys(venuePosMap).forEach(sym => {
          const vp = venuePosMap[sym];
          const qty = Number(vp.quantity || 0);
          if (qty > 0) {
            const existingIdx = finalPositions.findIndex(p => p.symbol === sym);
            const mappedPos: Position = {
              symbol: sym,
              mode: "live",
              side: String(vp.side).toUpperCase(),
              qty: qty,
              avg_price: Number(vp.entry_price || vp.avg_price || 0),
              status: "OPEN",
              last_event_ts: Date.now()
            };
            if (existingIdx >= 0) {
              finalPositions[existingIdx] = mappedPos;
            } else {
              finalPositions.push(mappedPos);
            }
          }
        });
      }
      setPositions(finalPositions);

      // Set orders and merge venue ones if in live mode
      let finalOrders: Order[] = [];
      if (Array.isArray(ordersRes)) {
        finalOrders = [...ordersRes];
      }

      if (mode === "live" && venueRes && venueRes.ok && Array.isArray(venueRes.open_orders)) {
        venueRes.open_orders.forEach((vo: any) => {
          const oid = vo.exchange_order_id;
          const existingIdx = finalOrders.findIndex(o => o.exchange_order_id === oid);
          const mappedOrder: Order = {
            exchange_order_id: oid,
            mode: "live",
            symbol: vo.symbol,
            side: String(vo.side).toUpperCase(),
            order_type: String(vo.order_type || vo.stage || "LIMIT").toUpperCase(),
            qty: Number(vo.quantity || 0),
            filled_qty: Number(vo.filled_quantity || 0),
            avg_fill_price: 0.0,
            status: String(vo.status || "NEW").toUpperCase(),
            created_at: Date.now()
          };
          if (existingIdx >= 0) {
            finalOrders[existingIdx] = mappedOrder;
          } else {
            finalOrders.unshift(mappedOrder);
          }
        });
      }
      setOrders(finalOrders);

      if (Array.isArray(fillsRes)) {
        setFills(fillsRes);
      }

      setHealth(healthRes);

      if (accountRes) {
        if (accountRes.balance !== undefined) {
          setAccountBalance(Number(accountRes.balance));
        }
        if (accountRes.equity !== undefined) {
          setAccountEquity(Number(accountRes.equity));
        }
      }

      // Handle both formats of PnL: Record<string, Pnl> or Pnl directly
      if (pnlRes) {
        if (pnlRes.mode !== undefined) {
          setPnl(pnlRes as Pnl);
        } else if (pnlRes[mode] !== undefined) {
          setPnl(pnlRes[mode] as Pnl);
        } else {
          setPnl({ mode, fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 });
        }
      }

      // Update age of last event
      if (healthRes.last_event_ts) {
        const age = Date.now() - healthRes.last_event_ts;
        setLastEventAge(age > 0 ? age : 0);
      } else {
        setLastEventAge(null);
      }
    } catch (err) {
      console.error("[Dashboard] Error syncing state from API:", err);
    }
  }, [mode]);

  useEffect(() => {
    let active = true;

    const connectSSE = () => {
      if (!active) return;
      console.log(`[Dashboard] Connecting to SSE stream: ${STREAM_URL}`);
      setConnState("connecting");

      if (sseRef.current) {
        sseRef.current.close();
      }

      const sse = new EventSource(STREAM_URL);
      sseRef.current = sse;

      sse.onopen = () => {
        if (!active) return;
        console.log("✅ SSE Connection Established");
        setConnState("live");
        syncState();
      };

      sse.onerror = (err) => {
        if (!active) return;
        console.error("❌ SSE Connection Error, auto-reconnecting:", err);
        setConnState("reconnecting");
      };

      sse.onmessage = (e) => {
        if (!active) return;
        try {
          const raw = JSON.parse(e.data);
          const { event_type, payload, ts } = raw;
          if (!event_type) return;

          // Append raw event to logging panel
          setEvents(prev => [raw, ...prev].slice(0, 50));

          // Set bot health heartbeat timestamp
          if (ts) {
            setHealth(prev => ({
              ...prev,
              status: "ok",
              last_event_ts: ts * 1000
            }));
          }

          // Check mode filter: ignore event if it doesn't align with UI mode selection
          const eventMode = payload?.mode;
          if (eventMode && eventMode !== mode) {
            return;
          }

          console.log(`[Dashboard] Applying real-time event: ${event_type}`, payload);

          // Apply state changes based on event type or trigger complete sync
          if (event_type === "ORDER_CREATED") {
            const oid = payload.order_id || payload.exchange_order_id;
            const newOrder: Order = {
              exchange_order_id: oid,
              mode: payload.mode || mode,
              symbol: payload.symbol,
              side: payload.side,
              order_type: payload.order_type || "MARKET",
              qty: Number(payload.quantity || 0),
              filled_qty: 0,
              avg_fill_price: 0,
              status: payload.status || "NEW",
              created_at: ts ? ts * 1000 : Date.now(),
            };
            setOrders(prev => {
              const filtered = prev.filter(o => o.exchange_order_id !== oid);
              return [newOrder, ...filtered].slice(0, 25);
            });
            triggerFlash(oid, "orders", "green");
            showToast(`Order Created: ${newOrder.side} ${newOrder.qty} ${newOrder.symbol}`, "success");
          } 
          else if (event_type === "ORDER_FILLED" || event_type === "ORDER_PARTIALLY_FILLED") {
            const oid = payload.order_id || payload.exchange_order_id;
            const fillQty = Number(payload.quantity || 0);
            const fillPrice = Number(payload.fill_price || payload.price || 0);

            setOrders(prev => prev.map(o => {
              if (o.exchange_order_id === oid) {
                return {
                  ...o,
                  filled_qty: o.filled_qty + fillQty,
                  avg_fill_price: fillPrice,
                  status: event_type === "ORDER_FILLED" ? "FILLED" : "PARTIALLY_FILLED"
                };
              }
              return o;
            }));
            triggerFlash(oid, "orders", "green");
            syncState(); // Re-sync accounts, positions and fills
          }
          else {
            // For other events (POSITION_OPENED, POSITION_CLOSED, etc.), trigger full sync
            syncState();
          }
        } catch (err) {
          console.error("Error processing SSE message:", err);
        }
      };
    };

    connectSSE();

    // Fallback polling for status check
    const pollInterval = setInterval(() => {
      syncState();
    }, 10000);

    return () => {
      active = false;
      clearInterval(pollInterval);
      if (sseRef.current) {
        sseRef.current.close();
      }
    };
  }, [mode, syncState, triggerFlash, showToast]);

  // Update event age counter
  useEffect(() => {
    const interval = setInterval(() => {
      if (health?.last_event_ts) {
        const age = Date.now() - health.last_event_ts;
        setLastEventAge(age > 0 ? age : 0);
      }
    }, 1000);
    return () => clearInterval(interval);
  }, [health]);

  return {
    positions,
    orders,
    fills,
    pnl,
    events,
    health,
    venueHealth,
    accountBalance,
    accountEquity,
    toast,
    connState,
    lastEventAge,
    gate,
    risk,
    watchlistSymbols,
    syncState,
    showToast,
    triggerFlash,
  };
}
