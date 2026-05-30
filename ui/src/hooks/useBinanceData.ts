import { useState, useEffect, useCallback, useRef } from "react";
import type { Candle, WatchlistItem, OrderBookEntry } from "@/types";

const DEFAULT_WATCHLIST: WatchlistItem[] = [
  { symbol: "BTCUSDT", price: 68420.5, prevPrice: 68420.5, change24h: 1.25, sparkline: [68000, 68150, 68080, 68250, 68400, 68350, 68420], bias: "BULLISH" },
  { symbol: "ETHUSDT", price: 3822.4, prevPrice: 3822.4, change24h: -0.45, sparkline: [3850, 3840, 3810, 3830, 3820, 3825, 3822], bias: "NEUTRAL" },
  { symbol: "SOLUSDT", price: 178.65, prevPrice: 178.65, change24h: 3.8, sparkline: [170, 172, 171, 174, 176, 175, 178.65], bias: "BULLISH" },
  { symbol: "XRPUSDT", price: 0.5422, prevPrice: 0.5422, change24h: -1.12, sparkline: [0.552, 0.548, 0.545, 0.546, 0.541, 0.543, 0.5422], bias: "BEARISH" },
  { symbol: "DOGEUSDT", price: 0.1502, prevPrice: 0.1502, change24h: 5.18, sparkline: [0.141, 0.143, 0.142, 0.147, 0.148, 0.149, 0.1502], bias: "BULLISH" },
  { symbol: "ADAUSDT", price: 0.485, prevPrice: 0.485, change24h: -0.85, sparkline: [0.492, 0.489, 0.488, 0.49, 0.484, 0.486, 0.485], bias: "BEARISH" },
  { symbol: "LINKUSDT", price: 15.65, prevPrice: 15.65, change24h: 2.12, sparkline: [15.2, 15.3, 15.25, 15.4, 15.6, 15.55, 15.65], bias: "BULLISH" },
];

const INITIAL_BIDS: OrderBookEntry[] = [
  { price: 68419.5, qty: 0.452, total: 0.452 },
  { price: 68419.0, qty: 1.123, total: 1.575 },
  { price: 68418.5, qty: 0.789, total: 2.364 },
  { price: 68418.0, qty: 2.341, total: 4.705 },
  { price: 68417.5, qty: 0.567, total: 5.272 },
  { price: 68417.0, qty: 1.890, total: 7.162 },
  { price: 68416.5, qty: 0.334, total: 7.496 },
  { price: 68416.0, qty: 3.210, total: 10.706 },
];

const INITIAL_ASKS: OrderBookEntry[] = [
  { price: 68421.0, qty: 0.678, total: 0.678 },
  { price: 68421.5, qty: 1.234, total: 1.912 },
  { price: 68422.0, qty: 0.456, total: 2.368 },
  { price: 68422.5, qty: 2.890, total: 5.258 },
  { price: 68423.0, qty: 0.123, total: 5.381 },
  { price: 68423.5, qty: 1.567, total: 6.948 },
  { price: 68424.0, qty: 0.890, total: 7.838 },
  { price: 68424.5, qty: 4.321, total: 12.159 },
];

export function useBinanceData() {
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>(DEFAULT_WATCHLIST);
  const [chartData, setChartData] = useState<Candle[]>([]);
  const [bids, setBids] = useState<OrderBookEntry[]>(INITIAL_BIDS);
  const [asks, setAsks] = useState<OrderBookEntry[]>(INITIAL_ASKS);
  const selectedSymbolRef = useRef("BTCUSDT");
  const timeframeRef = useRef("5m");

  const setSelectedSymbolRef = useCallback((sym: string) => {
    selectedSymbolRef.current = sym;
  }, []);

  const setTimeframeRef = useCallback((tf: string) => {
    timeframeRef.current = tf;
  }, []);

  // Fetch klines/candles
  const fetchKlines = useCallback(async (sym: string, tf: string) => {
    try {
      const binanceInterval = tf === "1d" ? "1d" : tf === "1h" ? "1h" : tf === "15m" ? "15m" : tf === "5m" ? "5m" : "1m";
      const response = await fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=${sym}&interval=${binanceInterval}&limit=150`);
      if (!response.ok) throw new Error("Binance API call failed");
      const rawKlines = await response.json();
      const parsed: Candle[] = rawKlines.map((k: unknown[]) => ({
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
    } catch (e) {
      console.warn("[Chart] Fetching binance klines failed:", e);
      setChartData([]);
    }
  }, []);

  // Fetch live watchlist prices
  const fetchLiveWatchlistData = useCallback(async () => {
    const symbols = watchlist.map(w => w.symbol);
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

        // If this is the active symbol, update chart's last candle
        if (w.symbol === selectedSymbolRef.current && chartData.length > 0) {
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
      console.warn("Failed to fetch live watchlist data:", e);
    }
  }, [watchlist, chartData.length]);

  // Fetch sparklines
  const fetchWatchlistSparklines = useCallback(async () => {
    const symbols = watchlist.map(w => w.symbol);
    try {
      const results = await Promise.all(
        symbols.map(sym =>
          fetch(`https://fapi.binance.com/fapi/v1/klines?symbol=${sym}&interval=1h&limit=10`)
            .then(r => {
              if (!r.ok) throw new Error();
              return r.json();
            })
            .then((data: unknown[][]) => data.map((k) => Number(k[4])))
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
      console.warn("Failed to fetch sparklines:", e);
    }
  }, [watchlist]);

  // Fetch order book
  const fetchLiveOrderBook = useCallback(async (sym: string) => {
    try {
      const response = await fetch(`https://fapi.binance.com/fapi/v1/depth?symbol=${sym}&limit=10`);
      if (!response.ok) throw new Error();
      const data = await response.json() as { bids: string[][]; asks: string[][] };

      let bidAccum = 0;
      const parsedBids = data.bids.slice(0, 8).map((b) => {
        const price = Number(b[0]);
        const qty = Number(b[1]);
        bidAccum += qty;
        return { price, qty, total: Number(bidAccum.toFixed(3)) };
      });

      let askAccum = 0;
      const parsedAsks = data.asks.slice(0, 8).map((a) => {
        const price = Number(a[0]);
        const qty = Number(a[1]);
        askAccum += qty;
        return { price, qty, total: Number(askAccum.toFixed(3)) };
      });

      setBids(parsedBids);
      setAsks(parsedAsks.reverse());
    } catch (e) {
      console.warn("Failed to fetch order book:", e);
    }
  }, []);

  // Periodic updates
  useEffect(() => {
    const feedInterval = setInterval(() => {
      fetchKlines(selectedSymbolRef.current, timeframeRef.current);
      fetchLiveWatchlistData();
      fetchLiveOrderBook(selectedSymbolRef.current);
    }, 2000);

    return () => clearInterval(feedInterval);
  }, [fetchKlines, fetchLiveWatchlistData, fetchLiveOrderBook]);

  // Initial load
  useEffect(() => {
    fetchKlines("BTCUSDT", "5m");
    fetchLiveWatchlistData();
    fetchWatchlistSparklines();
    fetchLiveOrderBook("BTCUSDT");
  }, []);

  return {
    watchlist,
    setWatchlist,
    chartData,
    setChartData,
    bids,
    asks,
    fetchKlines,
    fetchLiveWatchlistData,
    fetchWatchlistSparklines,
    fetchLiveOrderBook,
    setSelectedSymbolRef,
    setTimeframeRef,
  };
}
