import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, CandlestickSeries, HistogramSeries, LineStyle, CrosshairMode, createSeriesMarkers } from "lightweight-charts";
import type { IChartApi, ISeriesApi, UTCTimestamp, IPriceLine } from "lightweight-charts";
import type { Candle, Position, Order, Fill, TradeEvent, WatchlistItem, TabType } from "@/types";

interface CenterColumnProps {
  chartData: Candle[];
  selectedSymbol: string;
  timeframe: string;
  onTimeframeChange: (tf: string) => void;
  positions: Position[];
  orders: Order[];
  fills: Fill[];
  events: TradeEvent[];
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  selectedTicker: WatchlistItem | undefined;
  watchlist: WatchlistItem[];
}

function formatNum(num: number, dec = 2): string {
  if (num == null || isNaN(num)) return "0.00";
  return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"];

export function CenterColumn({
  chartData,
  selectedSymbol,
  timeframe,
  onTimeframeChange,
  positions,
  orders,
  fills,
  events,
  activeTab,
  onTabChange,
  selectedTicker,
  watchlist,
}: CenterColumnProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartInstanceRef = useRef<IChartApi | undefined>(undefined);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | undefined>(undefined);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | undefined>(undefined);
  const markersPluginRef = useRef<any>(undefined);
  const activePriceLineRef = useRef<IPriceLine | undefined>(undefined);
  const [hoveredCandle, setHoveredCandle] = useState<Candle | null>(null);
  const lastSymTfRef = useRef("");

  // Initialize chart
  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      layout: {
        background: { color: "#0b0e14" },
        textColor: "#94a3b8",
        fontSize: 11,
        fontFamily: "Inter, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: "#161c28", style: LineStyle.Dotted },
        horzLines: { color: "#161c28", style: LineStyle.Dotted },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: "#3b82f6",
          width: 1,
          style: LineStyle.LargeDashed,
          labelBackgroundColor: "#1e293b",
        },
        horzLine: {
          color: "#3b82f6",
          width: 1,
          style: LineStyle.LargeDashed,
          labelBackgroundColor: "#3b82f6",
        },
      },
      rightPriceScale: {
        borderColor: "#1b2230",
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: "#1b2230",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#00e676",
      downColor: "#ff3d00",
      borderUpColor: "#00e676",
      borderDownColor: "#ff3d00",
      wickUpColor: "#00e676",
      wickDownColor: "#ff3d00",
    });

    const markersPlugin = createSeriesMarkers(candleSeries);

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    // ResizeObserver
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !chart) return;
      const { width, height } = entries[0].contentRect;
      chart.resize(width, height);
    });
    resizeObserver.observe(container);

    // Crosshair
    chart.subscribeCrosshairMove((param) => {
      if (!param?.time || param.point === undefined || !candleSeries || !volumeSeries) {
        setHoveredCandle(null);
        return;
      }
      const rawCandle = param.seriesData.get(candleSeries);
      const rawVolumeObj = param.seriesData.get(volumeSeries);
      if (rawCandle) {
        const cd = rawCandle as { open?: number; high?: number; low?: number; close?: number };
        const vd = rawVolumeObj as { value?: number } | undefined;
        setHoveredCandle({
          time: Number(param.time) * 1000,
          open: Number(cd.open ?? 0),
          high: Number(cd.high ?? 0),
          low: Number(cd.low ?? 0),
          close: Number(cd.close ?? 0),
          volume: vd && "value" in vd ? Number(vd.value) : 0,
        });
      } else {
        setHoveredCandle(null);
      }
    });

    chartInstanceRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersPluginRef.current = markersPlugin;

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartInstanceRef.current = undefined;
      candleSeriesRef.current = undefined;
      volumeSeriesRef.current = undefined;
      markersPluginRef.current = undefined;
    };
  }, []);

  // Sync chart data
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!candleSeries || !volumeSeries || chartData.length === 0) return;

    const formattedCandles = chartData.map((c) => ({
      time: (c.time / 1000) as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    const formattedVolume = chartData.map((c) => ({
      time: (c.time / 1000) as UTCTimestamp,
      value: c.volume,
      color: c.close >= c.open ? "rgba(0, 230, 118, 0.25)" : "rgba(255, 61, 0, 0.25)",
    }));

    candleSeries.setData(formattedCandles);
    volumeSeries.setData(formattedVolume);

    const currentKey = `${selectedSymbol}-${timeframe}`;
    if (currentKey !== lastSymTfRef.current) {
      chartInstanceRef.current?.timeScale().fitContent();
      lastSymTfRef.current = currentKey;
    }
  }, [chartData, selectedSymbol, timeframe]);

  // Active position price line
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    if (!candleSeries) return;

    if (activePriceLineRef.current) {
      candleSeries.removePriceLine(activePriceLineRef.current);
      activePriceLineRef.current = undefined;
    }

    const activePos = positions.find((p) => p.symbol === selectedSymbol && p.status.toUpperCase() === "OPEN");

    if (activePos && activePos.avg_price > 0) {
      const isLong = activePos.side.toLowerCase() === "buy" || activePos.side.toLowerCase() === "long";
      activePriceLineRef.current = candleSeries.createPriceLine({
        price: activePos.avg_price,
        color: isLong ? "#00e676" : "#ff3d00",
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `POSITION (${activePos.side.toUpperCase()}) - ${activePos.qty} qty`,
      });
    }
  }, [positions, selectedSymbol]);

  // Trade fill markers
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const markersPlugin = markersPluginRef.current;
    if (!candleSeries || !markersPlugin || chartData.length === 0) return;

    const activeFills = fills.filter((f) => f.symbol === selectedSymbol);

    const markers = activeFills.map((fill) => {
      const fillTimeSec = fill.ts / 1000;
      let closestCandle = chartData[0];
      let minDiff = Math.abs(chartData[0].time / 1000 - fillTimeSec);

      for (let i = 1; i < chartData.length; i++) {
        const diff = Math.abs(chartData[i].time / 1000 - fillTimeSec);
        if (diff < minDiff) {
          minDiff = diff;
          closestCandle = chartData[i];
        }
      }

      const isBuy = fill.side.toLowerCase() === "buy";
      return {
        time: (closestCandle.time / 1000) as UTCTimestamp,
        position: (isBuy ? "belowBar" : "aboveBar") as "belowBar" | "aboveBar",
        shape: (isBuy ? "arrowUp" : "arrowDown") as "arrowUp" | "arrowDown",
        color: isBuy ? "#00e676" : "#ff3d00",
        text: `${fill.side.toUpperCase()} ${fill.qty}`,
        size: 1.2,
      };
    });

    markers.sort((a, b) => (a.time as number) - (b.time as number));
    markersPlugin.setMarkers(markers);
  }, [fills, selectedSymbol, chartData]);

  const lastCandle = chartData[chartData.length - 1];
  const displayCandle = hoveredCandle || lastCandle;

  const openOrdersCount = orders.filter((o) =>
    ["new", "pending", "partially_filled"].includes(o.status.toLowerCase())
  ).length;

  const getUnrealizedPnl = useCallback((p: Position) => {
    const livePrice = watchlist.find((w) => w.symbol === p.symbol)?.price;
    if (livePrice && p.qty > 0) {
      const sign = p.side === "LONG" || p.side === "BUY" ? 1 : -1;
      return (livePrice - p.avg_price) * p.qty * sign;
    }
    return 0;
  }, [watchlist]);

  return (
    <main className="flex flex-col h-full overflow-hidden">
      {/* Chart Panel */}
      <section className="flex-[1.3] flex flex-col border-b border-[var(--border-main)] overflow-hidden bg-[var(--bg-panel)]">
        {/* Chart Header */}
        <div className="flex items-center justify-between h-8 bg-[var(--bg-header)] border-b border-[var(--border-main)] px-3">
          <div className="flex items-center gap-3">
            <span className="font-extrabold text-[13px] text-[var(--text-bright)]">{selectedSymbol}</span>
            <span className={`font-[var(--font-mono)] font-bold text-[13px] ${selectedTicker && selectedTicker.price >= selectedTicker.prevPrice ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
              {selectedTicker ? selectedTicker.price.toFixed(selectedTicker.price > 10 ? 2 : 4) : "—"}
            </span>
            <span className={`font-[var(--font-mono)] text-[11px] font-semibold ${selectedTicker && selectedTicker.change24h >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
              {selectedTicker ? `${selectedTicker.change24h >= 0 ? "+" : ""}${selectedTicker.change24h.toFixed(2)}%` : ""}
            </span>
          </div>
          <div className="flex gap-1">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                className={`bg-transparent border-none text-[10px] font-bold px-1.5 py-0.5 rounded-[2px] cursor-pointer transition-colors ${
                  timeframe === tf ? "bg-[var(--border-main)] text-[var(--text-bright)]" : "text-[var(--text-secondary)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text-bright)]"
                }`}
                onClick={() => onTimeframeChange(tf)}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>

        {/* Chart Area */}
        <div className="flex-1 relative overflow-hidden bg-[#080a0f] select-none">
          {/* HUD */}
          <div className="absolute top-1.5 left-3 flex gap-3 text-[10px] font-[var(--font-mono)] bg-[rgba(6,8,14,0.8)] px-2 py-[3px] rounded-[3px] border border-[var(--border-main)] pointer-events-none z-[5]">
            {displayCandle ? (
              <>
                <div className="text-[var(--text-muted)]">O: <strong className="text-[var(--text-bright)] ml-0.5">{displayCandle.open.toFixed(selectedSymbol.includes("XRP") ? 4 : 2)}</strong></div>
                <div className="text-[var(--text-muted)]">H: <strong className="text-[var(--text-bright)] ml-0.5">{displayCandle.high.toFixed(selectedSymbol.includes("XRP") ? 4 : 2)}</strong></div>
                <div className="text-[var(--text-muted)]">L: <strong className="text-[var(--text-bright)] ml-0.5">{displayCandle.low.toFixed(selectedSymbol.includes("XRP") ? 4 : 2)}</strong></div>
                <div className="text-[var(--text-muted)]">C: <strong className="text-[var(--text-bright)] ml-0.5">{displayCandle.close.toFixed(selectedSymbol.includes("XRP") ? 4 : 2)}</strong></div>
                <div className="text-[var(--text-muted)]">V: <strong className="text-[var(--text-bright)] ml-0.5">{formatNum(displayCandle.volume, 0)}</strong></div>
              </>
            ) : (
              <span className="text-[var(--text-muted)]">Loading Chart Data...</span>
            )}
          </div>

          {chartData.length === 0 && (
            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-10 text-[var(--text-muted)] italic text-[11px]">
              Loading Chart Data...
            </div>
          )}

          <div ref={chartContainerRef} className="w-full h-full absolute top-0 left-0 right-0 bottom-0 z-[1]" />
        </div>
      </section>

      {/* Tabbed Workspace */}
      <section className="flex-1 flex flex-col overflow-hidden bg-[var(--bg-panel)]">
        {/* Tabs */}
        <div className="flex h-8 bg-[var(--bg-header)] border-b border-[var(--border-main)] items-center px-1">
          {[
            { key: "positions" as TabType, label: "Positions", count: positions.length },
            { key: "orders" as TabType, label: "Open Orders", count: openOrdersCount },
            { key: "fills" as TabType, label: "Trade History", count: fills.length },
            { key: "logs" as TabType, label: "System Logs", count: undefined },
          ].map((tab) => (
            <button
              key={tab.key}
              className={`h-full bg-transparent border-none border-b-2 text-[11px] font-bold px-3.5 flex items-center gap-1.5 cursor-pointer transition-colors ${
                activeTab === tab.key
                  ? "text-[var(--text-bright)] border-b-[var(--border-active)] bg-white/[0.02]"
                  : "text-[var(--text-secondary)] border-b-transparent hover:text-[var(--text-bright)]"
              }`}
              onClick={() => onTabChange(tab.key)}
            >
              {tab.label}
              {tab.count !== undefined && (
                <span className="bg-white/[0.08] text-[9px] px-1 py-[1px] rounded font-[var(--font-mono)]">{tab.count}</span>
              )}
            </button>
          ))}
        </div>

        {/* Tab Content */}
        <div className="flex-1 overflow-hidden flex flex-col">
          {/* Positions */}
          {activeTab === "positions" && (
            <div className="flex-1 overflow-y-auto bg-[#07090e]">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Symbol</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Side</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Size (Qty)</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Entry Price</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Mark Price</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Unrealized PnL</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="text-center py-6 text-[var(--text-muted)] italic text-[11px]">No open positions. Portfolio flat.</td>
                    </tr>
                  ) : (
                    positions.map((p) => {
                      const livePrice = watchlist.find((w) => w.symbol === p.symbol)?.price || p.avg_price;
                      const uPnl = getUnrealizedPnl(p);
                      return (
                        <tr key={p.symbol} className={`${p.flash ? `flash-row-${p.flash}` : ""} hover:bg-[var(--bg-subtle)]`}>
                          <td className="px-2.5 py-1.5 font-[var(--font-sans)] font-bold text-[var(--text-bright)] text-[11px] border-b border-[var(--border-subtle)]">{p.symbol}</td>
                          <td className="px-2.5 py-1.5 text-[11px] border-b border-[var(--border-subtle)]">
                            <span className={`px-1 py-[1px] text-[9px] font-extrabold rounded uppercase inline-block ${
                              p.side.toLowerCase() === "long" || p.side.toLowerCase() === "buy"
                                ? "bg-[var(--bg-up-subtle)] text-[var(--color-up)] border border-[rgba(0,230,118,0.2)]"
                                : "bg-[var(--bg-down-subtle)] text-[var(--color-down)] border border-[rgba(255,61,0,0.2)]"
                            }`}>
                              {p.side}
                            </span>
                          </td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{p.qty.toFixed(4)}</td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{p.avg_price.toFixed(p.avg_price > 10 ? 2 : 4)}</td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{livePrice.toFixed(livePrice > 10 ? 2 : 4)}</td>
                          <td className={`px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] border-b border-[var(--border-subtle)] ${uPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                            {uPnl >= 0 ? "+" : ""}{uPnl.toFixed(2)} USDT
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          )}

          {/* Orders */}
          {activeTab === "orders" && (
            <div className="flex-1 overflow-y-auto bg-[#07090e]">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Order ID</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Symbol</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Side</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Type</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Qty</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Filled</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Status</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.filter((o) => ["new", "pending", "partially_filled"].includes(o.status.toLowerCase())).length === 0 ? (
                    <tr>
                      <td colSpan={8} className="text-center py-6 text-[var(--text-muted)] italic text-[11px]">No open orders.</td>
                    </tr>
                  ) : (
                    orders
                      .filter((o) => ["new", "pending", "partially_filled"].includes(o.status.toLowerCase()))
                      .map((o) => (
                        <tr key={o.exchange_order_id} className={`${o.flash ? `flash-row-${o.flash}` : ""} hover:bg-[var(--bg-subtle)]`}>
                          <td className="px-2.5 py-1.5 font-[var(--font-sans)] font-bold text-[var(--text-bright)] text-[11px] border-b border-[var(--border-subtle)]">{o.exchange_order_id.substring(0, 10)}...</td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{o.symbol}</td>
                          <td className="px-2.5 py-1.5 text-[11px] border-b border-[var(--border-subtle)]">
                            <span className={`px-1 py-[1px] text-[9px] font-extrabold rounded uppercase inline-block ${
                              o.side.toLowerCase() === "buy"
                                ? "bg-[var(--bg-up-subtle)] text-[var(--color-up)] border border-[rgba(0,230,118,0.2)]"
                                : "bg-[var(--bg-down-subtle)] text-[var(--color-down)] border border-[rgba(255,61,0,0.2)]"
                            }`}>
                              {o.side}
                            </span>
                          </td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{o.order_type}</td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{o.qty.toFixed(4)}</td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{o.filled_qty.toFixed(4)}</td>
                          <td className="px-2.5 py-1.5 text-[11px] border-b border-[var(--border-subtle)]">
                            <span className={`px-1 py-[1px] text-[9px] font-bold rounded uppercase ${
                              o.status.toLowerCase() === "open" || o.status.toLowerCase() === "new"
                                ? "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                                : "bg-white/5 text-[var(--text-muted)]"
                            }`}>
                              {o.status}
                            </span>
                          </td>
                          <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-muted)] border-b border-[var(--border-subtle)]">{new Date(o.created_at).toLocaleTimeString()}</td>
                        </tr>
                      ))
                  )}
                </tbody>
              </table>
            </div>
          )}

          {/* Fills */}
          {activeTab === "fills" && (
            <div className="flex-1 overflow-y-auto bg-[#07090e]">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Time</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Symbol</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Side</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Execution Price</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Executed Qty</th>
                    <th className="bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[var(--text-secondary)] text-[10px] font-bold uppercase tracking-[0.04em] px-2.5 py-1.5 sticky top-0 z-[5]">Fee Paid</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="text-center py-6 text-[var(--text-muted)] italic text-[11px]">No execution logs.</td>
                    </tr>
                  ) : (
                    fills.map((f, idx) => (
                      <tr key={`${f.exchange_order_id}-${idx}`} className={`${f.flash ? `flash-row-${f.flash}` : ""} hover:bg-[var(--bg-subtle)]`}>
                        <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-muted)] border-b border-[var(--border-subtle)]">{new Date(f.ts).toLocaleTimeString()}</td>
                        <td className="px-2.5 py-1.5 font-[var(--font-sans)] font-bold text-[var(--text-bright)] text-[11px] border-b border-[var(--border-subtle)]">{f.symbol}</td>
                        <td className="px-2.5 py-1.5 text-[11px] border-b border-[var(--border-subtle)]">
                          <span className={`px-1 py-[1px] text-[9px] font-extrabold rounded uppercase inline-block ${
                            f.side.toLowerCase() === "buy"
                              ? "bg-[var(--bg-up-subtle)] text-[var(--color-up)] border border-[rgba(0,230,118,0.2)]"
                              : "bg-[var(--bg-down-subtle)] text-[var(--color-down)] border border-[rgba(255,61,0,0.2)]"
                          }`}>
                            {f.side}
                          </span>
                        </td>
                        <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{f.price.toFixed(f.price > 10 ? 2 : 4)}</td>
                        <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-primary)] border-b border-[var(--border-subtle)]">{f.qty.toFixed(4)}</td>
                        <td className="px-2.5 py-1.5 font-[var(--font-mono)] text-[11px] text-[var(--text-muted)] border-b border-[var(--border-subtle)]">{f.fee.toFixed(4)} USDT</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          )}

          {/* Logs */}
          {activeTab === "logs" && (
            <div className="flex-1 bg-[var(--bg-terminal)] overflow-y-auto font-[var(--font-mono)] text-[10px] p-1.5">
              {events.length === 0 ? (
                <div className="text-center py-6 text-[var(--text-muted)] italic">Streaming engine log feeds...</div>
              ) : (
                events.map((ev, idx) => {
                  const logClass = ev.event_type.includes("FILL")
                    ? "text-[var(--color-up)]"
                    : ev.event_type.includes("CLOSED")
                    ? "text-[var(--color-down)]"
                    : ev.event_type.includes("ERROR") || ev.event_type.includes("REJECTED")
                    ? "text-[var(--color-warn)]"
                    : "text-blue-400";
                  return (
                    <div key={idx} className="border-b border-white/[0.02] py-[3px] leading-relaxed">
                      <span className="text-[var(--text-muted)]">[{new Date(ev.ts * 1000).toLocaleTimeString()}]</span>{" "}
                      <span className={`font-bold ${logClass}`}>{ev.event_type}</span>
                      <pre className="mt-0.5 ml-3 text-[var(--text-secondary)] whitespace-pre-wrap break-words bg-white/[0.01] p-1 rounded border-l-2 border-[var(--border-main)]">
                        {JSON.stringify(ev.payload, (key, value) => {
                          if (key === "mode" || key === "symbol") return undefined;
                          return value;
                        }, 2)}
                      </pre>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}
