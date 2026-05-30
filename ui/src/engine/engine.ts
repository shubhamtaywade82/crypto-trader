/**
 * Trading Engine — Main Orchestrator
 * DataFeed → RegimeAnalyzer → Supertrend2 Strategy → Risk → PositionManager
 */

import type { Candle } from "./indicators";
import { computeEMA } from "./indicators";
import { analyzeRegime, MarketRegime } from "./regime";
import { evaluateSupertrend2, checkFlipExit, DEFAULT_ST2_CONFIG, type Supertrend2Config, type TradeSetup } from "./supertrend2";
import { canTrade, updatePeakBalance, recordTradeOpen, recordTradeClose, calculatePositionSize, createRiskState, DEFAULT_RISK_CONFIG } from "./risk";
import { openPosition, updatePosition, closePosition, type Position, type PositionUpdate } from "./position";
import type { RiskConfig, RiskState } from "./risk";

export type BotStatus = "idle" | "running" | "paused" | "halted";

export interface BotConfig {
  symbol: string;
  initialBalance: number;
  leverage: number;
  riskPerTrade: number; // e.g., 0.01 = 1%
  risk: RiskConfig;
  supertrend: Supertrend2Config;
  paperTrading: boolean;
}

export const DEFAULT_BOT_CONFIG: BotConfig = {
  symbol: "BTCUSDT",
  initialBalance: 1000,
  leverage: 10,
  riskPerTrade: 0.01,
  risk: DEFAULT_RISK_CONFIG,
  supertrend: DEFAULT_ST2_CONFIG,
  paperTrading: true,
};

export interface BotState {
  status: BotStatus;
  balance: number;
  unrealizedPnl: number;
  realizedPnl: number;
  marginBalance: number;
  availableBalance: number;
  utilizedMargin: number;
  openPositions: Position[];
  positionHistory: Position[];
  dailyTrades: number;
  totalTrades: number;
  regime: string;
  regimeScore: number;
  lastSignal: TradeSetup | null;
  lastSignalTime: number | null;
  lastError: string | null;
  tick: number;
  riskState: RiskState;
}

export interface TradeEvent {
  type: "OPENED" | "CLOSED" | "BLOCKED" | "SIGNAL" | "ERROR" | "RISK";
  timestamp: number;
  symbol: string;
  message: string;
  pnl?: number;
  setup?: TradeSetup;
  position?: Position;
}

export class TradingEngine {
  private config: BotConfig;
  private state: BotState;
  private candles: Candle[] = [];
  private htfCandles: Candle[] = [];
  private listeners: Set<(state: BotState, events: TradeEvent[]) => void> = new Set();
  private intervalId: ReturnType<typeof setInterval> | null = null;
  private ema9: number[] = [];

  constructor(config: Partial<BotConfig> = {}) {
    this.config = { ...DEFAULT_BOT_CONFIG, ...config };
    this.state = this.createInitialState();
  }

  private createInitialState(): BotState {
    return {
      status: "idle",
      balance: this.config.initialBalance,
      unrealizedPnl: 0,
      realizedPnl: 0,
      marginBalance: this.config.initialBalance,
      availableBalance: this.config.initialBalance,
      utilizedMargin: 0,
      openPositions: [],
      positionHistory: [],
      dailyTrades: 0,
      totalTrades: 0,
      regime: MarketRegime.UNKNOWN,
      regimeScore: 0,
      lastSignal: null,
      lastSignalTime: null,
      lastError: null,
      tick: 0,
      riskState: createRiskState(),
    };
  }

  // ── Public API ──

  getState(): BotState {
    return { ...this.state };
  }

  getConfig(): BotConfig {
    return { ...this.config };
  }

  updateConfig(config: Partial<BotConfig>) {
    this.config = { ...this.config, ...config };
  }

  onUpdate(callback: (state: BotState, events: TradeEvent[]) => void) {
    this.listeners.add(callback);
    return () => this.listeners.delete(callback);
  }

  /** Start the trading loop */
  start() {
    if (this.state.status === "running") return;
    this.state.status = "running";
    this.emitUpdate([]);

    // Run immediately
    this.tick();

    // Then every 30 seconds
    this.intervalId = setInterval(() => this.tick(), 30000);
  }

  /** Pause the bot */
  pause() {
    if (this.state.status !== "running") return;
    this.state.status = "paused";
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
    this.emitUpdate([]);
  }

