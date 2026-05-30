/**
 * Technical Indicators for the Trading Bot
 * Implements Supertrend2, Wilder's ATR, EMA, and ADX
 */

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Wilder's RMA-smoothed ATR (matches Pine Script customATR) */
export function wilderATR(highs: number[], lows: number[], closes: number[], length: number): number[] {
  const n = highs.length;
  if (n === 0) return [];

  const tr = new Float64Array(n);
  tr[0] = highs[0] - lows[0];
  for (let i = 1; i < n; i++) {
    tr[i] = Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i] - closes[i - 1])
    );
  }

  const alpha = 1.0 / Math.max(length, 1);
  const atr = new Float64Array(n);
  atr[0] = tr[0];
  for (let i = 1; i < n; i++) {
    atr[i] = atr[i - 1] + alpha * (tr[i] - atr[i - 1]);
  }
  return Array.from(atr);
}

/** Adaptive ATR length based on timeframe */
export function adaptiveATRLength(base: number, timeframe: string): number {
  const tf = timeframe.toLowerCase().trim();

  if (tf.endsWith("m")) {
    const mins = parseInt(tf.slice(0, -1), 10);
    if (isNaN(mins)) return base;
    if (mins <= 1) return base;
    if (mins <= 5) return Math.max(1, Math.round(base * 1.2));
    if (mins <= 15) return Math.max(1, Math.round(base * 1.5));
    if (mins <= 60) return Math.max(1, Math.round(base * 2.0));
    return Math.max(1, Math.round(base * 3.0));
  }

  if (tf.endsWith("h")) {
    const h = parseInt(tf.slice(0, -1), 10);
    if (isNaN(h)) return base;
    if (h <= 1) return Math.max(1, Math.round(base * 2.0));
    if (h <= 4) return Math.max(1, Math.round(base * 3.0));
    return Math.max(1, Math.round(base * 4.0));
  }

  if (tf === "1d" || tf === "d" || tf === "day" || tf === "1day") {
    return Math.max(1, Math.round(base * 4.0));
  }
  if (tf === "1w" || tf === "w" || tf === "week" || tf === "1week") {
    return Math.max(1, Math.round(base * 8.0));
  }

  return base;
}

export interface SupertrendResult {
  supertrend: number[];
  direction: number[]; // -1 = uptrend, +1 = downtrend
}

/** Supertrend2 — replicates Pine Script supertrend2() function */
export function computeSupertrend2(
  highs: number[],
  lows: number[],
  closes: number[],
  factor: number = 3.0,
  atrLength: number = 10
): SupertrendResult {
  const n = highs.length;
  if (n === 0) return { supertrend: [], direction: [] };

  const atr = wilderATR(highs, lows, closes, atrLength);
  const src = highs.map((h, i) => (h + lows[i]) / 2.0);

  const upperBasic = src.map((s, i) => s + factor * atr[i]);
  const lowerBasic = src.map((s, i) => s - factor * atr[i]);

  const upper = new Float64Array(n);
  const lower = new Float64Array(n);
  const direction = new Int8Array(n);
  const st = new Float64Array(n);

  upper[0] = upperBasic[0];
  lower[0] = lowerBasic[0];
  st[0] = lower[0];
  direction[0] = -1;

  for (let i = 1; i < n; i++) {
    const prevClose = closes[i - 1];
    const prevUpper = upper[i - 1];
    const prevLower = lower[i - 1];

    if (prevClose > prevUpper) {
      direction[i] = -1;
      st[i] = lowerBasic[i];
      upper[i] = upperBasic[i];
      lower[i] = lowerBasic[i];
    } else if (prevClose < prevLower) {
      direction[i] = 1;
      st[i] = upperBasic[i];
      upper[i] = upperBasic[i];
      lower[i] = lowerBasic[i];
    } else {
      direction[i] = direction[i - 1];
      if (direction[i] === -1) {
        lower[i] = Math.max(lowerBasic[i], prevLower);
        upper[i] = upperBasic[i];
        st[i] = lower[i];
      } else {
        upper[i] = Math.min(upperBasic[i], prevUpper);
        lower[i] = lowerBasic[i];
        st[i] = upper[i];
      }
    }
  }

  return {
    supertrend: Array.from(st),
    direction: Array.from(direction),
  };
}

/** EMA calculation */
export function computeEMA(values: number[], period: number): number[] {
  const n = values.length;
  if (n === 0) return [];
  const alpha = 2 / (period + 1);
  const ema = new Float64Array(n);
  ema[0] = values[0];
  for (let i = 1; i < n; i++) {
    ema[i] = values[i] * alpha + ema[i - 1] * (1 - alpha);
  }
  return Array.from(ema);
}

