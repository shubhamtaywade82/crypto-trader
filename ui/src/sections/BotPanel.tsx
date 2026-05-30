import { useState } from "react";
import type { BotState, TradeEvent, BotStatus } from "@/engine/engine";

interface BotPanelProps {
  botState: BotState | null;
  botEvents: TradeEvent[];
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onReset: () => void;
  onManualTick: () => void;
}

function formatNum(num: number, dec = 2): string {
  if (num == null || isNaN(num)) return "0.00";
  return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function statusColor(status: BotStatus): string {
  switch (status) {
    case "running": return "text-[var(--color-up)]";
    case "paused": return "text-[var(--color-warn)]";
    case "halted": return "text-[var(--color-down)]";
    default: return "text-[var(--text-muted)]";
  }
}

function statusBg(status: BotStatus): string {
  switch (status) {
    case "running": return "bg-[rgba(0,230,118,0.08)] border-[rgba(0,230,118,0.2)]";
    case "paused": return "bg-[rgba(255,171,0,0.08)] border-[rgba(255,171,0,0.2)]";
    case "halted": return "bg-[rgba(255,61,0,0.08)] border-[rgba(255,61,0,0.2)]";
    default: return "bg-white/[0.02] border-[var(--border-subtle)]";
  }
}

export function BotPanel({ botState, botEvents, onStart, onPause, onResume, onStop, onReset, onManualTick }: BotPanelProps) {
  const [showEvents, setShowEvents] = useState(false);

  if (!botState) {
    return (
      <div className="bg-[var(--bg-panel)] border border-[var(--border-main)] rounded p-3">
        <div className="text-[var(--text-muted)] text-[11px] text-center py-4">Loading bot engine...</div>
      </div>
    );
  }

  const status = botState.status;
  const winTrades = botState.positionHistory.filter((p) => (p.realizedPnl || 0) > 0).length;
  const lossTrades = botState.positionHistory.filter((p) => (p.realizedPnl || 0) < 0).length;
  const winRate = botState.positionHistory.length > 0 ? (winTrades / botState.positionHistory.length * 100) : 0;

  return (
    <div className="flex flex-col gap-2">
      {/* Status Card */}
      <div className={`border rounded p-2.5 ${statusBg(status)}`}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${
              status === "running" ? "bg-[var(--color-up)] animate-[pulse-green_2s_infinite]" :
              status === "paused" ? "bg-[var(--color-warn)]" :
              status === "halted" ? "bg-[var(--color-down)]" :
              "bg-[var(--text-muted)]"
            }`} />
            <span className={`text-[11px] font-bold uppercase ${statusColor(status)}`}>
              {status}
            </span>
          </div>
          <span className="text-[9px] text-[var(--text-muted)] font-[var(--font-mono)]">
            Tick #{botState.tick}
          </span>
        </div>

        {/* Controls */}
        <div className="flex gap-1.5 mb-2">
          {status === "idle" && (
            <button onClick={onStart} className="flex-1 h-7 bg-[var(--color-up)] text-black text-[10px] font-extrabold uppercase rounded border-none cursor-pointer hover:brightness-110 transition-all">
              Start Bot
            </button>
          )}
          {status === "running" && (
            <button onClick={onPause} className="flex-1 h-7 bg-[var(--color-warn)] text-black text-[10px] font-extrabold uppercase rounded border-none cursor-pointer hover:brightness-110 transition-all">
              Pause
            </button>
          )}
          {status === "paused" && (
            <>
              <button onClick={onResume} className="flex-1 h-7 bg-[var(--color-up)] text-black text-[10px] font-extrabold uppercase rounded border-none cursor-pointer hover:brightness-110 transition-all">
                Resume
              </button>
              <button onClick={onStop} className="flex-1 h-7 bg-[var(--color-down)] text-white text-[10px] font-extrabold uppercase rounded border-none cursor-pointer hover:brightness-110 transition-all">
                Stop
              </button>
            </>
          )}
          {(status === "halted" || status === "idle") && (
            <button onClick={onReset} className="flex-1 h-7 bg-[var(--bg-subtle)] text-[var(--text-secondary)] text-[10px] font-extrabold uppercase rounded border border-[var(--border-main)] cursor-pointer hover:text-[var(--text-bright)] transition-all">
              Reset
            </button>
          )}
          <button onClick={onManualTick} className="h-7 px-2 bg-[var(--bg-subtle)] text-[var(--text-muted)] text-[10px] font-bold rounded border border-[var(--border-main)] cursor-pointer hover:text-[var(--text-bright)] transition-all" title="Manual Tick">
            T
          </button>
        </div>

        {/* Metrics Grid */}
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[10px]">
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">Balance</span>
            <span className="font-[var(--font-mono)] font-bold text-[var(--text-bright)]">{formatNum(botState.balance)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">U-PnL</span>
            <span className={`font-[var(--font-mono)] font-bold ${botState.unrealizedPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
              {botState.unrealizedPnl >= 0 ? "+" : ""}{formatNum(botState.unrealizedPnl)}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">R-PnL</span>
            <span className={`font-[var(--font-mono)] font-bold ${botState.realizedPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
              {botState.realizedPnl >= 0 ? "+" : ""}{formatNum(botState.realizedPnl)}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">Margin</span>
            <span className="font-[var(--font-mono)] font-bold text-[var(--text-bright)]">{formatNum(botState.marginBalance)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">Trades</span>
            <span className="font-[var(--font-mono)] font-bold text-blue-400">{botState.totalTrades}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">Win Rate</span>
            <span className="font-[var(--font-mono)] font-bold text-blue-400">{winRate.toFixed(0)}%</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">W/L</span>
            <span className="font-[var(--font-mono)] font-bold text-[var(--color-up)]">{winTrades}</span>
            <span className="text-[var(--text-muted)]">/</span>
            <span className="font-[var(--font-mono)] font-bold text-[var(--color-down)]">{lossTrades}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--text-muted)]">Open</span>
            <span className="font-[var(--font-mono)] font-bold text-[var(--text-bright)]">{botState.openPositions.filter((p) => p.status === "OPEN").length}</span>
          </div>
        </div>

        {/* Regime */}
        <div className="flex justify-between items-center mt-2 pt-1.5 border-t border-[var(--border-subtle)]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-bold">Regime</span>
          <span className="text-[10px] font-bold font-[var(--font-mono)]">
            <span className={
              botState.regime.includes("UP") ? "text-[var(--color-up)]" :
              botState.regime.includes("DOWN") ? "text-[var(--color-down)]" :
              "text-[var(--text-muted)]"
            }>
              {botState.regime.replace("TRENDING_", "")}
            </span>
            <span className="text-[var(--text-muted)] ml-1">({(botState.regimeScore * 100).toFixed(0)}%)</span>
          </span>
        </div>

        {/* Last Signal */}
        {botState.lastSignal && (
          <div className="mt-1.5 pt-1.5 border-t border-[var(--border-subtle)]">
            <div className="text-[9px] text-[var(--text-muted)] uppercase font-bold mb-0.5">Last Signal</div>
            <div className="text-[10px] font-[var(--font-mono)]">
              <span className={botState.lastSignal.side === "LONG" ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}>
                {botState.lastSignal.side}
              </span>
              <span className="text-[var(--text-secondary)] ml-1">@{botState.lastSignal.entryPrice.toFixed(2)}</span>
              <span className="text-[var(--text-muted)] ml-1">SL:{botState.lastSignal.slPrice.toFixed(2)}</span>
              {botState.lastSignal.tpPrice && (
                <span className="text-[var(--text-muted)] ml-1">TP:{botState.lastSignal.tpPrice.toFixed(2)}</span>
              )}
            </div>
          </div>
        )}

        {/* Halt reason */}
        {status === "halted" && botState.riskState.haltReason && (
          <div className="mt-1.5 p-1.5 bg-[var(--color-down)]/10 border border-[var(--color-down)]/20 rounded text-[10px] text-[var(--color-down)] font-bold">
            {botState.riskState.haltReason}
          </div>
        )}

        {/* Last error */}
        {botState.lastError && (
          <div className="mt-1.5 p-1.5 bg-[var(--color-down)]/10 border border-[var(--color-down)]/20 rounded text-[9px] text-[var(--color-down)]">
            {botState.lastError}
          </div>
        )}
      </div>

      {/* Events Log */}
      <div className="bg-[var(--bg-panel)] border border-[var(--border-main)] rounded overflow-hidden">
        <button
          onClick={() => setShowEvents(!showEvents)}
          className="w-full flex justify-between items-center h-7 px-2.5 bg-[var(--bg-header)] border-b border-[var(--border-main)] text-[10px] font-bold uppercase cursor-pointer hover:bg-[var(--bg-subtle)] transition-colors"
        >
          <span className="text-[var(--text-bright)]">Bot Events</span>
          <span className="text-[var(--text-muted)]">{botEvents.length} events</span>
        </button>
        {showEvents && (
          <div className="max-h-[200px] overflow-y-auto font-[var(--font-mono)] text-[9px] p-1.5">
            {botEvents.length === 0 ? (
              <div className="text-[var(--text-muted)] text-center py-2 italic">No events yet</div>
            ) : (
              botEvents.map((ev, i) => {
                const colorClass =
                  ev.type === "OPENED" ? "text-[var(--color-up)]" :
                  ev.type === "CLOSED" ? (ev.pnl && ev.pnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]") :
                  ev.type === "SIGNAL" ? "text-blue-400" :
                  ev.type === "BLOCKED" ? "text-[var(--color-warn)]" :
                  ev.type === "RISK" ? "text-[var(--color-warn)]" :
                  ev.type === "ERROR" ? "text-[var(--color-down)]" :
                  "text-[var(--text-muted)]";
                return (
                  <div key={i} className="py-0.5 border-b border-white/[0.02] last:border-0">
                    <span className="text-[var(--text-muted)]">[{new Date(ev.timestamp).toLocaleTimeString()}]</span>{" "}
                    <span className={`font-bold ${colorClass}`}>{ev.type}</span>{" "}
                    <span className="text-[var(--text-secondary)]">{ev.message}</span>
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>
    </div>
  );
}