  /** Resume */
  resume() {
    if (this.state.status !== "paused") return;
    this.state.status = "running";
    this.tick();
    this.intervalId = setInterval(() => this.tick(), 30000);
  }

  /** Stop and reset */
  stop() {
    this.state.status = "idle";
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
    this.emitUpdate([]);
  }

  /** Reset everything */
  reset() {
    this.stop();
    this.state = this.createInitialState();
    this.candles = [];
    this.htfCandles = [];
    this.ema9 = [];
    this.emitUpdate([]);
  }

  /** Manual tick for testing */
  async tick() {
    if (this.state.status !== "running") return;
    this.state.tick++;
    const events: TradeEvent[] = [];

    try {
      // 1. Fetch data
      const fetched = await this.fetchData();
      if (!fetched) {
        events.push({ type: "ERROR", timestamp: Date.now(), symbol: this.config.symbol, message: "Failed to fetch data" });
        this.emitUpdate(events);
        return;
      }

      // 2. Regime analysis
      const regime = analyzeRegime(
        this.candles.map((c) => c.high),
        this.candles.map((c) => c.low),
        this.candles.map((c) => c.close),
        this.candles.map((c) => c.volume)
      );
      this.state.regime = regime.regime;
      this.state.regimeScore = regime.score;

      // Compute EMA9 for trailing stops
      this.ema9 = computeEMA(this.candles.map((c) => c.close), 9);
      const ema9Val = this.ema9[this.ema9.length - 1];

      const markPrice = this.candles[this.candles.length - 1].close;
      const candleTime = this.candles[this.candles.length - 1].time;

      // 3. Risk check
      updatePeakBalance(this.state.riskState, this.state.marginBalance);
      const riskCheck = canTrade(this.state.riskState, this.config.risk, this.state.marginBalance);

      // 4. Update existing positions
      for (const pos of this.state.openPositions) {
        if (pos.status !== "OPEN") continue;

        // Check Supertrend flip exit
        if (checkFlipExit(this.candles, pos.side)) {
          const update = closePosition(pos, markPrice, "FLIP_EXIT");
          this.applyClose(update, events);
          continue;
        }

        const update = updatePosition(
          pos,
          markPrice,
          candleTime,
          this.config.risk.catastrophicSLPct,
          this.config.risk.windfallROMPct,
          this.state.regime,
          ema9Val
        );

        if (update) {
          if (update.event === "CLOSED" || update.event === "SL_HIT" || update.event === "TP_HIT" ||
              update.event === "TRAILING_STOP" || update.event === "TIME_STOP" || update.event === "FLIP_EXIT" ||
              update.event === "CATASTROPHIC_SL" || update.event === "WINDFALL" || update.event === "PEAK_PNL" ||
              update.event === "AI_EXIT" || update.event === "REGIME_EXIT" || update.event === "LIQUIDATED") {
            this.applyClose(update, events);
          }
        }
      }

      // Recalculate totals
      this.recalcTotals();

      // 5. Evaluate entry (only if no open position on this symbol)
      const hasOpen = this.state.openPositions.some((p) => p.status === "OPEN");

      if (!hasOpen && riskCheck.allowed) {
        const setup = evaluateSupertrend2(this.candles, this.config.supertrend, this.htfCandles);

        if (setup) {
          this.state.lastSignal = setup;
          this.state.lastSignalTime = Date.now();
          events.push({
            type: "SIGNAL",
            timestamp: Date.now(),
            symbol: this.config.symbol,
            message: `ST2 ${setup.side} @ ${setup.entryPrice.toFixed(2)} | Score: ${setup.score.toFixed(2)}`,
            setup,
          });

          // Execute
          this.executeEntry(setup, markPrice, candleTime, events);
        }
      } else if (!hasOpen && !riskCheck.allowed) {
        events.push({
          type: "RISK",
          timestamp: Date.now(),
          symbol: this.config.symbol,
          message: riskCheck.reason,
        });
      }

      // Check halted
      if (this.state.riskState.halted) {
        this.state.status = "halted";
        if (this.intervalId) {
          clearInterval(this.intervalId);
          this.intervalId = null;
        }
      }

      this.emitUpdate(events);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.state.lastError = msg;
      events.push({ type: "ERROR", timestamp: Date.now(), symbol: this.config.symbol, message: msg });
      this.emitUpdate(events);
    }
  }

