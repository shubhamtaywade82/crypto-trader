/**
 * Position Manager
 * Manages open positions, PnL, exits (SL, TP, trailing, time, flip)
 */

import type { PositionSide } from "./supertrend2";

export interface Position {
  id: string;
  symbol: string;
  side: PositionSide;
  entryPrice: number;
  quantity: number;
  marginUsed: number;
  leverage: number;
  slPrice: number;
  tpPrice: number | null;
  trailingActive: boolean;
  trailingStopPrice: number | null;
  highestPrice: number | null;
  lowestPrice: number | null;
  peakPnlPct: number;
  timeStopHours: number;
  openTime: number; // timestamp ms
  openCandleTime: number;
  status: "OPEN" | "CLOSED";
  closePrice?: number;
  closeTime?: number;
  closeReason?: string;
  realizedPnl: number;
  unrealizedPnl: number;
  fees: number;
}

export interface PositionUpdate {
  position: Position;
  event: "OPENED" | "CLOSED" | "PARTIAL_CLOSE" | "SL_HIT" | "TP_HIT" | "TRAILING_STOP" | "TIME_STOP" | "FLIP_EXIT" | "CATASTROPHIC_SL" | "WINDFALL" | "PEAK_PNL" | "AI_EXIT" | "REGIME_EXIT" | "LIQUIDATED";
  pnl: number;
}

let posIdCounter = 0;

export function generatePositionId(): string {
  return `pos_${++posIdCounter}_${Date.now().toString(36)}`;
}

/** Open a new position */
export function openPosition(
  symbol: string,
  side: PositionSide,
  entryPrice: number,
  quantity: number,
  leverage: number,
  slPrice: number,
  tpPrice: number | null,
  timeStopHours: number,
  candleCloseTime: number,
  fee: number
): Position {
  const pos: Position = {
    id: generatePositionId(),
    symbol,
    side,
    entryPrice,
    quantity,
    marginUsed: (entryPrice * quantity) / leverage,
    leverage,
    slPrice,
    tpPrice,
    trailingActive: false,
    trailingStopPrice: null,
    highestPrice: entryPrice,
    lowestPrice: entryPrice,
    peakPnlPct: 0,
    timeStopHours,
    openTime: Date.now(),
    openCandleTime: candleCloseTime,
    status: "OPEN",
    realizedPnl: 0,
    unrealizedPnl: 0,
    fees: fee,
  };
  return pos;
}

