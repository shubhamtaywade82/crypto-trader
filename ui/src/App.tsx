import { createSignal, For, onMount, onCleanup, createEffect, Show } from 'solid-js';
import { createChart, CandlestickSeries, HistogramSeries } from 'lightweight-charts';
import type { IChartApi, ISeriesApi, UTCTimestamp, IPriceLine } from 'lightweight-charts';
import './App.css';

// ── types & interfaces ──────────────────────────────────────────────────────
interface Position {
  symbol: string;
  mode: string;
  side: string;
  qty: number;
  avg_price: number;
  status: string;
  last_event_ts: number;
  flash?: "green" | "red" | null;
}

interface Order {
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

interface Fill {
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

interface Pnl {
  mode: string;
  fills: number;
  total_fees: number;
  total_qty: number;
  realized_pnl: number;
}

interface Health {
  status: string;
  db?: string;
  last_event_ts?: number;
  last_event_age_ms?: number | null;
  mode?: string;
}

interface VenueHealth {
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

type ConnState = "connecting" | "live" | "reconnecting";

interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma?: number;
}

interface WatchlistItem {
  symbol: string;
  price: number;
  prevPrice: number;
  change24h: number;
  sparkline: number[];
  bias: "BULLISH" | "BEARISH" | "NEUTRAL";
  flashState?: "up" | "down" | null;
}

function App() {
  const [mode, setMode] = createSignal<"paper" | "live">("paper");
  const [connState, setConnState] = createSignal<ConnState>("connecting");
  const [lastEventAge, setLastEventAge] = createSignal<number | null>(null);
  
  // Local reactive state stores
  const [positions, setPositions] = createSignal<Position[]>([]);
  const [orders, setOrders] = createSignal<Order[]>([]);
  const [fills, setFills] = createSignal<Fill[]>([]);
  const [pnl, setPnl] = createSignal<Pnl>({ mode: "paper", fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 });
  const [events, setEvents] = createSignal<any[]>([]);
  const [health, setHealth] = createSignal<Health | null>(null);
  const [venueHealth, setVenueHealth] = createSignal<VenueHealth | null>(null);
  const [accountBalance, setAccountBalance] = createSignal<number>(1000.0);

  // Active terminal context
  const [selectedSymbol, setSelectedSymbol] = createSignal<string>("BTCUSDT");
  const [timeframe, setTimeframe] = createSignal<string>("5m");
  const [chartData, setChartData] = createSignal<Candle[]>([]);
  const [hoveredCandle, setHoveredCandle] = createSignal<Candle | null>(null);
  const [activeTab, setActiveTab] = createSignal<"positions" | "orders" | "fills" | "logs">("positions");

  let chartContainerRef: HTMLDivElement | undefined;
  let chartInstance: IChartApi | undefined;
  let candleSeries: ISeriesApi<"Candlestick"> | undefined;
  let volumeSeries: ISeriesApi<"Histogram"> | undefined;
  let activePriceLine: IPriceLine | undefined;

  // Order placement state
  const [orderSide, setOrderSide] = createSignal<"buy" | "sell">("buy");
  const [orderType, setOrderType] = createSignal<"market" | "limit">("market");
  const [orderPrice, setOrderPrice] = createSignal<string>("");
  const [orderQty, setOrderQty] = createSignal<string>("0.1");
  const [leverage, setLeverage] = createSignal<number>(20);
  const [toast, setToast] = createSignal<{ text: string; type: "success" | "danger" } | null>(null);

  // Watchlist state
  const [watchlist, setWatchlist] = createSignal<WatchlistItem[]>([
    { symbol: "BTCUSDT", price: 68420.50, prevPrice: 68420.50, change24h: 1.25, sparkline: [68000, 68150, 68080, 68250, 68400, 68350, 68420], bias: "BULLISH" },
    { symbol: "ETHUSDT", price: 3822.40, prevPrice: 3822.40, change24h: -0.45, sparkline: [3850, 3840, 3810, 3830, 3820, 3825, 3822], bias: "NEUTRAL" },
    { symbol: "SOLUSDT", price: 178.65, prevPrice: 178.65, change24h: 3.80, sparkline: [170, 172, 171, 174, 176, 175, 178.65], bias: "BULLISH" },
    { symbol: "XRPUSDT", price: 0.5422, prevPrice: 0.5422, change24h: -1.12, sparkline: [0.552, 0.548, 0.545, 0.546, 0.541, 0.543, 0.5422], bias: "BEARISH" },
    { symbol: "DOGEUSDT", price: 0.1502, prevPrice: 0.1502, change24h: 5.18, sparkline: [0.141, 0.143, 0.142, 0.147, 0.148, 0.149, 0.1502], bias: "BULLISH" },
    { symbol: "ADAUSDT", price: 0.4850, prevPrice: 0.4850, change24h: -0.85, sparkline: [0.492, 0.489, 0.488, 0.490, 0.484, 0.486, 0.4850], bias: "BEARISH" },
    { symbol: "LINKUSDT", price: 15.65, prevPrice: 15.65, change24h: 2.12, sparkline: [15.2, 15.3, 15.25, 15.4, 15.6, 15.55, 15.65], bias: "BULLISH" }
  ]);

  // Order Book state (mocked real-time bid/ask depths)
  const [bids, setBids] = createSignal<{ price: number; qty: number; total: number }[]>([]);
  const [asks, setAsks] = createSignal<{ price: number; qty: number; total: number }[]>([]);

  // ── dynamic api endpoints resolution ──────────────────────────────────────
  const isDev = window.location.port === "3030" || window.location.port === "5173" || window.location.port === "3000";
  const API_BASE = isDev ? "http://localhost:8088" : "";
  const API_PREFIX = isDev ? "" : "/api";
  const STREAM_URL = isDev ? "http://localhost:8088/events/stream" : "/api/stream";

  const getUrl = (path: string) => `${API_BASE}${API_PREFIX}${path}`;

  // Helper to trigger temporary flash highlight on rows
  const triggerFlash = (id: string, type: "positions" | "orders" | "fills", color: "green" | "red") => {
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
  };

  const showToast = (text: string, type: "success" | "danger" = "success") => {
    setToast({ text, type });
    setTimeout(() => setToast(null), 3000);
  };

  // ── fetch klines from binance or fallback ────────────────────────────────
  const fetchKlines = async (sym: string, tf: string) => {
    try {
      const binanceInterval = tf === "1d" ? "1d" : tf === "1h" ? "1h" : tf === "15m" ? "15m" : tf === "5m" ? "5m" : "1m";
      const response = await fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=${sym}&interval=${binanceInterval}&limit=80`);
      
      if (!response.ok) throw new Error("Binance API call failed");
      
      const rawKlines = await response.json();
      const parsed: Candle[] = rawKlines.map((k: any) => ({
        time: Number(k[0]),
        open: Number(k[1]),
        high: Number(k[2]),
        low: Number(k[3]),
        close: Number(k[4]),
        volume: Number(k[5])
      }));

      // Calculate Simple Moving Average (MA 20)
      for (let i = 0; i < parsed.length; i++) {
        if (i >= 19) {
          const sum = parsed.slice(i - 19, i + 1).reduce((acc, c) => acc + c.close, 0);
          parsed[i].ma = sum / 20;
        }
      }

      setChartData(parsed);
      
      // Prefill order form price if limit order
      const lastCandle = parsed[parsed.length - 1];
      if (lastCandle && orderPrice() === "") {
        setOrderPrice(lastCandle.close.toString());
      }
    } catch (e) {
      console.warn("[Chart] Fetching binance klines failed:", e);
      setChartData([]);
    }
  };

  const fetchLiveWatchlistData = async () => {
    const activeSym = selectedSymbol();
    const symbols = watchlist().map(w => w.symbol);
    try {
      const results = await Promise.all(
        symbols.map(sym => 
          fetch(`https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=${sym}`)
            .then(r => {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .catch(() => null)
        )
      );

      setWatchlist(prev => prev.map((w, idx) => {
        const data = results[idx];
        if (!data || data.code !== undefined || data.lastPrice == null) return w;
        
        const newPrice = Number(data.lastPrice);
        const direction = newPrice > w.price ? "up" : newPrice < w.price ? "down" : null;
        
        let newSpark = w.sparkline;
        if (newPrice !== w.price) {
          newSpark = [...w.sparkline.slice(1), newPrice];
        }

        if (w.symbol === activeSym) {
          // Push a minor real-time tick to the chart data
          setChartData(cData => {
            if (cData.length === 0) return cData;
            const updated = [...cData];
            const last = { ...updated[updated.length - 1] };
            
            last.close = newPrice;
            if (newPrice > last.high) last.high = newPrice;
            if (newPrice < last.low) last.low = newPrice;
            
            updated[updated.length - 1] = last;
            return updated;
          });

          // Fetch actual live order book
          fetchLiveOrderBook(activeSym);
        }

        return {
          ...w,
          price: newPrice,
          prevPrice: w.price,
          change24h: Number(data.priceChangePercent),
          sparkline: newSpark,
          flashState: direction
        };
      }));
    } catch (e) {
      console.warn("Failed to fetch live watchlist data from Binance:", e);
    }
  };

  const fetchWatchlistSparklines = async () => {
    const symbols = watchlist().map(w => w.symbol);
    try {
      const results = await Promise.all(
        symbols.map(sym => 
          fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=${sym}&interval=1h&limit=10`)
            .then(r => {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then(data => data.map((k: any) => Number(k[4])))
            .catch(() => [])
        )
      );

      setWatchlist(prev => prev.map((w, idx) => {
        const spark = results[idx];
        return {
          ...w,
          sparkline: spark && spark.length > 0 ? spark : w.sparkline
        };
      }));
    } catch (e) {
      console.warn("Failed to fetch watchlist sparklines from Binance:", e);
    }
  };