  // ── Private methods ──

  private async fetchData(): Promise<boolean> {
    try {
      // Fetch 1H candles
      const res1h = await fetch(
        `https://fapi.binance.com/fapi/v1/klines?symbol=${this.config.symbol}&interval=1h&limit=150`
      );
      if (!res1h.ok) return false;
      const raw1h = await res1h.json() as [number, string, string, string, string, string][];
      this.candles = raw1h.map((k) => ({
        time: k[0],
        open: parseFloat(k[1]),
        high: parseFloat(k[2]),
        low: parseFloat(k[3]),
        close: parseFloat(k[4]),
        volume: parseFloat(k[5]),
      }));

      // Fetch 4H candles for HTF filter
      const res4h = await fetch(
        `https://fapi.binance.com/fapi/v1/klines?symbol=${this.config.symbol}&interval=4h&limit=150`
      );
      if (res4h.ok) {
        const raw4h = await res4h.json() as [number, string, string, string, string, string][];
        this.htfCandles = raw4h.map((k) => ({
          time: k[0],
          open: parseFloat(k[1]),
          high: parseFloat(k[2]),
          low: parseFloat(k[3]),
          close: parseFloat(k[4]),
          volume: parseFloat(k[5]),
        }));
      }

      return this.candles.length > 0;
    } catch {
      return false;
    }
  }

  private executeEntry(setup: TradeSetup, _markPrice: number, candleTime: number, events: TradeEvent[]) {
    // Fee (taker)
    const notional = setup.entryPrice * 0.01; // placeholder qty, will recalculate
    const fee = notional * 0.0005;

    // Calculate position size
    const qty = calculatePositionSize(
      this.state.balance,
      this.config.riskPerTrade,
      setup.entryPrice,
      setup.slPrice,
      this.config.leverage,
      setup.score,
      0.6 // dynamic threshold
    );

    if (qty <= 0) {
      events.push({
        type: "BLOCKED",
        timestamp: Date.now(),
        symbol: this.config.symbol,
        message: "Position size calculation returned 0",
      });
      return;
    }

    const pos = openPosition(
      this.config.symbol,
      setup.side,
      setup.entryPrice,
      qty,
      this.config.leverage,
      setup.slPrice,
      setup.tpPrice,
      setup.timeStopHours,
      candleTime,
      fee
    );

    this.state.openPositions.push(pos);
    recordTradeOpen(this.state.riskState);
    this.state.totalTrades++;

    events.push({
      type: "OPENED",
      timestamp: Date.now(),
      symbol: this.config.symbol,
      message: `${setup.side} ${qty.toFixed(4)} @ ${setup.entryPrice.toFixed(2)} | SL: ${setup.slPrice.toFixed(2)}${setup.tpPrice ? ` | TP: ${setup.tpPrice.toFixed(2)}` : ""}`,
      position: pos,
    });
  }

  private applyClose(update: PositionUpdate, events: TradeEvent[]) {
    recordTradeClose(this.state.riskState, update.pnl);

    // Credit PnL
    this.state.balance += update.pnl;
    this.state.realizedPnl += update.pnl;

    events.push({
      type: "CLOSED",
      timestamp: Date.now(),
      symbol: update.position.symbol,
      message: `${update.event} | ${update.position.side} | PnL: ${update.pnl >= 0 ? "+" : ""}${update.pnl.toFixed(2)} USDT`,
      pnl: update.pnl,
      position: update.position,
    });

    // Move to history
    this.state.positionHistory.push(update.position);
  }

  private recalcTotals() {
    let unrealized = 0;
    let utilized = 0;

    for (const pos of this.state.openPositions) {
      if (pos.status === "OPEN") {
        unrealized += pos.unrealizedPnl;
        utilized += pos.marginUsed;
      }
    }

    this.state.unrealizedPnl = unrealized;
    this.state.marginBalance = this.state.balance + unrealized;
    this.state.availableBalance = Math.max(0, this.state.marginBalance - utilized);
    this.state.utilizedMargin = utilized;

    // Clean up closed positions
    this.state.openPositions = this.state.openPositions.filter((p) => p.status === "OPEN");
  }

  private emitUpdate(events: TradeEvent[]) {
    for (const listener of this.listeners) {
      listener(this.getState(), events);
    }
  }
}
