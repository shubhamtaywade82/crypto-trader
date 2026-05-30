import type { AppMode, ConnState } from "@/types";

interface HeaderProps {
  mode: AppMode;
  setMode: (m: AppMode) => void;
  equity: number;
  unrealizedPnl: number;
  realizedPnl: number;
  totalFees: number;
  activeTrades: number;
  connState: ConnState;
  lastEventAge: number | null;
}

function formatNum(num: number, dec = 2): string {
  if (num == null || isNaN(num)) return "0.00";
  return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function formatAge(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return "now";
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  return `${Math.round(sec / 60)}m ago`;
}

export function Header({ mode, setMode, equity, unrealizedPnl, realizedPnl, totalFees, activeTrades, connState, lastEventAge }: HeaderProps) {
  return (
    <header className="flex items-center h-12 min-h-[48px] bg-[var(--bg-header)] border-b border-[var(--border-main)] px-3 gap-4 z-10">
      {/* Brand */}
      <div className="flex items-center gap-2 pr-4 border-r border-[var(--border-main)]">
        <div className="flex items-center gap-1.5">
          <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" strokeWidth="2.5" fill="none" className="text-[var(--border-active)]" style={{ filter: "drop-shadow(0 0 4px rgba(59,130,246,0.6))" }}>
            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          <h2 className="m-0 text-sm font-extrabold tracking-[0.08em] bg-gradient-to-br from-white to-blue-400 bg-clip-text text-transparent">
            ANTIGRAVITY
          </h2>
        </div>
        <span className="px-1 py-0.5 text-[9px] font-bold rounded bg-white/5 border border-white/[0.08] text-[var(--text-secondary)]">
          v4.0 PRO
        </span>
      </div>

      {/* Metrics Ribbon */}
      <div className="flex items-center gap-[18px]">
        <div className="flex flex-col justify-center text-[11px]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-semibold tracking-[0.02em]">Equity (USDT)</span>
          <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(equity, 2)}</span>
        </div>
        <div className="flex flex-col justify-center text-[11px]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-semibold tracking-[0.02em]">Unrealized PnL</span>
          <span className={`font-bold font-[var(--font-mono)] ${unrealizedPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
            {unrealizedPnl >= 0 ? "+" : ""}{formatNum(unrealizedPnl, 2)}
          </span>
        </div>
        <div className="flex flex-col justify-center text-[11px]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-semibold tracking-[0.02em]">Realized PnL</span>
          <span className={`font-bold font-[var(--font-mono)] ${realizedPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
            {realizedPnl >= 0 ? "+" : ""}{formatNum(realizedPnl, 2)}
          </span>
        </div>
        <div className="flex flex-col justify-center text-[11px]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-semibold tracking-[0.02em]">Fees Paid</span>
          <span className="font-bold font-[var(--font-mono)] text-[var(--text-secondary)]">{formatNum(totalFees, 4)}</span>
        </div>
        <div className="flex flex-col justify-center text-[11px]">
          <span className="text-[9px] text-[var(--text-muted)] uppercase font-semibold tracking-[0.02em]">Active Trades</span>
          <span className="font-bold font-[var(--font-mono)] text-blue-400">{activeTrades}</span>
        </div>
      </div>

      {/* Controls */}
      <div className="ml-auto flex items-center gap-4">
        {/* Mode Toggle */}
        <div className="flex bg-[var(--bg-main)] border border-[var(--border-main)] p-0.5 rounded">
          <button
            className={`px-2 py-[3px] border-none bg-transparent text-[10px] font-bold rounded-[3px] transition-all duration-150 uppercase cursor-pointer ${
              mode === "paper" ? "bg-blue-500/15 text-blue-400" : "text-[var(--text-muted)] hover:text-[var(--text-bright)]"
            }`}
            onClick={() => setMode("paper")}
          >
            Paper
          </button>
          <button
            className={`px-2 py-[3px] border-none bg-transparent text-[10px] font-bold rounded-[3px] transition-all duration-150 uppercase cursor-pointer ${
              mode === "live" ? "bg-red-500/15 text-red-400" : "text-[var(--text-muted)] hover:text-[var(--text-bright)]"
            }`}
            onClick={() => setMode("live")}
          >
            Live
          </button>
        </div>

        {/* Connection Status */}
        <div className="flex items-center gap-1.5 text-[11px] pl-3 border-l border-[var(--border-main)]">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              connState === "live"
                ? "bg-[var(--color-up)] animate-[pulse-green_2s_infinite]"
                : connState === "connecting"
                ? "bg-blue-500 animate-[pulse-blue_1.5s_infinite]"
                : "bg-[var(--color-warn)] animate-[pulse-orange_1s_infinite]"
            }`}
            style={
              connState === "live"
                ? { boxShadow: "0 0 6px var(--color-up)" }
                : connState === "connecting"
                ? { boxShadow: "0 0 6px #3b82f6" }
                : { boxShadow: "0 0 6px var(--color-warn)" }
            }
          />
          <span className="uppercase font-bold text-[10px] tracking-[0.05em]">
            {connState === "live" ? "connected" : connState}
          </span>
          <span className="text-[var(--text-muted)] font-[var(--font-mono)] text-[10px]">
            BOT: {formatAge(lastEventAge)}
          </span>
        </div>
      </div>
    </header>
  );
}
