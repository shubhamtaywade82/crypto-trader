/**
 * Supertrend2 Strategy
 * Adaptive ATR-based trend following strategy
 */

import {
  computeSupertrend2,
  adaptiveATRLength,
  computeRVOL,
  wilderATR,
  type Candle,
} from "./indicators";


export type PositionSide = "LONG" | "SHORT";

export interface TradeSetup {
  side: PositionSide;
  entryPrice: number;
  slPrice: number;
  tpPrice: number | null;
  score: number;
  reason: string;
  playbook: "INTRADAY" | "SWING";
  timeStopHours: number;
}

export interface Supertrend2Config {
  atrPeriod: number;
  factor: number;
  useAdaptiveATR: boolean;
  timeframe: string;
  htfTimeframe: string;
  entryMode: "flip" | "retracement";
  retracementPct: number;
  flipLookback: number;
  useHTFFilter: boolean;
  useVolFilter: boolean;
  volMultiplier: number;
  useADXFilter: boolean;
  adxThreshold: number;
  tp1Pct: number;
  useTP: boolean;
  slMode: "supertrend" | "fixed_pct" | "atr";
  slPct: number;
  slATRMult: number;
  maxHoldHours: number;
}

export const DEFAULT_ST2_CONFIG: Supertrend2Config = {
  atrPeriod: 10,
  factor: 3.0,
  useAdaptiveATR: true,
  timeframe: "1h",
  htfTimeframe: "4h",
  entryMode: "flip",
  retracementPct: 1.0,
  flipLookback: 5,
  useHTFFilter: true,
  useVolFilter: false,
  volMultiplier: 1.2,
  useADXFilter: false,
  adxThreshold: 20,
  tp1Pct: 1.0,
  useTP: true,
  slMode: "supertrend",
  slPct: 1.5,
  slATRMult: 2.5,
  maxHoldHours: 168, // 7 days
};

function effectiveATRLen(config: Supertrend2Config): number {
  return config.useAdaptiveATR
    ? adaptiveATRLength(config.atrPeriod, config.timeframe)
    : config.atrPeriod;
}

function minBars(config: Supertrend2Config): number {
  return Math.max(effectiveATRLen(config) * 3, 20);
}

/** Evaluate Supertrend2 strategy on given candles */
export function evaluateSupertrend2(
  candles: Candle[],
  config: Supertrend2Config = DEFAULT_ST2_CONFIG,
  htfCandles?: Candle[]
): TradeSetup | null {
  const mb = minBars(config);
  if (candles.length < mb + 2) return null;

  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);
  const closes = candles.map((c) => c.close);

  const atrLen = effectiveATRLen(config);
  const { supertrend, direction } = computeSupertrend2(highs, lows, closes, config.factor, atrLen);

  const currDir = direction[direction.length - 1];
  const prevDir = direction[direction.length - 2];
  const currST = supertrend[supertrend.length - 1];
  const currClose = closes[closes.length - 1];

  // HTF Filter
  if (config.useHTFFilter && htfCandles && htfCandles.length >= mb + 2) {
    const htfHighs = htfCandles.map((c) => c.high);
    const htfLows = htfCandles.map((c) => c.low);
    const htfCloses = htfCandles.map((c) => c.close);
    const htfAtrLen = config.useAdaptiveATR
      ? adaptiveATRLength(config.atrPeriod, config.htfTimeframe)
      : config.atrPeriod;
    const { direction: htfDir } = computeSupertrend2(htfHighs, htfLows, htfCloses, config.factor, htfAtrLen);
    const htfCurr = htfDir[htfDir.length - 1];
    if (currDir !== htfCurr) return null;
  }

  // Volume Filter
  if (config.useVolFilter) {
    const volumes = candles.map((c) => c.volume);
    const rvol = computeRVOL(volumes, 20);
    const lastRVOL = rvol[rvol.length - 1];
    if (!isNaN(lastRVOL) && lastRVOL < config.volMultiplier) return null;
  }

  // Entry signal
  let side: PositionSide | null = null;

  if (config.entryMode === "flip") {
    if (currDir === -1 && prevDir === 1) {
      side = "LONG";
    } else if (currDir === 1 && prevDir === -1) {
      side = "SHORT";
    }
  } else if (config.entryMode === "retracement") {
    side = retracementSignal(candles, config, currDir, currClose);
  }

  if (!side) return null;

  // SL calculation
  const atrVals = wilderATR(highs, lows, closes, atrLen);
  const atrVal = atrVals[atrVals.length - 1] || 0;
  const isLong = side === "LONG";
  const entryPrice = currClose;

  let sl: number;
  if (config.slMode === "supertrend") {
    sl = currST;
  } else if (config.slMode === "atr") {
    sl = isLong ? entryPrice - atrVal * config.slATRMult : entryPrice + atrVal * config.slATRMult;
  } else {
    sl = isLong ? entryPrice * (1 - config.slPct / 100) : entryPrice * (1 + config.slPct / 100);
  }

  // Guard: SL must be on the correct side
  if (isLong && sl >= entryPrice) sl = entryPrice * 0.99;
  if (!isLong && sl <= entryPrice) sl = entryPrice * 1.01;

  // TP calculation
  let tp: number | null = null;
  if (config.useTP && config.tp1Pct > 0) {
    tp = isLong ? entryPrice * (1 + config.tp1Pct / 100) : entryPrice * (1 - config.tp1Pct / 100);
  }

  const setup: TradeSetup = {
    side,
    entryPrice,
    slPrice: sl,
    tpPrice: tp,
    score: 1.0,
    reason: `ST2 ${side} | mode=${config.entryMode} factor=${config.factor} atr_len=${atrLen} st=${currST.toFixed(4)} sl_mode=${config.slMode}`,
    playbook: "INTRADAY",
    timeStopHours: config.maxHoldHours,
  };

  return setup;
}

