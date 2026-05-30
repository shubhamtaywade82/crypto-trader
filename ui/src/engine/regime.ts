/**
 * Market Regime Classifier
 * Analyzes price action to classify market as trending or ranging
 */

import { computeEMA, computeADX, computeBollingerBands } from "./indicators";

export const MarketRegime = {
  TRENDING_UP: "TRENDING_UP",
  TRENDING_DOWN: "TRENDING_DOWN",
  RANGING: "RANGING",
  UNKNOWN: "UNKNOWN",
} as const;

export type MarketRegimeType = (typeof MarketRegime)[keyof typeof MarketRegime];

export interface RegimeAnalysis {
  regime: MarketRegimeType;
  score: number; // 0-1, higher = stronger trend
  emaTrend: number; // EMA slope
  adxValue: number;
  bbWidth: number; // Bollinger Band width (volatility measure)
}

/** Classify market regime using EMA trend, ADX, and Bollinger Band width */
export function analyzeRegime(
  highs: number[],
  lows: number[],
  closes: number[],
  _volumes: number[]
): RegimeAnalysis {
  const n = closes.length;
  if (n < 50) {
    return { regime: MarketRegime.UNKNOWN, score: 0, emaTrend: 0, adxValue: 0, bbWidth: 0 };
  }

  // EMA trend
  const ema20 = computeEMA(closes, 20);
  const ema50 = computeEMA(closes, 50);
  const recentEma20 = ema20.slice(-10);
  const recentEma50 = ema50.slice(-10);

  const ema20Slope = recentEma20.length >= 2
    ? (recentEma20[recentEma20.length - 1] - recentEma20[0]) / recentEma20[0]
    : 0;
  const ema50Slope = recentEma50.length >= 2
    ? (recentEma50[recentEma50.length - 1] - recentEma50[0]) / recentEma50[0]
    : 0;

  const emaTrend = ema20Slope + ema50Slope * 0.5;

  // ADX
  const adx = computeADX(highs, lows, closes, 14);
  const adxValue = adx[adx.length - 1] || 0;

  // Bollinger Band width
  const bb = computeBollingerBands(closes, 20, 2);
  const lastMiddle = bb.middle[bb.middle.length - 1];
  const lastUpper = bb.upper[bb.upper.length - 1];
  const lastLower = bb.lower[bb.lower.length - 1];
  const bbWidth = lastMiddle > 0 ? (lastUpper - lastLower) / lastMiddle : 0;

  // Regime classification
  let regime: MarketRegimeType;
  let score: number;

  if (adxValue > 25) {
    // Trending market
    if (emaTrend > 0.001) {
      regime = MarketRegime.TRENDING_UP;
      score = Math.min(1, (adxValue / 50) + Math.abs(emaTrend) * 10);
    } else if (emaTrend < -0.001) {
      regime = MarketRegime.TRENDING_DOWN;
      score = Math.min(1, (adxValue / 50) + Math.abs(emaTrend) * 10);
    } else {
      regime = MarketRegime.RANGING;
      score = 0.3;
    }
  } else {
    // Ranging / weak trend
    if (Math.abs(emaTrend) < 0.0005 && bbWidth < 0.03) {
      regime = MarketRegime.RANGING;
      score = Math.min(1, (25 - adxValue) / 25 + (0.03 - bbWidth));
    } else if (emaTrend > 0) {
      regime = MarketRegime.TRENDING_UP;
      score = Math.min(0.5, adxValue / 50);
    } else {
      regime = MarketRegime.TRENDING_DOWN;
      score = Math.min(0.5, adxValue / 50);
    }
  }

  return { regime, score, emaTrend, adxValue, bbWidth };
}
