/**
 * Risk Management System
 * Controls position sizing, drawdown limits, daily trade limits, cooldowns
 */

export interface RiskConfig {
  maxDailyTrades: number;
  maxConsecutiveLosses: number;
  maxDrawdownPct: number; // e.g., 0.15 = 15%
  maxDailyDrawdownPct: number; // e.g., 0.08 = 8%
  cooldownAfterLossMinutes: number;
  maxOrdersPerMinute: number;
  maxMarginRatio: number;
  equityUtilization: number;
  catastrophicSLPct: number; // e.g., -0.50 = -50% of margin
  windfallROMPct: number; // e.g., 0.40 = 40% return on margin
}

export const DEFAULT_RISK_CONFIG: RiskConfig = {
  maxDailyTrades: 10,
  maxConsecutiveLosses: 4,
  maxDrawdownPct: 0.15,
  maxDailyDrawdownPct: 0.08,
  cooldownAfterLossMinutes: 30,
  maxOrdersPerMinute: 3,
  maxMarginRatio: 0.8,
  equityUtilization: 0.5,
  catastrophicSLPct: -0.5,
  windfallROMPct: 0.4,
};

export interface RiskState {
  dailyTrades: number;
  consecutiveLosses: number;
  peakBalance: number;
  dailyStartBalance: number;
  lastLossTime: number | null;
  ordersThisMinute: number;
  lastMinuteReset: number;
  halted: boolean;
  haltReason: string;
}

export function createRiskState(): RiskState {
  return {
    dailyTrades: 0,
    consecutiveLosses: 0,
    peakBalance: 0,
    dailyStartBalance: 0,
    lastLossTime: null,
    ordersThisMinute: 0,
    lastMinuteReset: Date.now(),
    halted: false,
    haltReason: "",
  };
}

/** Check if trading is allowed */
export function canTrade(state: RiskState, config: RiskConfig, currentBalance: number): { allowed: boolean; reason: string } {
  if (state.halted) {
    return { allowed: false, reason: `HALTED: ${state.haltReason}` };
  }

  const now = Date.now();

  // Reset daily stats if new day
  const lastDay = state.lastMinuteReset ? new Date(state.lastMinuteReset).getDate() : null;
  const today = new Date(now).getDate();
  if (lastDay !== null && lastDay !== today) {
    state.dailyTrades = 0;
    state.dailyStartBalance = currentBalance;
  }

  // Reset orders per minute
  if (now - state.lastMinuteReset > 60000) {
    state.ordersThisMinute = 0;
    state.lastMinuteReset = now;
  }

  // Daily trade limit
  if (state.dailyTrades >= config.maxDailyTrades) {
    return { allowed: false, reason: `Daily trade limit reached (${state.dailyTrades}/${config.maxDailyTrades})` };
  }

  // Consecutive losses
  if (state.consecutiveLosses >= config.maxConsecutiveLosses) {
    return { allowed: false, reason: `Max consecutive losses (${state.consecutiveLosses}/${config.maxConsecutiveLosses})` };
  }

  // Drawdown
  if (state.peakBalance > 0) {
    const drawdown = (state.peakBalance - currentBalance) / state.peakBalance;
    if (drawdown >= config.maxDrawdownPct) {
      state.halted = true;
      state.haltReason = `Max drawdown reached (${(drawdown * 100).toFixed(2)}%)`;
      return { allowed: false, reason: state.haltReason };
    }
  }

  // Daily drawdown
  if (state.dailyStartBalance > 0) {
    const dailyDD = (state.dailyStartBalance - currentBalance) / state.dailyStartBalance;
    if (dailyDD >= config.maxDailyDrawdownPct) {
      state.halted = true;
      state.haltReason = `Max daily drawdown reached (${(dailyDD * 100).toFixed(2)}%)`;
      return { allowed: false, reason: state.haltReason };
    }
  }

  // Cooldown after loss
  if (state.lastLossTime && config.cooldownAfterLossMinutes > 0) {
    const minsSinceLoss = (now - state.lastLossTime) / 60000;
    if (minsSinceLoss < config.cooldownAfterLossMinutes) {
      return { allowed: false, reason: `Cooldown: ${(config.cooldownAfterLossMinutes - minsSinceLoss).toFixed(1)}m remaining` };
    }
  }

  // Orders per minute
  if (state.ordersThisMinute >= config.maxOrdersPerMinute) {
    return { allowed: false, reason: `Rate limit: ${state.ordersThisMinute} orders/min` };
  }

  return { allowed: true, reason: "OK" };
}

/** Update peak balance tracking */
export function updatePeakBalance(state: RiskState, balance: number) {
  if (balance > state.peakBalance) {
    state.peakBalance = balance;
  }
  if (state.dailyStartBalance === 0) {
    state.dailyStartBalance = balance;
  }
}

/** Record a trade open */
export function recordTradeOpen(state: RiskState) {
  state.dailyTrades++;
  state.ordersThisMinute++;
}

/** Record a trade close */
export function recordTradeClose(state: RiskState, pnl: number) {
  if (pnl < 0) {
    state.consecutiveLosses++;
    state.lastLossTime = Date.now();
  } else {
    state.consecutiveLosses = 0;
  }
}

/** Calculate position size based on risk budget and stop distance */
export function calculatePositionSize(
  balance: number,
  riskPerTrade: number, // e.g., 0.01 = 1% of balance
  entryPrice: number,
  slPrice: number,
  leverage: number,
  score: number,
  threshold: number
): number {
  const stopDistance = Math.abs(entryPrice - slPrice);
  const riskBudget = balance * riskPerTrade;
  const sizeMultiplier = Math.min(score / threshold, 1.0);
  const adjustedRisk = riskBudget * sizeMultiplier;
  const quantity = stopDistance > 0 ? (adjustedRisk / stopDistance) : 0;
  const maxNotional = balance * leverage;
  const maxQty = maxNotional / entryPrice;
  return Math.min(quantity, maxQty);
}