/** Simple Moving Average */
export function computeSMA(values: number[], period: number): number[] {
  const result: number[] = [];
  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) {
      result.push(NaN);
    } else {
      let sum = 0;
      for (let j = i - period + 1; j <= i; j++) {
        sum += values[j];
      }
      result.push(sum / period);
    }
  }
  return result;
}

/** ADX (Average Directional Index) */
export function computeADX(highs: number[], lows: number[], closes: number[], period: number = 14): number[] {
  const n = highs.length;
  if (n < 2) return new Array(n).fill(NaN);

  const tr: number[] = [highs[0] - lows[0]];
  const plusDM: number[] = [0];
  const minusDM: number[] = [0];

  for (let i = 1; i < n; i++) {
    tr.push(Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1])));
    const upMove = highs[i] - highs[i - 1];
    const downMove = lows[i - 1] - lows[i];
    plusDM.push(upMove > downMove && upMove > 0 ? upMove : 0);
    minusDM.push(downMove > upMove && downMove > 0 ? downMove : 0);
  }

  const atr = wilderATR(highs, lows, closes, period);
  const alpha = 1.0 / period;

  const plusDI: number[] = [];
  const minusDI: number[] = [];
  const dx: number[] = [];
  const adx: number[] = new Array(n).fill(NaN);

  for (let i = 0; i < n; i++) {
    if (atr[i] === 0) {
      plusDI.push(0);
      minusDI.push(0);
    } else {
      plusDI.push((100 * plusDM[i]) / atr[i]);
      minusDI.push((100 * minusDM[i]) / atr[i]);
    }
    const sum = plusDI[i] + minusDI[i];
    dx.push(sum === 0 ? 0 : (100 * Math.abs(plusDI[i] - minusDI[i])) / sum);
  }

  // Smooth DX with Wilder's RMA
  let smoothedDx = 0;
  let count = 0;
  for (let i = 0; i < n; i++) {
    if (!isNaN(dx[i])) {
      if (count === 0) {
        smoothedDx = dx[i];
      } else {
        smoothedDx = smoothedDx + alpha * (dx[i] - smoothedDx);
      }
      count++;
      if (count >= period) {
        adx[i] = smoothedDx;
      }
    }
  }

  return adx;
}

/** RSI (Relative Strength Index) */
export function computeRSI(closes: number[], period: number = 14): number[] {
  const n = closes.length;
  if (n < 2) return new Array(n).fill(NaN);

  const gains: number[] = [0];
  const losses: number[] = [0];

  for (let i = 1; i < n; i++) {
    const change = closes[i] - closes[i - 1];
    gains.push(change > 0 ? change : 0);
    losses.push(change < 0 ? -change : 0);
  }

  const rsi = new Array(n).fill(NaN);
  let avgGain = 0;
  let avgLoss = 0;

  for (let i = 0; i < n; i++) {
    if (i < period) {
      avgGain += gains[i] / period;
      avgLoss += losses[i] / period;
    } else {
      avgGain = (avgGain * (period - 1) + gains[i]) / period;
      avgLoss = (avgLoss * (period - 1) + losses[i]) / period;
    }

    if (i >= period - 1) {
      if (avgLoss === 0) {
        rsi[i] = 100;
      } else {
        const rs = avgGain / avgLoss;
        rsi[i] = 100 - 100 / (1 + rs);
      }
    }
  }

  return rsi;
}

/** Bollinger Bands */
export function computeBollingerBands(closes: number[], period: number = 20, stdDev: number = 2) {
  const sma = computeSMA(closes, period);
  const upper: number[] = [];
  const lower: number[] = [];

  for (let i = 0; i < closes.length; i++) {
    if (isNaN(sma[i])) {
      upper.push(NaN);
      lower.push(NaN);
    } else {
      let sumSq = 0;
      for (let j = i - period + 1; j <= i; j++) {
        sumSq += Math.pow(closes[j] - sma[i], 2);
      }
      const sd = Math.sqrt(sumSq / period);
      upper.push(sma[i] + stdDev * sd);
      lower.push(sma[i] - stdDev * sd);
    }
  }

  return { middle: sma, upper, lower };
}

/** Relative Volume (RVOL) */
export function computeRVOL(volumes: number[], period: number = 20): number[] {
  const rvol: number[] = [];
  for (let i = 0; i < volumes.length; i++) {
    if (i < period) {
      rvol.push(NaN);
    } else {
      let avg = 0;
      for (let j = i - period + 1; j <= i; j++) {
        avg += volumes[j];
      }
      avg /= period;
      rvol.push(avg > 0 ? volumes[i] / avg : 1);
    }
  }
  return rvol;
}
