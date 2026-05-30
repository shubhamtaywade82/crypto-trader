import type { WatchlistItem, VenueHealth, AppMode } from "@/types";

interface LeftSidebarProps {
  watchlist: WatchlistItem[];
  selectedSymbol: string;
  onSelectSymbol: (sym: string) => void;
  mode: AppMode;
  venueHealth: VenueHealth | null;
  realizedBalance: number;
  unrealizedPnl: number;
  equity: number;
}

function formatNum(num: number, dec = 2): string {
  if (num == null || isNaN(num)) return "0.00";
  return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function getSparklinePath(points: number[], width = 60, height = 18): string {
  if (points.length < 2) return "";
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  return points.map((p, i) => {
    const x = (i / (points.length - 1)) * width;
    const y = height - ((p - min) / range) * height;
    return `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

export function LeftSidebar({ watchlist, selectedSymbol, onSelectSymbol, mode, venueHealth, realizedBalance, unrealizedPnl, equity }: LeftSidebarProps) {
  return (
    <aside className="flex flex-col border-r border-[var(--border-main)] bg-[var(--bg-panel)] h-full overflow-hidden">
      {/* Watchlist Header */}
      <div className="flex justify-between items-center h-8 min-h-[32px] bg-[var(--bg-header)] border-b border-[var(--border-main)] px-2.5">
        <h3 className="text-[10px] font-bold uppercase tracking-[0.06em] text-[var(--text-bright)]">Markets Watchlist</h3>
        <span className="px-1 py-0.5 text-[9px] font-bold rounded bg-white/5 border border-white/[0.08] text-[var(--text-secondary)]">
          {watchlist.length} Symbols
        </span>
      </div>

      {/* Watchlist Items */}
      <div className="flex-1 overflow-y-auto">
        {watchlist.map((coin) => {
          const flashClass = coin.flashState === "up" ? "flash-up" : coin.flashState === "down" ? "flash-down" : "";
          const isSelected = selectedSymbol === coin.symbol;
          return (
            <div
              key={coin.symbol}
              className={`grid grid-cols-[80px_75px_65px_1fr] items-center px-2.5 py-1.5 border-b border-[var(--border-subtle)] cursor-pointer transition-colors duration-150 hover:bg-[var(--bg-subtle)] ${
                isSelected ? "bg-blue-500/[0.08] border-l-2 border-l-[var(--border-active)] pl-2" : ""
              } ${flashClass}`}
              onClick={() => onSelectSymbol(coin.symbol)}
            >
              <div className="font-bold text-[var(--text-bright)] text-[11px]">{coin.symbol}</div>
              <div className={`font-[var(--font-mono)] font-bold text-[11px] text-right pr-2 ${coin.price >= coin.prevPrice ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                {coin.price.toFixed(coin.price > 10 ? 2 : 4)}
              </div>
              <div className={`font-[var(--font-mono)] text-[10px] font-semibold text-right ${coin.change24h >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                {coin.change24h >= 0 ? "+" : ""}{coin.change24h.toFixed(2)}%
              </div>
              <div className={`flex items-center justify-end h-5 pl-1.5 ${coin.change24h >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                <svg width="60" height="18">
                  <path
                    d={getSparklinePath(coin.sparkline)}
                    fill="none"
                    strokeWidth="1.5"
                    style={{
                      stroke: "currentColor",
                      filter: coin.change24h >= 0
                        ? "drop-shadow(0 0 2px rgba(0,230,118,0.4))"
                        : "drop-shadow(0 0 2px rgba(255,61,0,0.4))",
                    }}
                  />
                </svg>
              </div>
            </div>
          );
        })}
      </div>

      {/* Account Widget */}
      <div className="bg-[var(--bg-header)] border-t border-[var(--border-main)] px-2.5 py-2 flex flex-col gap-1.5">
        <h3 className="text-[9px] text-[var(--text-secondary)] font-bold">Wallet Breakdowns</h3>
        <div className="flex justify-between text-[11px]">
          <span className="text-[var(--text-secondary)]">Margin Balance:</span>
          <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(realizedBalance, 2)} USDT</span>
        </div>
        <div className="flex justify-between text-[11px]">
          <span className="text-[var(--text-secondary)]">Unrealized PnL:</span>
          <span className={`font-bold font-[var(--font-mono)] ${unrealizedPnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
            {unrealizedPnl >= 0 ? "+" : ""}{formatNum(unrealizedPnl, 2)} USDT
          </span>
        </div>
        <div className="flex justify-between text-[11px] border-t border-[var(--border-subtle)] pt-1 mt-0.5">
          <span className="text-[var(--text-secondary)] font-bold">Account Equity:</span>
          <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(equity, 2)} USDT</span>
        </div>
      </div>

      {/* Venue Health (Live only) */}
      {mode === "live" && (
        <div className="bg-[var(--bg-header)] border-t border-[var(--color-warn)]/20 px-2.5 py-2 flex flex-col gap-1 mt-1">
          <h3 className="text-[9px] text-[var(--color-warn)] font-bold mb-1">Authoritative Venue Health</h3>
          {venueHealth?.ok ? (
            <>
              <div className="flex justify-between text-[11px]">
                <span className="text-[var(--text-secondary)]">Venue PnL:</span>
                <span className={`font-bold font-[var(--font-mono)] ${(venueHealth.details?.pnl || 0) >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                  {formatNum(venueHealth.details?.pnl || 0, 4)} USDT
                </span>
              </div>
              <div className="flex justify-between text-[11px]">
                <span className="text-[var(--text-secondary)]">Margin Ratio:</span>
                <span className={`font-bold font-[var(--font-mono)] ${(venueHealth.details?.margin_ratio_cross || 0) > 0.8 ? "text-[var(--color-down)]" : "text-[var(--color-up)]"}`}>
                  {((venueHealth.details?.margin_ratio_cross || 0) * 100).toFixed(2)}%
                </span>
              </div>
              <div className="flex justify-between text-[11px]">
                <span className="text-[var(--text-secondary)]">Maint. Margin:</span>
                <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(venueHealth.details?.maintenance_margin || 0, 4)} USDT</span>
              </div>
              <div className="flex justify-between text-[11px]">
                <span className="text-[var(--text-secondary)]">Total Equity:</span>
                <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(venueHealth.details?.total_account_equity || 0, 2)} USDT</span>
              </div>
              <div className="flex justify-between text-[7px] text-[var(--text-muted)] mt-1 border-t border-[var(--border-subtle)] pt-0.5">
                <span>Last Sync:</span>
                <span>{venueHealth.details?.updated_at ? new Date(venueHealth.details.updated_at).toLocaleTimeString() : "—"}</span>
              </div>
            </>
          ) : (
            <div className="text-[9px] text-[var(--text-muted)] p-1">{venueHealth?.error || "Fetching venue metrics..."}</div>
          )}
        </div>
      )}
    </aside>
  );
}