/** Check for retracement entry signal */
function retracementSignal(
  candles: Candle[],
  config: Supertrend2Config,
  _currDir: number,
  currClose: number
): PositionSide | null {
  const lookback = Math.min(config.flipLookback + 1, candles.length);
  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);
  const closes = candles.map((c) => c.close);
  const atrLen = effectiveATRLen(config);

  const { direction: dirSeries } = computeSupertrend2(highs, lows, closes, config.factor, atrLen);
  const dirs = dirSeries.slice(-lookback);
  const closeSlice = closes.slice(-lookback);

  for (let i = dirs.length - 2; i > 0; i--) {
    const d_i = dirs[i];
    const d_prev = dirs[i - 1];

    if (d_i === d_prev) continue;

    const flipPrice = closeSlice[i];

    // Check direction hasn't flipped again
    let stillValid = true;
    for (let j = i; j < dirs.length; j++) {
      if (dirs[j] !== d_i) {
        stillValid = false;
        break;
      }
    }
    if (!stillValid) break;

    const lo = flipPrice * (1 - config.retracementPct / 100);
    const hi = flipPrice * (1 + config.retracementPct / 100);

    if (d_i === -1 && lo <= currClose && currClose <= hi) return "LONG";
    if (d_i === 1 && lo <= currClose && currClose <= hi) return "SHORT";

    break; // Only use most recent flip
  }

  return null;
}

/** Get current Supertrend direction */
export function getSupertrendDirection(candles: Candle[], config: Supertrend2Config = DEFAULT_ST2_CONFIG): number | null {
  if (candles.length < minBars(config)) return null;
  const highs = candles.map((c) => c.high);
  const lows = candles.map((c) => c.low);
  const closes = candles.map((c) => c.close);
  const atrLen = effectiveATRLen(config);
  const { direction } = computeSupertrend2(highs, lows, closes, config.factor, atrLen);
  return direction[direction.length - 1];
}

/** Check if Supertrend has flipped against the position */
export function checkFlipExit(candles: Candle[], side: PositionSide, config: Supertrend2Config = DEFAULT_ST2_CONFIG): boolean {
  const dir = getSupertrendDirection(candles, config);
  if (dir === null) return false;
  if (side === "LONG" && dir === 1) return true; // flipped to downtrend
  if (side === "SHORT" && dir === -1) return true; // flipped to uptrend
  return false;
}