/** Update position PnL and check exit conditions */
export function updatePosition(
  pos: Position,
  markPrice: number,
  candleCloseTime: number,
  catastrophicSLPct: number,
  windfallROMPct: number,
  currentRegime: string | null,
  ema9: number | null
): PositionUpdate | null {
  if (pos.status !== "OPEN") return null;

  // Calculate unrealized PnL
  const notional = pos.quantity * markPrice;
  pos.marginUsed = notional / pos.leverage;

  if (pos.side === "LONG") {
    pos.unrealizedPnl = (markPrice - pos.entryPrice) * pos.quantity;
    if (pos.highestPrice === null || markPrice > pos.highestPrice) {
      pos.highestPrice = markPrice;
    }
  } else {
    pos.unrealizedPnl = (pos.entryPrice - markPrice) * pos.quantity;
    if (pos.lowestPrice === null || markPrice < pos.lowestPrice) {
      pos.lowestPrice = markPrice;
    }
  }

  // Peak PnL tracking
  const marginPnlPct = pos.marginUsed > 0 ? pos.unrealizedPnl / pos.marginUsed : 0;
  const priceChangePct = pos.entryPrice > 0 ? (markPrice - pos.entryPrice) / pos.entryPrice : 0;
  const undilutedPnl = pos.side === "LONG" ? priceChangePct : -priceChangePct;
  if (undilutedPnl > pos.peakPnlPct) {
    pos.peakPnlPct = undilutedPnl;
  }

  // 1. Liquidation check (approximate)
  const liquidationPrice = pos.side === "LONG"
    ? pos.entryPrice * (1 - 1 / pos.leverage)
    : pos.entryPrice * (1 + 1 / pos.leverage);
  if (
    (pos.side === "LONG" && markPrice <= liquidationPrice) ||
    (pos.side === "SHORT" && markPrice >= liquidationPrice)
  ) {
    return closePosition(pos, markPrice, "LIQUIDATED");
  }

  // 2. Catastrophic SL (-50% margin)
  const catSL = pos.marginUsed * catastrophicSLPct;
  if (pos.unrealizedPnl <= catSL) {
    return closePosition(pos, markPrice, "CATASTROPHIC_SL");
  }

  // 3. Windfall exit (40% ROM)
  if (marginPnlPct >= windfallROMPct) {
    return closePosition(pos, markPrice, "WINDFALL");
  }

  // 4. Peak PnL trailing stop (>= 2% peak, exit if drops 25%)
  if (pos.peakPnlPct >= 0.02) {
    const peakPnlValue = pos.peakPnlPct * pos.entryPrice * pos.quantity;
    const currentPnlValue = pos.unrealizedPnl;
    if (currentPnlValue <= peakPnlValue * 0.75) {
      return closePosition(pos, markPrice, "PEAK_PNL");
    }
  }

  // 5. Regime reversal exit
  if (currentRegime) {
    const isReversal =
      (pos.side === "LONG" && currentRegime === "TRENDING_DOWN") ||
      (pos.side === "SHORT" && currentRegime === "TRENDING_UP");
    if (isReversal) {
      const nearEntry = pos.side === "LONG"
        ? markPrice >= pos.entryPrice * 0.995
        : markPrice <= pos.entryPrice * 1.005;
      if (nearEntry) {
        return closePosition(pos, markPrice, "REGIME_EXIT");
      }
    }
  }

  // 6. TP hit
  if (pos.tpPrice !== null) {
    if (pos.side === "LONG" && markPrice >= pos.tpPrice) {
      return closePosition(pos, markPrice, "TP_HIT");
    }
    if (pos.side === "SHORT" && markPrice <= pos.tpPrice) {
      return closePosition(pos, markPrice, "TP_HIT");
    }
  }

  // 7. SL hit
  if (pos.side === "LONG" && markPrice <= pos.slPrice) {
    return closePosition(pos, markPrice, "SL_HIT");
  }
  if (pos.side === "SHORT" && markPrice >= pos.slPrice) {
    return closePosition(pos, markPrice, "SL_HIT");
  }

  // 8. Trailing stop (EMA9)
  if (pos.trailingActive && ema9 !== null) {
    if (pos.side === "LONG" && markPrice < ema9) {
      return closePosition(pos, markPrice, "TRAILING_STOP");
    }
    if (pos.side === "SHORT" && markPrice > ema9) {
      return closePosition(pos, markPrice, "TRAILING_STOP");
    }
  }

  // 9. Time stop
  const hoursOpen = (candleCloseTime - pos.openCandleTime) / 3600000;
  if (hoursOpen >= pos.timeStopHours) {
    return closePosition(pos, markPrice, "TIME_STOP");
  }

  return null; // No exit triggered
}

/** Close a position */
export function closePosition(pos: Position, markPrice: number, reason: string): PositionUpdate {
  pos.status = "CLOSED";
  pos.closePrice = markPrice;
  pos.closeTime = Date.now();
  pos.closeReason = reason;
  pos.realizedPnl = pos.unrealizedPnl;
  pos.unrealizedPnl = 0;

  let event: PositionUpdate["event"] = "CLOSED";
  if (reason === "SL_HIT") event = "SL_HIT";
  else if (reason === "TP_HIT") event = "TP_HIT";
  else if (reason === "TRAILING_STOP") event = "TRAILING_STOP";
  else if (reason === "TIME_STOP") event = "TIME_STOP";
  else if (reason === "FLIP_EXIT") event = "FLIP_EXIT";
  else if (reason === "CATASTROPHIC_SL") event = "CATASTROPHIC_SL";
  else if (reason === "WINDFALL") event = "WINDFALL";
  else if (reason === "PEAK_PNL") event = "PEAK_PNL";
  else if (reason === "AI_EXIT") event = "AI_EXIT";
  else if (reason === "REGIME_EXIT") event = "REGIME_EXIT";
  else if (reason === "LIQUIDATED") event = "LIQUIDATED";

  return { position: pos, event, pnl: pos.realizedPnl };
}

/** Activate trailing stop */
export function activateTrailingStop(pos: Position) {
  pos.trailingActive = true;
}

/** Adjust SL price */
export function adjustSL(pos: Position, newSL: number) {
  pos.slPrice = newSL;
}
