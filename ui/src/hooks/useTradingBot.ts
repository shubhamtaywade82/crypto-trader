import { useState, useEffect, useRef, useCallback } from "react";
import { TradingEngine } from "@/engine/engine";
import type { BotState, TradeEvent, BotConfig } from "@/engine/engine";
import type { Position, Order, Fill } from "@/types";

function botStateToPositions(state: BotState): Position[] {
  return state.openPositions.map((p) => ({
    symbol: p.symbol,
    mode: "paper",
    side: p.side,
    qty: p.quantity,
    avg_price: p.entryPrice,
    status: p.status,
    last_event_ts: p.openTime,
  }));
}

function botHistoryToFills(state: BotState): Fill[] {
  const fills: Fill[] = [];
  for (const p of state.positionHistory) {
    if (p.closePrice && p.closeTime) {
      fills.push({
        exchange_order_id: p.id,
        symbol: p.symbol,
        side: p.side,
        mode: "paper",
        price: p.closePrice,
        qty: p.quantity,
        fee: p.fees,
        ts: p.closeTime,
      });
    }
  }
  // Also add open positions as entry fills
  for (const p of state.openPositions) {
    fills.push({
      exchange_order_id: p.id,
      symbol: p.symbol,
      side: p.side,
      mode: "paper",
      price: p.entryPrice,
      qty: p.quantity,
      fee: p.fees,
      ts: p.openTime,
    });
  }
  return fills.sort((a, b) => b.ts - a.ts);
}

function botStateToOrders(state: BotState): Order[] {
  return state.openPositions.map((p) => ({
    exchange_order_id: p.id,
    mode: "paper",
    symbol: p.symbol,
    side: p.side,
    order_type: "MARKET",
    qty: p.quantity,
    filled_qty: p.quantity,
    avg_fill_price: p.entryPrice,
    status: p.status === "OPEN" ? "FILLED" : "CLOSED",
    created_at: p.openTime,
  }));
}

export function useTradingBot(symbol: string) {
  const engineRef = useRef<TradingEngine | null>(null);
  const [botState, setBotState] = useState<BotState | null>(null);
  const [botEvents, setBotEvents] = useState<TradeEvent[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [isRunning, setIsRunning] = useState(false);

  // Initialize engine
  useEffect(() => {
    const config: Partial<BotConfig> = {
      symbol,
      initialBalance: 1000,
      leverage: 10,
      riskPerTrade: 0.01,
      paperTrading: true,
    };
    const engine = new TradingEngine(config);

    engine.onUpdate((state, events) => {
      setBotState(state);
      setPositions(botStateToPositions(state));
      setOrders(botStateToOrders(state));
      setFills(botHistoryToFills(state));
      setIsRunning(state.status === "running");
      if (events.length > 0) {
        setBotEvents((prev) => [...events, ...prev].slice(0, 100));
      }
    });

    engineRef.current = engine;

    return () => {
      engine.stop();
    };
  }, [symbol]);

  const start = useCallback(() => {
    engineRef.current?.start();
  }, []);

  const pause = useCallback(() => {
    engineRef.current?.pause();
  }, []);

  const resume = useCallback(() => {
    engineRef.current?.resume();
  }, []);

  const stop = useCallback(() => {
    engineRef.current?.stop();
  }, []);

  const reset = useCallback(() => {
    engineRef.current?.reset();
    setBotEvents([]);
  }, []);

  const manualTick = useCallback(() => {
    engineRef.current?.tick();
  }, []);

  return {
    botState,
    botEvents,
    positions,
    orders,
    fills,
    isRunning,
    start,
    pause,
    resume,
    stop,
    reset,
    manualTick,
  };
}