  const fetchVenueHealth = async () => {
    if (mode() !== "live") return;
    try {
      const res = await fetch(getUrl("/venue/health"));
      const data = await res.json() as VenueHealth;
      setVenueHealth(data);
    } catch (e) {
      console.warn("Failed to fetch venue health:", e);
    }
  };

  // Fetch real-time Order Book depth from Binance
  const fetchLiveOrderBook = async (sym: string) => {
    try {
      const response = await fetch(`https://fapi.binance.com/fapi/v1/depth?symbol=${sym}&limit=10`);
      if (!response.ok) throw new Error();
      const data = await response.json();
      
      let bidAccum = 0;
      const parsedBids = data.bids.slice(0, 8).map((b: any) => {
        const price = Number(b[0]);
        const qty = Number(b[1]);
        bidAccum += qty;
        return { price, qty, total: Number(bidAccum.toFixed(3)) };
      });

      let askAccum = 0;
      const parsedAsks = data.asks.slice(0, 8).map((a: any) => {
        const price = Number(a[0]);
        const qty = Number(a[1]);
        askAccum += qty;
        return { price, qty, total: Number(askAccum.toFixed(3)) };
      });

      setBids(parsedBids);
      setAsks(parsedAsks.reverse()); // Asks shown highest to lowest at spread
    } catch (e) {
      console.warn("Failed to fetch live order book from Binance:", e);
    }
  };

