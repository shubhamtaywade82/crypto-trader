import { useState, useMemo, useCallback } from "react";
import { Header } from "@/sections/Header";
import { LeftSidebar } from "@/sections/LeftSidebar";
import { CenterColumn } from "@/sections/CenterColumn";
import { RightSidebar } from "@/sections/RightSidebar";
import { BotPanel } from "@/sections/BotPanel";
import { Toast } from "@/components/Toast";
import { useBinanceData } from "@/hooks/useBinanceData";
import { useTradingBot } from "@/hooks/useTradingBot";
import { useTradingEngine } from "@/hooks/useTradingEngine";
import type { AppMode, TabType } from "@/types";
import "./App.css";

function App() {
  const [mode, setMode] = useState<AppMode>("paper");
  const [dataSource, setDataSource] = useState<"browser" | "backend">("backend");
  const [selectedSymbol, setSelectedSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [activeTab, setActiveTab] = useState<TabType>("positions");

  const {
    watchlist,
    chartData,
    bids,
    asks,
    fetchKlines,
    fetchLiveOrderBook,
    setSelectedSymbolRef,
    setTimeframeRef,
  } = useBinanceData();

  // 1. Browser Sandbox Bot Hook
  const browserBot = useTradingBot(selectedSymbol);

  // 2. Python Backend API Hook
  const backendBot = useTradingEngine(mode);

  // Computed / Mapped States based on Selected Data Source
  const activePositions = useMemo(() => {
    return dataSource === "backend" ? backendBot.positions : browserBot.positions;
  }, [dataSource, backendBot.positions, browserBot.positions]);

  const activeOrders = useMemo(() => {
    return dataSource === "backend" ? backendBot.orders : browserBot.orders;
  }, [dataSource, backendBot.orders, browserBot.orders]);

  const activeFills = useMemo(() => {
    return dataSource === "backend" ? backendBot.fills : browserBot.fills;
  }, [dataSource, backendBot.fills, browserBot.fills]);

  const activeEvents = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.events.map((ev) => ({
        event_type: ev.event_type || "INFO",
        payload: ev.payload || {},
        ts: ev.ts || Math.floor(Date.now() / 1000),
      }));
    } else {
      return browserBot.botEvents.map((ev) => ({
        event_type: ev.type,
        payload: { symbol: ev.symbol, message: ev.message, pnl: ev.pnl },
        ts: Math.floor(ev.timestamp / 1000),
      }));
    }
  }, [dataSource, backendBot.events, browserBot.botEvents]);

  // Unrealized PnL calculation for Sandbox Mode
  const browserUnrealizedPnL = useMemo(() => {
    let total = 0;
    browserBot.positions.forEach((p) => {
      const livePrice = watchlist.find((w) => w.symbol === p.symbol)?.price;
      if (livePrice && p.qty > 0) {
        const sign = p.side === "LONG" || p.side === "BUY" ? 1 : -1;
        total += (livePrice - p.avg_price) * p.qty * sign;
      }
    });
    return total;
  }, [browserBot.positions, watchlist]);

  // Derive resolved Header Metrics
  const resolvedEquity = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.accountEquity;
    }
    const balance = browserBot.botState?.balance ?? 1000;
    return balance + browserUnrealizedPnL;
  }, [dataSource, backendBot.accountEquity, browserBot.botState, browserUnrealizedPnL]);

  const resolvedUnrealizedPnL = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.accountEquity - backendBot.accountBalance;
    }
    return browserUnrealizedPnL;
  }, [dataSource, backendBot.accountEquity, backendBot.accountBalance, browserUnrealizedPnL]);

  const resolvedRealizedPnL = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.pnl.realized_pnl;
    }
    return browserBot.botState?.realizedPnl ?? 0;
  }, [dataSource, backendBot.pnl.realized_pnl, browserBot.botState?.realizedPnl]);

  const resolvedTotalFees = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.pnl.total_fees;
    }
    return browserBot.fills.reduce((sum, f) => sum + f.fee, 0);
  }, [dataSource, backendBot.pnl.total_fees, browserBot.fills]);

  const resolvedActiveTradesCount = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.positions.length;
    }
    return browserBot.botState?.openPositions.filter((p) => p.status === "OPEN").length ?? 0;
  }, [dataSource, backendBot.positions.length, browserBot.botState?.openPositions]);

  const resolvedConnState = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.connState;
    }
    return browserBot.isRunning ? "live" : "connecting";
  }, [dataSource, backendBot.connState, browserBot.isRunning]);

  const resolvedLastEventAge = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.lastEventAge;
    }
    return browserBot.botState?.tick ? 0 : null;
  }, [dataSource, backendBot.lastEventAge, browserBot.botState?.tick]);

  const resolvedAccountBalance = useMemo(() => {
    if (dataSource === "backend") {
      return backendBot.accountBalance;
    }
    return browserBot.botState?.balance ?? 1000;
  }, [dataSource, backendBot.accountBalance, browserBot.botState?.balance]);

  // Construct mock/mapped BotState for Backend BotPanel integration
  const resolvedBotPanelState = useMemo(() => {
    if (dataSource === "browser") {
      return browserBot.botState;
    }

    // Build compatible state structure for read-only backend bot
    const totalTrades = backendBot.fills.length;

    return {
      status: backendBot.health.status === "ok" ? "running" : "idle",
      balance: backendBot.accountBalance,
      unrealizedPnl: backendBot.accountEquity - backendBot.accountBalance,
      realizedPnl: backendBot.pnl.realized_pnl,
      marginBalance: backendBot.accountEquity,
      availableBalance: backendBot.accountBalance,
      utilizedMargin: 0,
      openPositions: backendBot.positions.map((p) => ({
        id: p.symbol,
        symbol: p.symbol,
        side: p.side,
        quantity: p.qty,
        entryPrice: p.avg_price,
        currentPrice: p.avg_price,
        unrealizedPnl: 0,
        margin: 0,
        leverage: 10,
        status: p.status,
        openTime: p.last_event_ts,
        fees: 0,
      })),
      positionHistory: [],
      dailyTrades: 0,
      totalTrades,
      regime: backendBot.risk?.available ? "ACTIVE" : "RUNNING",
      regimeScore: 1.0,
      lastSignal: null,
      lastSignalTime: null,
      lastError: backendBot.risk?.halt_reason || null,
      tick: backendBot.events.length,
      riskState: {
        dailyDrawdown: backendBot.risk?.drawdown || 0,
        haltReason: backendBot.risk?.halt_reason || null,
        lastHaltTime: null,
        cooldownUntil: null,
      },
    } as any;
  }, [dataSource, browserBot.botState, backendBot.accountBalance, backendBot.accountEquity, backendBot.pnl, backendBot.positions, backendBot.fills, backendBot.health, backendBot.risk, backendBot.events]);

  const resolvedBotPanelEvents = useMemo(() => {
    if (dataSource === "browser") {
      return browserBot.botEvents;
    }
    return backendBot.events.map((ev) => ({
      type: (ev.event_type || "INFO") as any,
      timestamp: (ev.ts ? ev.ts * 1000 : Date.now()),
      symbol: (ev.payload?.symbol as string) || "SYSTEM",
      message: (ev.payload?.message as string) || JSON.stringify(ev.payload || {}),
      pnl: (ev.payload?.pnl as number) || undefined,
    }));
  }, [dataSource, browserBot.botEvents, backendBot.events]);

  const selectedTicker = watchlist.find((w) => w.symbol === selectedSymbol);

  const handleSelectSymbol = useCallback((sym: string) => {
    setSelectedSymbol(sym);
    setSelectedSymbolRef(sym);
    fetchKlines(sym, timeframe);
    fetchLiveOrderBook(sym);
  }, [setSelectedSymbolRef, fetchKlines, fetchLiveOrderBook, timeframe]);

  const handleTimeframeChange = useCallback((tf: string) => {
    setTimeframe(tf);
    setTimeframeRef(tf);
    fetchKlines(selectedSymbol, tf);
  }, [setTimeframeRef, fetchKlines, selectedSymbol]);

  // Read-only handlers for Backend mode
  const handleStart = useCallback(() => {
    if (dataSource === "browser") browserBot.start();
    else backendBot.showToast("Backend bot control must be managed via CLI.", "danger");
  }, [dataSource, browserBot, backendBot]);

  const handlePause = useCallback(() => {
    if (dataSource === "browser") browserBot.pause();
    else backendBot.showToast("Backend bot control must be managed via CLI.", "danger");
  }, [dataSource, browserBot, backendBot]);

  const handleResume = useCallback(() => {
    if (dataSource === "browser") browserBot.resume();
    else backendBot.showToast("Backend bot control must be managed via CLI.", "danger");
  }, [dataSource, browserBot, backendBot]);

  const handleStop = useCallback(() => {
    if (dataSource === "browser") browserBot.stop();
    else backendBot.showToast("Backend bot control must be managed via CLI.", "danger");
  }, [dataSource, browserBot, backendBot]);

  const handleReset = useCallback(() => {
    if (dataSource === "browser") browserBot.reset();
    else backendBot.showToast("Backend bot control must be managed via CLI.", "danger");
  }, [dataSource, browserBot, backendBot]);

  const handleManualTick = useCallback(() => {
    if (dataSource === "browser") browserBot.manualTick();
    else backendBot.showToast("Manual tick simulation is only available in Browser Sandbox.", "danger");
  }, [dataSource, browserBot, backendBot]);

  return (
    <div className="app-container">
      <Header
        mode={mode}
        setMode={setMode}
        dataSource={dataSource}
        setDataSource={setDataSource}
        equity={resolvedEquity}
        unrealizedPnl={resolvedUnrealizedPnL}
        realizedPnl={resolvedRealizedPnL}
        totalFees={resolvedTotalFees}
        activeTrades={resolvedActiveTradesCount}
        connState={resolvedConnState}
        lastEventAge={resolvedLastEventAge}
      />

      <div className="dashboard-layout">
        {/* Column 1: Watchlist */}
        <div className="min-w-0">
          <LeftSidebar
            watchlist={watchlist}
            selectedSymbol={selectedSymbol}
            onSelectSymbol={handleSelectSymbol}
            mode={mode}
            venueHealth={dataSource === "backend" ? backendBot.venueHealth : null}
            realizedBalance={resolvedAccountBalance}
            unrealizedPnl={resolvedUnrealizedPnL}
            equity={resolvedEquity}
          />
        </div>

        {/* Column 2: Chart & Tables */}
        <div className="min-w-0">
          <CenterColumn
            chartData={chartData}
            selectedSymbol={selectedSymbol}
            timeframe={timeframe}
            onTimeframeChange={handleTimeframeChange}
            positions={activePositions}
            orders={activeOrders}
            fills={activeFills}
            events={activeEvents}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            selectedTicker={selectedTicker}
            watchlist={watchlist}
          />
        </div>

        {/* Column 3: Bot Panel + Order Book */}
        <div className="min-w-0 overflow-y-auto">
          <div className="flex flex-col gap-2 p-2">
            <BotPanel
              botState={resolvedBotPanelState}
              botEvents={resolvedBotPanelEvents}
              onStart={handleStart}
              onPause={handlePause}
              onResume={handleResume}
              onStop={handleStop}
              onReset={handleReset}
              onManualTick={handleManualTick}
            />
            <RightSidebar
              selectedSymbol={selectedSymbol}
              selectedTicker={selectedTicker}
              bids={bids}
              asks={asks}
              realizedBalance={resolvedAccountBalance}
            />
          </div>
        </div>
      </div>

      <Toast toast={dataSource === "backend" ? backendBot.toast : null} />
    </div>
  );
}

export default App;
