import { useState, useMemo, useCallback } from "react";
import { Header } from "@/sections/Header";
import { LeftSidebar } from "@/sections/LeftSidebar";
import { CenterColumn } from "@/sections/CenterColumn";
import { RightSidebar } from "@/sections/RightSidebar";
import { BotPanel } from "@/sections/BotPanel";
import { Toast } from "@/components/Toast";
import { useBinanceData } from "@/hooks/useBinanceData";
import { useTradingBot } from "@/hooks/useTradingBot";
import type { AppMode, TabType } from "@/types";
import "./App.css";

function App() {
  const [mode, setMode] = useState<AppMode>("paper");
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

  // Trading Bot
  const {
    botState,
    botEvents,
    positions: botPositions,
    orders: botOrders,
    fills: botFills,
    isRunning,
    start,
    pause,
    resume,
    stop,
    reset,
    manualTick,
  } = useTradingBot(selectedSymbol);

  // Unrealized PnL calculation
  const unrealizedPnL = useMemo(() => {
    let total = 0;
    botPositions.forEach((p) => {
      const livePrice = watchlist.find((w) => w.symbol === p.symbol)?.price;
      if (livePrice && p.qty > 0) {
        const sign = p.side === "LONG" || p.side === "BUY" ? 1 : -1;
        total += (livePrice - p.avg_price) * p.qty * sign;
      }
    });
    return total;
  }, [botPositions, watchlist]);

  const accountBalance = botState?.balance ?? 1000;
  const equity = accountBalance + unrealizedPnL;
  const realizedPnl = botState?.realizedPnl ?? 0;
  const totalFees = botFills.reduce((sum, f) => sum + f.fee, 0);

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

  // Bot header metrics
  const botEquity = botState ? botState.marginBalance : equity;
  const botUnrealized = botState ? botState.unrealizedPnl : unrealizedPnL;
  const botRealized = botState ? botState.realizedPnl : realizedPnl;
  const botFees = totalFees;
  const botActiveTrades = botState ? botState.openPositions.filter((p) => p.status === "OPEN").length : 0;

  return (
    <div className="app-container">
      <Header
        mode={mode}
        setMode={setMode}
        equity={botEquity}
        unrealizedPnl={botUnrealized}
        realizedPnl={botRealized}
        totalFees={botFees}
        activeTrades={botActiveTrades}
        connState={isRunning ? "live" : "connecting"}
        lastEventAge={botState?.tick ? 0 : null}
      />

      <div className="dashboard-layout">
        {/* Column 1: Watchlist */}
        <div className="min-w-0">
          <LeftSidebar
            watchlist={watchlist}
            selectedSymbol={selectedSymbol}
            onSelectSymbol={handleSelectSymbol}
            mode={mode}
            venueHealth={null}
            realizedBalance={accountBalance}
            unrealizedPnl={botUnrealized}
            equity={botEquity}
          />
        </div>

        {/* Column 2: Chart & Tables */}
        <div className="min-w-0">
          <CenterColumn
            chartData={chartData}
            selectedSymbol={selectedSymbol}
            timeframe={timeframe}
            onTimeframeChange={handleTimeframeChange}
            positions={botPositions}
            orders={botOrders}
            fills={botFills}
            events={botEvents.map((ev) => ({
              event_type: ev.type,
              payload: { symbol: ev.symbol, message: ev.message, pnl: ev.pnl },
              ts: Math.floor(ev.timestamp / 1000),
            }))}
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
              botState={botState}
              botEvents={botEvents}
              onStart={start}
              onPause={pause}
              onResume={resume}
              onStop={stop}
              onReset={reset}
              onManualTick={manualTick}
            />
            <RightSidebar
              selectedSymbol={selectedSymbol}
              selectedTicker={selectedTicker}
              bids={bids}
              asks={asks}
              realizedBalance={accountBalance}
            />
          </div>
        </div>
      </div>

      <Toast toast={null} />
    </div>
  );
}

export default App;