  // ── http fetchers for synchronization ──────────────────────────────────────
  const syncState = async () => {
    const m = mode();
    console.log(`[Dashboard] Re-syncing state for mode: ${m}`);
    try {
      const [positionsRes, ordersRes, fillsRes, pnlRes, healthRes, accountRes, venueRes] = await Promise.all([
        fetch(getUrl(`/positions?mode=${m}`)).then(r => r.json() as Promise<Position[]>),
        fetch(getUrl(`/orders?mode=${m}&limit=25`)).then(r => r.json() as Promise<Order[]>),
        fetch(getUrl(`/fills?mode=${m}&limit=25`)).then(r => r.json() as Promise<Fill[]>),
        fetch(getUrl(`/pnl?mode=${m}`)).then(r => r.json() as Promise<Record<string, Pnl> | Pnl>),
        fetch(getUrl("/health")).then(r => r.json() as Promise<Health>),
        fetch(getUrl(`/account?mode=${m}`)).then(r => r.json() as Promise<any>),
        m === "live"
          ? fetch(getUrl("/venue/account")).then(r => r.json() as Promise<any>).catch(() => null)
          : Promise.resolve(null)
      ]);

      // Set positions and merge venue ones if in live mode
      let finalPositions: Position[] = [];
      if (Array.isArray(positionsRes)) {
        finalPositions = [...positionsRes.filter(p => p.qty > 0)];
      }

      if (m === "live" && venueRes && venueRes.ok && venueRes.positions) {
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

      if (m === "live" && venueRes && venueRes.ok && Array.isArray(venueRes.open_orders)) {
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

      if (accountRes && accountRes.balance !== undefined) {
        setAccountBalance(Number(accountRes.balance));
      }

      // Handle both formats of PnL: Record<string, Pnl> or Pnl directly
      if (pnlRes) {
        const res = pnlRes as any;
        if (res.mode !== undefined) {
          setPnl(res as Pnl);
        } else if (res[m] !== undefined) {
          setPnl(res[m] as Pnl);
        } else {
          setPnl({ mode: m, fills: 0, total_fees: 0, total_qty: 0, realized_pnl: 0 });
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
  };

  // Sync state when mode changes
  createEffect(() => {
    syncState();
  });

  // Keep track of bot age counters and live price ticks in UI
  onMount(() => {
    // Re-sync ticker prices and klines periodically
    const feedInterval = setInterval(() => {
      const sym = selectedSymbol();
      // Periodic fetch for active symbol candles
      fetchKlines(sym, timeframe());
      
      // Fetch live prices and update watchlist
      fetchLiveWatchlistData();

      // Fetch live order book
      fetchLiveOrderBook(sym);

      // Fetch venue health if live
      fetchVenueHealth();

      // Age updates
      const h = health();
      if (h && h.last_event_ts) {
        const age = Date.now() - h.last_event_ts;
        setLastEventAge(age > 0 ? age : 0);
      }
    }, 2000);

    onCleanup(() => clearInterval(feedInterval));
  });

  // Initial load
  onMount(() => {
    fetchLiveWatchlistData();
    fetchWatchlistSparklines();
    fetchKlines(selectedSymbol(), timeframe());
    fetchLiveOrderBook(selectedSymbol());
  });

  // Watch for active symbol change to reload chart
  createEffect(() => {
    const sym = selectedSymbol();
    const activeItem = watchlist().find(w => w.symbol === sym);
    if (activeItem) {
      setOrderPrice(activeItem.price.toString());
      fetchKlines(sym, timeframe());
      fetchLiveOrderBook(sym);
    }
  });

  // ── realtime event reducer ────────────────────────────────────────────────
  onMount(() => {
    let eventSource: EventSource;

    const connectSSE = () => {
      console.log(`[Dashboard] Connecting to SSE stream: ${STREAM_URL}`);
      setConnState("connecting");
      
      eventSource = new EventSource(STREAM_URL);

      eventSource.onopen = () => {
        console.log("✅ SSE Connection Established");
        setConnState("live");
        syncState();
      };

      eventSource.onerror = (err) => {
        console.error("❌ SSE Connection Error, auto-reconnecting:", err);
        setConnState("reconnecting");
      };

      eventSource.onmessage = (e) => {
        try {
          const raw = JSON.parse(e.data);
          const { event_type, payload, ts } = raw;
          if (!event_type) return;

          // Append raw event to logging panel
          setEvents(prev => [raw, ...prev].slice(0, 50));

          // Set bot health heartbeat timestamp
          if (ts) {
            setHealth(prev => prev ? { ...prev, last_event_ts: ts * 1000 } : { status: "ok", last_event_ts: ts * 1000 });
          }

          // Check mode filter: ignore event if it doesn't align with UI mode selection
          const eventMode = payload?.mode;
          if (eventMode && eventMode !== mode()) {
            return;
          }

          console.log(`[Dashboard] Applying real-time event: ${event_type}`, payload);

          // ── order updates ──
          if (event_type === "ORDER_CREATED") {
            const newOrder: Order = {
              exchange_order_id: payload.order_id,
              mode: payload.mode || mode(),
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
              const filtered = prev.filter(o => o.exchange_order_id !== newOrder.exchange_order_id);
              return [newOrder, ...filtered].slice(0, 25);
            });
            triggerFlash(newOrder.exchange_order_id, "orders", "green");
            showToast(`Order Created: ${newOrder.side} ${newOrder.qty} ${newOrder.symbol}`, "success");
          } 
          else if (event_type === "ORDER_FILLED" || event_type === "ORDER_PARTIALLY_FILLED") {
            const fillQty = Number(payload.quantity || 0);
            const fillPrice = Number(payload.fill_price || 0);
            const fee = Number(payload.fee || 0);
            const oid = payload.order_id;

            setOrders(prev => prev.map(o => {
              if (o.exchange_order_id === oid) {
                const updatedFilled = o.filled_qty + fillQty;
                return {
                  ...o,
                  filled_qty: updatedFilled,
                  avg_fill_price: fillPrice,
                  status: payload.status || (event_type === "ORDER_FILLED" ? "FILLED" : "PARTIALLY_FILLED")
                };
              }
              return o;
            }));
            triggerFlash(oid, "orders", "green");

            const newFill: Fill = {
              exchange_order_id: oid,
              symbol: payload.symbol,
              side: payload.side,
              mode: payload.mode || mode(),
              price: fillPrice,
              qty: fillQty,
              fee: fee,
              ts: ts ? ts * 1000 : Date.now()
            };
            setFills(prev => [newFill, ...prev].slice(0, 25));
            triggerFlash(oid, "fills", "green");

            // Update real-time metrics
            setPnl(prev => ({
              ...prev,
              fills: prev.fills + 1,
              total_fees: prev.total_fees + fee,
              total_qty: prev.total_qty + fillQty,
              realized_pnl: prev.realized_pnl - fee
            }));
            
            showToast(`Order Filled: ${newFill.side} ${newFill.qty} ${newFill.symbol} @ ${newFill.price}`, "success");
          } 
          else if (event_type === "ORDER_CANCELLED" || event_type === "ORDER_REJECTED" || event_type === "ORDER_EXPIRED") {
            const oid = payload.order_id;
            const newStatus = event_type.replace("ORDER_", "");
            setOrders(prev => prev.map(o => {
              if (o.exchange_order_id === oid) {
                return { ...o, status: newStatus };
              }
              return o;
            }));
            triggerFlash(oid, "orders", "red");
            showToast(`Order Status: ${newStatus}`, "danger");
          }

          // ── position updates ──
          else if (event_type === "POSITION_OPENED") {
            const newPos: Position = {
              symbol: payload.symbol,
              mode: payload.mode || mode(),
              side: payload.side,
              qty: Number(payload.quantity || 0),
              avg_price: Number(payload.execution_price || payload.entry_price || 0),
              status: "OPEN",
              last_event_ts: ts ? ts * 1000 : Date.now(),
            };
            setPositions(prev => {
              const filtered = prev.filter(p => p.symbol !== newPos.symbol);
              return [...filtered, newPos];
            });
            triggerFlash(newPos.symbol, "positions", "green");
          } 
          else if (event_type === "POSITION_PARTIALLY_CLOSED") {
            const closedQty = Number(payload.closed_qty || 0);
            const realizedPnlDelta = Number(payload.pnl || payload.remaining_pnl || 0);
            const fee = Number(payload.fee || 0);
            const sym = payload.symbol;

            setPositions(prev => prev.map(p => {
              if (p.symbol === sym) {
                const newQty = Math.max(0, p.qty - closedQty);
                return {
                  ...p,
                  qty: newQty,
                  last_event_ts: ts ? ts * 1000 : Date.now()
                };
              }
              return p;
            }).filter(p => p.qty > 0));

            triggerFlash(sym, "positions", "red");

            // Accumulate realized PnL
            setPnl(prev => ({
              ...prev,
              realized_pnl: prev.realized_pnl + (realizedPnlDelta - fee)
            }));
          } 
          else if (event_type === "POSITION_CLOSED" || event_type === "LIQUIDATION") {
            const sym = payload.symbol;
            const realizedPnlDelta = Number(payload.remaining_pnl || 0);
            const fee = Number(payload.fee || 0);

            // Remove position
            setPositions(prev => prev.filter(p => p.symbol !== sym));

            // Accumulate realized PnL
            setPnl(prev => ({
              ...prev,
              realized_pnl: prev.realized_pnl + (realizedPnlDelta - fee)
            }));
            showToast(`Position Closed for ${sym}. PnL: ${realizedPnlDelta >= 0 ? '+' : ''}${realizedPnlDelta.toFixed(2)}`, realizedPnlDelta >= 0 ? "success" : "danger");
          }
          else if (event_type === "RECON_ADJUST") {
            const sym = payload.symbol;
            const qty = Number(payload.quantity || 0);
            if (qty <= 0) {
              setPositions(prev => prev.filter(p => p.symbol !== sym));
            } else {
              const correctedPos: Position = {
                symbol: sym,
                mode: payload.mode || mode(),
                side: payload.side,
                qty: qty,
                avg_price: Number(payload.avg_price || 0),
                status: "OPEN",
                last_event_ts: ts ? ts * 1000 : Date.now()
              };
              setPositions(prev => {
                const filtered = prev.filter(p => p.symbol !== sym);
                return [...filtered, correctedPos];
              });
              triggerFlash(sym, "positions", "green");
            }
          }

          // ── financial balances ──
          else if (event_type === "FUNDING_APPLIED") {
            const amount = Number(payload.amount || 0);
            setPnl(prev => ({
              ...prev,
              realized_pnl: prev.realized_pnl + amount
            }));
          }
          else if (event_type === "FEE_CHARGED") {
            const amount = Number(payload.amount || 0);
            setPnl(prev => ({
              ...prev,
              realized_pnl: prev.realized_pnl - amount
            }));
          }
        } catch (err) {
          console.error("[Dashboard] Error processing event message:", err);
        }
      };
    };

    connectSSE();

    onCleanup(() => {
      console.log("[Dashboard] Cleaning up EventSource connection");
      if (eventSource) eventSource.close();
    });
  });

  // ── formatter helpers ─────────────────────────────────────────────────────
  const formatNum = (num: number, dec = 2) => {
    if (num == null || isNaN(num)) return "0.00";
    return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  };

  const formatAge = (ms: number | null) => {
    if (ms == null) return "—";
    if (ms < 1000) return "now";
    const sec = Math.round(ms / 1000);
    if (sec < 60) return `${sec}s ago`;
    return `${Math.round(sec / 60)}m ago`;
  };

  const getSparklinePath = (points: number[], width = 60, height = 18) => {
    if (points.length < 2) return "";
    const min = Math.min(...points);
    const max = Math.max(...points);
    const range = max - min || 1;
    return points.map((p, i) => {
      const x = (i / (points.length - 1)) * width;
      const y = height - ((p - min) / range) * height;
      return `${i === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
    }).join(" ");
  };

  // ── order and position handlers (manual entry is locked on engine dashboard) ────────────────

  // Calculate live Unrealized PnL based on current ticker prices
  const unrealizedPnL = () => {
    let total = 0;
    positions().forEach(p => {
      const livePrice = watchlist().find(w => w.symbol === p.symbol)?.price;
      if (livePrice && p.qty > 0) {
        const sign = p.side === "LONG" || p.side === "BUY" ? 1 : -1;
        total += (livePrice - p.avg_price) * p.qty * sign;
      }
    });
    return total;
  };

  const realizedBalance = () => accountBalance();
  const equity = () => realizedBalance() + unrealizedPnL();

  // Initialize TradingView Lightweight Charts on mount
  onMount(() => {
    if (!chartContainerRef) return;

    // Create the lightweight chart instance
    chartInstance = createChart(chartContainerRef, {
      layout: {
        background: { color: '#0b0e14' },
        textColor: '#94a3b8',
        fontSize: 11,
        fontFamily: "Inter, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: '#161c28', style: 1 },
        horzLines: { color: '#161c28', style: 1 },
      },
      crosshair: {
        mode: 1, // Normal crosshair
        vertLine: {
          color: '#3b82f6',
          width: 1,
          style: 3, // Dotted
          labelBackgroundColor: '#1e293b',
        },
        horzLine: {
          color: '#3b82f6',
          width: 1,
          style: 3, // Dotted
          labelBackgroundColor: '#3b82f6',
        },
      },
      rightPriceScale: {
        borderColor: '#1b2230',
        scaleMargins: {
          top: 0.1,
          bottom: 0.25,
        },
      },
      timeScale: {
        borderColor: '#1b2230',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    // Add Candlestick Series using lightweight-charts v5 addSeries API
    candleSeries = chartInstance.addSeries(CandlestickSeries, {
      upColor: '#00e676',
      downColor: '#ff3d00',
      borderUpColor: '#00e676',
      borderDownColor: '#ff3d00',
      wickUpColor: '#00e676',
      wickDownColor: '#ff3d00',
    });

    // Add Volume Series overlay using lightweight-charts v5 addSeries API
    volumeSeries = chartInstance.addSeries(HistogramSeries, {
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: '', // Overlay pane
    });

    volumeSeries?.priceScale().applyOptions({
      scaleMargins: {
        top: 0.8, // 80% from top (overlay at bottom 20%)
        bottom: 0,
      },
    });

    // ResizeObserver to resize chart dynamically
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !chartInstance) return;
      const { width, height } = entries[0].contentRect;
      chartInstance.resize(width, height);
    });

    resizeObserver.observe(chartContainerRef);

    // Crosshair movement listener to update HUD details
    chartInstance.subscribeCrosshairMove((param) => {
      if (!param || !param.time || param.point === undefined || !candleSeries || !volumeSeries) {
        setHoveredCandle(null);
        return;
      }

      const rawCandle = param.seriesData.get(candleSeries);
      const rawVolumeObj = param.seriesData.get(volumeSeries);

      if (rawCandle) {
        const candleData = rawCandle as any;
        const volumeData = rawVolumeObj as any;
        
        setHoveredCandle({
          time: Number(param.time) * 1000,
          open: Number(candleData.open),
          high: Number(candleData.high),
          low: Number(candleData.low),
          close: Number(candleData.close),
          volume: volumeData ? Number(volumeData.value) : 0,
        });
      } else {
        setHoveredCandle(null);
      }
    });

    onCleanup(() => {
      resizeObserver.disconnect();
      if (chartInstance) {
        chartInstance.remove();
        chartInstance = undefined;
        candleSeries = undefined;
        volumeSeries = undefined;
      }
    });
  });

  // Track the last loaded symbol and interval to fit content only on change
  let lastSymbolAndTf = "";

  // Sync historical & real-time klines with chart series
  createEffect(() => {
    const data = chartData();
    const sym = selectedSymbol();
    const tf = timeframe();
    
    if (!candleSeries || !volumeSeries || data.length === 0) return;

    // Convert millisecond timestamps to second timestamps for lightweight-charts
    const formattedCandles = data.map((c) => ({
      time: (c.time / 1000) as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    const formattedVolume = data.map((c) => ({
      time: (c.time / 1000) as UTCTimestamp,
      value: c.volume,
      color: c.close >= c.open ? 'rgba(0, 230, 118, 0.25)' : 'rgba(255, 61, 0, 0.25)',
    }));

    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedVolume);

    const currentKey = `${sym}-${tf}`;
    if (currentKey !== lastSymbolAndTf) {
      chartInstance?.timeScale().fitContent();
      lastSymbolAndTf = currentKey;
    }
  });

  // Dynamically render active position average entry price line
  createEffect(() => {
    const sym = selectedSymbol();
    const posList = positions();
    
    if (!candleSeries) return;

    if (activePriceLine) {
      candleSeries.removePriceLine(activePriceLine);
      activePriceLine = undefined;
    }

    const activePos = posList.find(p => p.symbol === sym && p.status.toUpperCase() === "OPEN");

    if (activePos && activePos.avg_price > 0) {
      const isLong = activePos.side.toLowerCase() === "buy" || activePos.side.toLowerCase() === "long";
      const lineConfig = {
        price: activePos.avg_price,
        color: isLong ? '#00e676' : '#ff3d00',
        lineWidth: 1.5 as any,
        lineStyle: 2, // Dashed
        axisLabelVisible: true,
        title: `POSITION (${activePos.side.toUpperCase()}) - ${activePos.qty} qty`,
      };
      
      activePriceLine = candleSeries.createPriceLine(lineConfig);
    }
  });

  // Selected symbol ticker helper
  const selectedTicker = () => watchlist().find(w => w.symbol === selectedSymbol()) || watchlist()[0];

  return (
    <div class="app-container">
      {/* ── HEADER RIBBON ─────────────────────────────────────────────────── */}
      <header class="app-header">
        <div class="brand-section">
          <div class="logo">
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2.5" fill="none">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <h2>ANTIGRAVITY</h2>
          </div>
          <span class="version-badge">v4.0 PRO</span>
        </div>

        {/* Dynamic Account Ribbon */}
        <div class="header-metrics-ribbon">
          <div class="header-metric-item">
            <span class="header-metric-label">Equity (USDT)</span>
            <span class="header-metric-value">{formatNum(equity(), 2)}</span>
          </div>
          <div class="header-metric-item">
            <span class="header-metric-label">Unrealized PnL</span>
            <span class={`header-metric-value ${unrealizedPnL() >= 0 ? 'pos' : 'neg'}`}>
              {unrealizedPnL() >= 0 ? '+' : ''}{formatNum(unrealizedPnL(), 2)}
            </span>
          </div>
          <div class="header-metric-item">
            <span class="header-metric-label">Realized PnL</span>
            <span class={`header-metric-value ${pnl().realized_pnl >= 0 ? 'pos' : 'neg'}`}>
              {pnl().realized_pnl >= 0 ? '+' : ''}{formatNum(pnl().realized_pnl, 2)}
            </span>
          </div>
          <div class="header-metric-item">
            <span class="header-metric-label">Fees Paid</span>
            <span class="header-metric-value" style="color: var(--text-secondary);">{formatNum(pnl().total_fees, 4)}</span>
          </div>
          <div class="header-metric-item">
            <span class="header-metric-label">Active Trades</span>
            <span class="header-metric-value" style="color: #60a5fa;">{positions().length}</span>
          </div>
        </div>

        <div class="controls-section">
          {/* Paper / Live Toggle */}
          <div class="mode-selector">
            <button 
              class={`mode-tab ${mode() === 'paper' ? 'active-paper' : ''}`}
              onClick={() => setMode("paper")}
            >
              Paper
            </button>
            <button 
              class={`mode-tab ${mode() === 'live' ? 'active-live' : ''}`}
              onClick={() => setMode("live")}
            >
              Live
            </button>
          </div>

          {/* Connection status */}
          <div class="connection-status">
            <span class={`status-dot ${connState()}`} />
            <span class="status-text">{connState() === 'live' ? 'connected' : connState()}</span>
            <span class="last-seen">BOT: {formatAge(lastEventAge())}</span>
          </div>
        </div>
      </header>

      {/* ── THREE-COLUMN TERMINAL LAYOUT ───────────────────────────────────── */}
      <div class="dashboard-layout">
        
        {/* COLUMN 1: WATCHLIST & ACCOUNT WIDGET */}
        <aside class="left-sidebar">
          <div class="panel-header">
            <h3>Markets Watchlist</h3>
            <span class="version-badge">{watchlist().length} Symbols</span>
          </div>
          
          <div class="watchlist-items">
            <For each={watchlist()}>
              {(coin) => {
                const flashClass = () => {
                  if (coin.flashState === "up") return "flash-up";
                  if (coin.flashState === "down") return "flash-down";
                  return "";
                };
                return (
                  <div 
                    class={`watchlist-row ${selectedSymbol() === coin.symbol ? 'selected' : ''}`}
                    onClick={() => setSelectedSymbol(coin.symbol)}
                  >
                    <div class="watchlist-sym">{coin.symbol}</div>
                    
                    <div class={`watchlist-price ${flashClass()} ${coin.price >= coin.prevPrice ? 'up' : 'down'}`}>
                      {coin.price.toFixed(coin.price > 10 ? 2 : 4)}
                    </div>
                    
                    <div class={`watchlist-change ${coin.change24h >= 0 ? 'up' : 'down'}`}>
                      {coin.change24h >= 0 ? '+' : ''}{coin.change24h.toFixed(2)}%
                    </div>
                    
                    <div class={`watchlist-sparkline ${coin.change24h >= 0 ? 'up' : 'down'}`}>
                      <svg width="60" height="18">
                        <path d={getSparklinePath(coin.sparkline)} />
                      </svg>
                    </div>
                  </div>
                );
              }}
            </For>
          </div>

          {/* Detailed Account Balance breakdown */}
          <div class="account-info-widget">
            <div class="panel-header" style="background: transparent; border: none; padding: 0; height: auto; min-height: 0;">
              <h3 style="font-size: 9px; color: var(--text-secondary);">Wallet Breakdowns</h3>
            </div>
            <div class="account-row">
              <span class="account-label">Margin Balance:</span>
              <span class="account-val">{formatNum(realizedBalance(), 2)} USDT</span>
            </div>
            <div class="account-row">
              <span class="account-label">Unrealized PnL:</span>
              <span class={`account-val ${unrealizedPnL() >= 0 ? 'up' : 'down'}`}>
                {unrealizedPnL() >= 0 ? '+' : ''}{formatNum(unrealizedPnL(), 2)} USDT
              </span>
            </div>
            <div class="account-row" style="border-top: 1px solid var(--border-subtle); padding-top: 4px; margin-top: 2px;">
              <span class="account-label" style="font-weight: 700;">Account Equity:</span>
              <span class="account-val" style="color: var(--text-bright);">{formatNum(equity(), 2)} USDT</span>
            </div>
          </div>

          {/* Authoritative Venue Health (Live only) */}
          <Show when={mode() === 'live'}>
            <div class="account-info-widget" style="margin-top: 1rem; border-color: var(--color-warn-subtle);">
              <div class="panel-header" style="background: transparent; border: none; padding: 0; height: auto; min-height: 0; margin-bottom: 4px;">
                <h3 style="font-size: 9px; color: var(--color-warn);">Authoritative Venue Health</h3>
              </div>
              <Show when={venueHealth()?.ok} fallback={<div class="empty-state" style="font-size: 9px; padding: 4px;">{venueHealth()?.error || 'Fetching venue metrics...'}</div>}>
                <div class="account-row">
                  <span class="account-label">Venue PnL:</span>
                  <span class={`account-val ${Number(venueHealth()?.details?.pnl || 0) >= 0 ? 'up' : 'down'}`}>
                    {formatNum(venueHealth()?.details?.pnl || 0, 4)} USDT
                  </span>
                </div>
                <div class="account-row">
                  <span class="account-label">Margin Ratio:</span>
                  <span class={`account-val ${Number(venueHealth()?.details?.margin_ratio_cross || 0) > 0.8 ? 'down' : 'up'}`}>
                    {(Number(venueHealth()?.details?.margin_ratio_cross || 0) * 100).toFixed(2)}%
                  </span>
                </div>
                <div class="account-row">
                  <span class="account-label">Maint. Margin:</span>
                  <span class="account-val">{formatNum(venueHealth()?.details?.maintenance_margin || 0, 4)} USDT</span>
                </div>
                <div class="account-row">
                  <span class="account-label">Total Equity:</span>
                  <span class="account-val" style="color: var(--text-bright);">{formatNum(venueHealth()?.details?.total_account_equity || 0, 2)} USDT</span>
                </div>
                <div class="account-row" style="margin-top: 4px; border-top: 1px solid var(--border-subtle); padding-top: 2px;">
                  <span class="account-label" style="font-size: 7px; color: var(--text-muted);">Last Sync:</span>
                  <span class="account-val" style="font-size: 7px; color: var(--text-muted);">
                    {venueHealth()?.details?.updated_at ? new Date(venueHealth()!.details!.updated_at).toLocaleTimeString() : '—'}
                  </span>
                </div>
              </Show>
            </div>
          </Show>
        </aside>

        {/* COLUMN 2: ACTIVE INTERACTIVE CHART & TABBED WORKSTATION */}
        <main class="center-column">
          {/* High-Fidelity Candlestick Chart */}
          <section class="chart-panel">
            <div class="chart-header">
              <div class="active-symbol-info">
                <span class="active-sym-title">{selectedSymbol()}</span>
                <span class={`active-sym-price ${selectedTicker().price >= selectedTicker().prevPrice ? 'up' : 'down'}`}>
                  {selectedTicker().price.toFixed(selectedTicker().price > 10 ? 2 : 4)}
                </span>
                <span class={`active-sym-change ${selectedTicker().change24h >= 0 ? 'up' : 'down'}`}>
                  {selectedTicker().change24h >= 0 ? '+' : ''}{selectedTicker().change24h.toFixed(2)}%
                </span>
              </div>
              
              <div class="chart-timeframes">
                <For each={["1m", "5m", "15m", "1h", "1d"]}>
                  {(tf) => (
                    <button 
                      class={`timeframe-btn ${timeframe() === tf ? 'active' : ''}`}
                      onClick={() => setTimeframe(tf)}
                    >
                      {tf}
                    </button>
                  )}
                </For>
              </div>
            </div>

            {/* Interactive SVG Chart Canvas */}
            <div class="chart-draw-area" style="position: relative;">
              {/* Chart HUD */}
              <div class="hud-overlay">
                <Show when={hoveredCandle() || chartData()[chartData().length - 1]} fallback={<span>Loading Chart Data...</span>}>
                  {(candle) => (
                    <>
                      <div class="hud-item">O: <strong>{candle().open.toFixed(selectedSymbol().includes("XRP") ? 4 : 2)}</strong></div>
                      <div class="hud-item">H: <strong>{candle().high.toFixed(selectedSymbol().includes("XRP") ? 4 : 2)}</strong></div>
                      <div class="hud-item">L: <strong>{candle().low.toFixed(selectedSymbol().includes("XRP") ? 4 : 2)}</strong></div>
                      <div class="hud-item">C: <strong>{candle().close.toFixed(selectedSymbol().includes("XRP") ? 4 : 2)}</strong></div>
                      <div class="hud-item">V: <strong>{formatNum(candle().volume, 0)}</strong></div>
                    </>
                  )}
                </Show>
              </div>

              {/* Fallback Loader */}
              <Show when={chartData().length === 0}>
                <div class="empty-row-state" style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 10;">
                  Loading Chart Data...
                </div>
              </Show>

              {/* Lightweight Chart Container */}
              <div 
                ref={chartContainerRef} 
                style="width: 100%; height: 100%; position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 1;"
              ></div>
            </div>
          </section>

          {/* WORKSTATION GRID TABLES PANEL */}
          <section class="tabbed-workspace">
            <div class="workspace-tabs-header">
              <button 
                class={`workspace-tab ${activeTab() === 'positions' ? 'active' : ''}`}
                onClick={() => setActiveTab("positions")}
              >
                Positions <span class="tab-badge">{positions().length}</span>
              </button>
              <button 
                class={`workspace-tab ${activeTab() === 'orders' ? 'active' : ''}`}
                onClick={() => setActiveTab("orders")}
              >
                Open Orders <span class="tab-badge">{orders().filter(o => o.status === 'NEW').length}</span>
              </button>
              <button 
                class={`workspace-tab ${activeTab() === 'fills' ? 'active' : ''}`}
                onClick={() => setActiveTab("fills")}
              >
                Trade History <span class="tab-badge">{fills().length}</span>
              </button>
              <button 
                class={`workspace-tab ${activeTab() === 'logs' ? 'active' : ''}`}
                onClick={() => setActiveTab("logs")}
              >
                System Logs
              </button>
            </div>

            <div class="tab-content">
              {/* POSITIONS TAB */}
              <Show when={activeTab() === 'positions'}>
                <div class="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Size (Qty)</th>
                        <th>Entry Price</th>
                        <th>Mark Price</th>
                        <th>Unrealized PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={positions()} fallback={
                        <tr>
                          <td colspan="6" class="empty-row-state">No open positions. Portfolio flat.</td>
                        </tr>
                      }>
                        {(p) => {
                          const livePrice = () => watchlist().find(w => w.symbol === p.symbol)?.price || p.avg_price;
                          const sign = () => p.side === "LONG" || p.side === "BUY" ? 1 : -1;
                          const uPnl = () => (livePrice() - p.avg_price) * p.qty * sign();
                          
                          return (
                            <tr class={p.flash ? `flash-row-${p.flash}` : ''}>
                              <td class="bold-sans">{p.symbol}</td>
                              <td>
                                <span class={`side-pill ${p.side.toLowerCase()}`}>
                                  {p.side}
                                </span>
                              </td>
                              <td>{p.qty.toFixed(4)}</td>
                              <td>{p.avg_price.toFixed(p.avg_price > 10 ? 2 : 4)}</td>
                              <td>{livePrice().toFixed(livePrice() > 10 ? 2 : 4)}</td>
                              <td class={uPnl() >= 0 ? "watchlist-price up" : "watchlist-price down"}>
                                {uPnl() >= 0 ? '+' : ''}{uPnl().toFixed(2)} USDT
                              </td>
                            </tr>
                          );
                        }}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>

              {/* ORDERS TAB */}
              <Show when={activeTab() === 'orders'}>
                <div class="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>Order ID</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Type</th>
                        <th>Qty</th>
                        <th>Filled</th>
                        <th>Status</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={orders()} fallback={
                        <tr>
                          <td colspan="8" class="empty-row-state">No open orders.</td>
                        </tr>
                      }>
                        {(o) => (
                          <tr class={o.flash ? `flash-row-${o.flash}` : ''}>
                            <td class="bold-sans">{o.exchange_order_id.substring(0, 10)}...</td>
                            <td>{o.symbol}</td>
                            <td>
                              <span class={`side-pill ${o.side.toLowerCase()}`}>
                                {o.side}
                              </span>
                            </td>
                            <td>{o.order_type}</td>
                            <td>{o.qty.toFixed(4)}</td>
                            <td>{o.filled_qty.toFixed(4)}</td>
                            <td>
                              <span class={`status-pill ${o.status.toLowerCase()}`}>
                                {o.status}
                              </span>
                            </td>
                            <td class="fee-col">{new Date(o.created_at).toLocaleTimeString()}</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>

              {/* TRADE HISTORY TAB */}
              <Show when={activeTab() === 'fills'}>
                <div class="table-wrapper">
                  <table>
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Execution Price</th>
                        <th>Executed Qty</th>
                        <th>Fee Paid</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={fills()} fallback={
                        <tr>
                          <td colspan="6" class="empty-row-state">No execution logs.</td>
                        </tr>
                      }>
                        {(f) => (
                          <tr class={f.flash ? `flash-row-${f.flash}` : ''}>
                            <td class="fee-col">{new Date(f.ts).toLocaleTimeString()}</td>
                            <td class="bold-sans">{f.symbol}</td>
                            <td>
                              <span class={`side-pill ${f.side.toLowerCase().includes('l') || f.side.toLowerCase() === 'buy' ? 'buy' : 'sell'}`}>
                                {f.side}
                              </span>
                            </td>
                            <td>{f.price.toFixed(f.price > 10 ? 2 : 4)}</td>
                            <td>{f.qty.toFixed(4)}</td>
                            <td class="fee-col">{f.fee.toFixed(4)} USDT</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>

              {/* LOGS TERMINAL TAB */}
              <Show when={activeTab() === 'logs'}>
                <div class="terminal-wrapper">
                  <For each={events()} fallback={
                    <div class="empty-row-state">Streaming engine log feeds...</div>
                  }>
                    {(ev) => {
                      const logClass = () => {
                        if (ev.event_type.includes("FILL")) return "term-type fill";
                        if (ev.event_type.includes("CLOSED")) return "term-type close";
                        if (ev.event_type.includes("ERROR") || ev.event_type.includes("REJECTED")) return "term-type warn";
                        return "term-type";
                      };
                      return (
                        <div class="terminal-row">
                          <span class="term-time">[{new Date(ev.ts * 1000).toLocaleTimeString()}]</span>{" "}
                          <span class={logClass()}>{ev.event_type}</span>
                          <pre class="term-payload">
                            {JSON.stringify(ev.payload, (key, value) => {
                              if (key === 'mode' || key === 'symbol') return undefined;
                              return value;
                            }, 2)}
                          </pre>
                        </div>
                      );
                    }}
                  </For>
                </div>
              </Show>
            </div>
          </section>
        </main>

        {/* COLUMN 3: ORDER ENTRY & SIMULATED ORDER BOOK */}
        <aside class="right-sidebar">
          
          {/* Order Placement Form */}
          <section class="order-entry-panel">
            <div class="order-type-tabs">
              <button 
                class={`order-side-btn ${orderSide() === 'buy' ? 'buy-active' : ''}`}
                onClick={() => setOrderSide("buy")}
              >
                Buy / Long
              </button>
              <button 
                class={`order-side-btn ${orderSide() === 'sell' ? 'sell-active' : ''}`}
                onClick={() => setOrderSide("sell")}
              >
                Sell / Short
              </button>
            </div>

            <form class="order-form-body" onSubmit={(e) => e.preventDefault()}>
              {/* Limit / Market Switch */}
              <div class="mode-selector" style="width: 100%;">
                <button 
                  type="button"
                  style="flex: 1;"
                  class={`mode-tab ${orderType() === 'market' ? 'active-paper' : ''}`}
                  onClick={() => setOrderType("market")}
                >
                  Market
                </button>
                <button 
                  type="button"
                  style="flex: 1;"
                  class={`mode-tab ${orderType() === 'limit' ? 'active-paper' : ''}`}
                  onClick={() => setOrderType("limit")}
                >
                  Limit
                </button>
              </div>

              {/* Price Field */}
              <Show when={orderType() === 'limit'}>
                <div class="form-row">
                  <span class="form-label">Limit Price</span>
                  <div class="input-container">
                    <input 
                      type="text" 
                      class="input-field" 
                      value={orderPrice()} 
                      onInput={(e) => setOrderPrice(e.currentTarget.value)}
                    />
                    <span class="input-suffix">USDT</span>
                  </div>
                </div>
              </Show>

              {/* Quantity Field */}
              <div class="form-row">
                <span class="form-label">Order Quantity</span>
                <div class="input-container">
                  <input 
                    type="text" 
                    class="input-field" 
                    value={orderQty()} 
                    onInput={(e) => setOrderQty(e.currentTarget.value)}
                  />
                  <span class="input-suffix">{selectedSymbol().replace("USDT", "")}</span>
                </div>
              </div>

              {/* Leverage Slider */}
              <div class="slider-group">
                <div class="slider-labels">
                  <span class="form-label">Leverage</span>
                  <span class="account-val" style="color: var(--color-warn);">{leverage()}x</span>
                </div>
                <input 
                  type="range" 
                  min="1" 
                  max="125" 
                  value={leverage()} 
                  onInput={(e) => setLeverage(Number(e.currentTarget.value))}
                  style="width: 100%; cursor: pointer;"
                />
              </div>

              {/* Balance % shortcuts */}
              <div class="percentage-badge-row">
                <div class="pct-badge" onClick={() => {
                  const price = orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price;
                  if (price > 0) setOrderQty(((realizedBalance() * 0.25 * leverage()) / price).toFixed(3));
                }}>25%</div>
                <div class="pct-badge" onClick={() => {
                  const price = orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price;
                  if (price > 0) setOrderQty(((realizedBalance() * 0.50 * leverage()) / price).toFixed(3));
                }}>50%</div>
                <div class="pct-badge" onClick={() => {
                  const price = orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price;
                  if (price > 0) setOrderQty(((realizedBalance() * 0.75 * leverage()) / price).toFixed(3));
                }}>75%</div>
                <div class="pct-badge" onClick={() => {
                  const price = orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price;
                  if (price > 0) setOrderQty(((realizedBalance() * 0.95 * leverage()) / price).toFixed(3));
                }}>95%</div>
              </div>

              {/* Order Calculations */}
              <div class="order-summary-box">
                <div class="account-row">
                  <span class="account-label">Order Cost:</span>
                  <span class="account-val">
                    {formatNum(( (orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price) * Number(orderQty()) / leverage() ), 2)} USDT
                  </span>
                </div>
                <div class="account-row">
                  <span class="account-label">Est. Fee (0.04%):</span>
                  <span class="account-val" style="color: var(--text-muted);">
                    {formatNum(( (orderType() === "limit" ? Number(orderPrice()) : selectedTicker().price) * Number(orderQty()) * 0.0004 ), 4)} USDT
                  </span>
                </div>
              </div>

              {/* Submit CTA */}
              <button 
                type="button" 
                class="submit-order-btn" 
                style="background-color: var(--bg-terminal); color: var(--text-muted); border: 1px solid var(--border-main); cursor: not-allowed;"
                disabled
              >
                Manual Entry Locked (Engine Running)
              </button>
            </form>
          </section>

          {/* Simulated Order Book / Depth Panel */}
          <section class="order-book-panel">
            <div class="panel-header">
              <h3>Live Order Book</h3>
              <span class="version-badge">BIDS / ASKS</span>
            </div>

            <div class="ob-container">
              <div class="ob-header">
                <span>Price (USDT)</span>
                <span style="text-align: right;">Size ({selectedSymbol().replace("USDT", "")})</span>
                <span style="text-align: right;">Total</span>
              </div>

              {/* Asks (Sells) */}
              <div style="display: flex; flex-direction: column-reverse; gap: 1px;">
                <For each={asks()}>
                  {(ask) => (
                    <div class="ob-row">
                      <span class="ob-val ask">{ask.price}</span>
                      <span class="ob-qty">{ask.qty.toFixed(3)}</span>
                      <span class="ob-total">{ask.total.toFixed(3)}</span>
                      <div class="ob-depth-bar ask" style={`width: ${(ask.total / 10) * 100}%`}></div>
                    </div>
                  )}
                </For>
              </div>

              {/* Spread Indicator */}
              <div class="ob-spread-row">
                <span class="ob-spread-label">Spread</span>
                <span class={`ob-spread-price ${selectedTicker().price >= selectedTicker().prevPrice ? 'up' : 'down'}`}>
                  {selectedTicker().price.toFixed(selectedSymbol().includes("XRP") ? 4 : 2)}
                </span>
                <span class="ob-spread-label">
                  {Math.abs(asks()[asks().length - 1]?.price - bids()[0]?.price).toFixed(2) || "0.00"}
                </span>
              </div>

              {/* Bids (Buys) */}
              <div style="display: flex; flex-direction: column; gap: 1px;">
                <For each={bids()}>
                  {(bid) => (
                    <div class="ob-row">
                      <span class="ob-val bid">{bid.price}</span>
                      <span class="ob-qty">{bid.qty.toFixed(3)}</span>
                      <span class="ob-total">{bid.total.toFixed(3)}</span>
                      <div class="ob-depth-bar bid" style={`width: ${(bid.total / 10) * 100}%`}></div>
                    </div>
                  )}
                </For>
              </div>

            </div>
          </section>

        </aside>

      </div>

      {/* Floating alert/toasts */}
      <Show when={toast()}>
        {(t) => (
          <div class={`toast-msg ${t().type}`}>
            <Show when={t().type === 'success'} fallback={
              <svg viewBox="0 0 24 24" width="14" height="14" stroke="#ff3d00" stroke-width="2.5" fill="none">
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
            }>
              <svg viewBox="0 0 24 24" width="14" height="14" stroke="#00e676" stroke-width="2.5" fill="none">
                <polyline points="20 6 9 17 4 12"/>
              </svg>
            </Show>
            <span>{t().text}</span>
          </div>
        )}
      </Show>

    </div>
  );
}

export default App;
