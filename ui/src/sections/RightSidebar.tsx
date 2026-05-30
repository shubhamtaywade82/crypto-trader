import { useState } from "react";
import type { OrderBookEntry, WatchlistItem, OrderSide, OrderType } from "@/types";

interface RightSidebarProps {
  selectedSymbol: string;
  selectedTicker: WatchlistItem | undefined;
  bids: OrderBookEntry[];
  asks: OrderBookEntry[];
  realizedBalance: number;
}

function formatNum(num: number, dec = 2): string {
  if (num == null || isNaN(num)) return "0.00";
  return num.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

export function RightSidebar({ selectedSymbol, selectedTicker, bids, asks, realizedBalance }: RightSidebarProps) {
  const [orderSide, setOrderSide] = useState<OrderSide>("buy");
  const [orderType, setOrderType] = useState<OrderType>("market");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderQty, setOrderQty] = useState("0.1");
  const [leverage, setLeverage] = useState(20);

  const price = orderType === "limit" ? Number(orderPrice) : (selectedTicker?.price || 0);
  const qty = Number(orderQty) || 0;
  const orderCost = price * qty / leverage;
  const estFee = price * qty * 0.0004;

  const handlePctClick = (pct: number) => {
    const p = orderType === "limit" ? Number(orderPrice) : (selectedTicker?.price || 0);
    if (p > 0) {
      setOrderQty(((realizedBalance * pct * leverage) / p).toFixed(3));
    }
  };

  return (
    <aside className="flex flex-col border-l border-[var(--border-main)] bg-[var(--bg-panel)] h-full overflow-hidden">
      {/* Order Entry Panel */}
      <div className="border-b border-[var(--border-main)] bg-[var(--bg-panel)] flex flex-col">
        {/* Buy/Sell Tabs */}
        <div className="flex bg-[var(--bg-header)] border-b border-[var(--border-main)]">
          <button
            className={`flex-1 h-8 border-none bg-transparent text-[11px] font-extrabold uppercase cursor-pointer transition-all duration-150 ${
              orderSide === "buy"
                ? "bg-[var(--bg-up-subtle)] text-[var(--color-up)] border-b-2 border-b-[var(--color-up)]"
                : "text-[var(--text-secondary)] hover:text-[var(--text-bright)]"
            }`}
            onClick={() => setOrderSide("buy")}
          >
            Buy / Long
          </button>
          <button
            className={`flex-1 h-8 border-none bg-transparent text-[11px] font-extrabold uppercase cursor-pointer transition-all duration-150 ${
              orderSide === "sell"
                ? "bg-[var(--bg-down-subtle)] text-[var(--color-down)] border-b-2 border-b-[var(--color-down)]"
                : "text-[var(--text-secondary)] hover:text-[var(--text-bright)]"
            }`}
            onClick={() => setOrderSide("sell")}
          >
            Sell / Short
          </button>
        </div>

        {/* Order Form */}
        <form className="p-3 flex flex-col gap-2.5" onSubmit={(e) => e.preventDefault()}>
          {/* Market/Limit Switch */}
          <div className="flex bg-[var(--bg-main)] border border-[var(--border-main)] p-0.5 rounded w-full">
            <button
              type="button"
              className={`flex-1 py-[3px] border-none bg-transparent text-[10px] font-bold rounded-[3px] transition-all duration-150 uppercase cursor-pointer ${
                orderType === "market" ? "bg-blue-500/15 text-blue-400" : "text-[var(--text-muted)] hover:text-[var(--text-bright)]"
              }`}
              onClick={() => setOrderType("market")}
            >
              Market
            </button>
            <button
              type="button"
              className={`flex-1 py-[3px] border-none bg-transparent text-[10px] font-bold rounded-[3px] transition-all duration-150 uppercase cursor-pointer ${
                orderType === "limit" ? "bg-blue-500/15 text-blue-400" : "text-[var(--text-muted)] hover:text-[var(--text-bright)]"
              }`}
              onClick={() => setOrderType("limit")}
            >
              Limit
            </button>
          </div>

          {/* Price Field */}
          {orderType === "limit" && (
            <div className="flex flex-col gap-1">
              <span className="text-[10px] uppercase text-[var(--text-secondary)] font-semibold">Limit Price</span>
              <div className="flex relative bg-[var(--bg-terminal)] border border-[var(--border-main)] rounded items-center focus-within:border-[var(--border-active)]">
                <input
                  type="text"
                  className="flex-1 bg-transparent border-none text-[var(--text-bright)] font-[var(--font-mono)] font-bold text-xs px-2 py-1.5 outline-none w-full"
                  value={orderPrice}
                  onChange={(e) => setOrderPrice(e.target.value)}
                />
                <span className="pr-2 text-[var(--text-muted)] text-[10px] font-bold">USDT</span>
              </div>
            </div>
          )}

          {/* Quantity Field */}
          <div className="flex flex-col gap-1">
            <span className="text-[10px] uppercase text-[var(--text-secondary)] font-semibold">Order Quantity</span>
            <div className="flex relative bg-[var(--bg-terminal)] border border-[var(--border-main)] rounded items-center focus-within:border-[var(--border-active)]">
              <input
                type="text"
                className="flex-1 bg-transparent border-none text-[var(--text-bright)] font-[var(--font-mono)] font-bold text-xs px-2 py-1.5 outline-none w-full"
                value={orderQty}
                onChange={(e) => setOrderQty(e.target.value)}
              />
              <span className="pr-2 text-[var(--text-muted)] text-[10px] font-bold">{selectedSymbol.replace("USDT", "")}</span>
            </div>
          </div>

          {/* Leverage Slider */}
          <div className="flex flex-col gap-1.5 mt-1">
            <div className="flex justify-between text-[10px] text-[var(--text-muted)]">
              <span className="uppercase text-[var(--text-secondary)] font-semibold">Leverage</span>
              <span className="font-bold text-[var(--color-warn)]">{leverage}x</span>
            </div>
            <input
              type="range"
              min={1}
              max={125}
              value={leverage}
              onChange={(e) => setLeverage(Number(e.target.value))}
              className="w-full cursor-pointer accent-blue-500"
            />
          </div>

          {/* Percentage Badges */}
          <div className="flex gap-1.5">
            {[0.25, 0.5, 0.75, 0.95].map((pct) => (
              <div
                key={pct}
                className="flex-1 bg-[var(--bg-terminal)] border border-[var(--border-main)] rounded text-[var(--text-secondary)] text-[9px] font-bold py-[3px] text-center cursor-pointer select-none hover:bg-[var(--bg-subtle)] hover:text-[var(--text-bright)] transition-colors"
                onClick={() => handlePctClick(pct)}
              >
                {Math.round(pct * 100)}%
              </div>
            ))}
          </div>

          {/* Order Summary */}
          <div className="bg-[var(--bg-terminal)] border border-[var(--border-subtle)] rounded p-1.5 flex flex-col gap-1 mt-1">
            <div className="flex justify-between text-[11px]">
              <span className="text-[var(--text-secondary)]">Order Cost:</span>
              <span className="font-bold font-[var(--font-mono)] text-[var(--text-bright)]">{formatNum(orderCost, 2)} USDT</span>
            </div>
            <div className="flex justify-between text-[11px]">
              <span className="text-[var(--text-secondary)]">Est. Fee (0.04%):</span>
              <span className="font-bold font-[var(--font-mono)] text-[var(--text-muted)]">{formatNum(estFee, 4)} USDT</span>
            </div>
          </div>

          {/* Submit Button */}
          <button
            type="button"
            className="w-full h-8 rounded border text-xs font-extrabold uppercase mt-1.5 transition-[filter] duration-150 hover:brightness-110 cursor-not-allowed"
            style={{
              backgroundColor: "var(--bg-terminal)",
              color: "var(--text-muted)",
              border: "1px solid var(--border-main)",
            }}
            disabled
          >
            Manual Entry Locked (Engine Running)
          </button>
        </form>
      </div>

      {/* Order Book Panel */}
      <div className="flex-1 flex flex-col overflow-hidden bg-[var(--bg-panel)]">
        {/* Order Book Header */}
        <div className="flex justify-between items-center h-8 min-h-[32px] bg-[var(--bg-header)] border-b border-[var(--border-main)] px-2.5">
          <h3 className="text-[10px] font-bold uppercase tracking-[0.06em] text-[var(--text-bright)]">Live Order Book</h3>
          <span className="px-1 py-0.5 text-[9px] font-bold rounded bg-white/5 border border-white/[0.08] text-[var(--text-secondary)]">
            BIDS / ASKS
          </span>
        </div>

        <div className="flex-1 flex flex-col py-1.5 font-[var(--font-mono)] text-[11px] overflow-y-auto">
          {/* OB Header */}
          <div className="grid grid-cols-3 text-[var(--text-muted)] text-[9px] px-2.5 pb-1 uppercase font-bold">
            <span>Price (USDT)</span>
            <span className="text-right">Size ({selectedSymbol.replace("USDT", "")})</span>
            <span className="text-right">Total</span>
          </div>

          {/* Asks (reversed) */}
          <div className="flex flex-col-reverse gap-px">
            {asks.map((ask, i) => (
              <div key={`ask-${i}`} className="grid grid-cols-3 px-2.5 py-0.5 relative items-center hover:bg-[var(--bg-subtle)]">
                <span className="z-[2] font-semibold text-[var(--color-down)]">{ask.price.toFixed(ask.price > 10 ? 2 : 4)}</span>
                <span className="z-[2] text-right text-[var(--text-primary)]">{ask.qty.toFixed(3)}</span>
                <span className="z-[2] text-right text-[var(--text-secondary)]">{ask.total.toFixed(3)}</span>
                <div className="absolute top-0 bottom-0 right-0 z-[1] pointer-events-none" style={{ width: `${Math.min((ask.total / 15) * 100, 100)}%`, backgroundColor: "rgba(255, 61, 0, 0.07)" }} />
              </div>
            ))}
          </div>

          {/* Spread */}
          <div className="flex justify-between items-center bg-[var(--bg-header)] border-y border-[var(--border-subtle)] py-1 px-2.5 my-1 text-[11px]">
            <span className="text-[var(--text-muted)] text-[9px] uppercase font-bold">Spread</span>
            <span className={`font-bold text-xs ${selectedTicker && selectedTicker.price >= selectedTicker.prevPrice ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
              {selectedTicker ? selectedTicker.price.toFixed(selectedSymbol.includes("XRP") ? 4 : 2) : "—"}
            </span>
            <span className="text-[var(--text-muted)] text-[9px] uppercase font-bold">
              {asks.length > 0 && bids.length > 0 ? Math.abs(asks[asks.length - 1]?.price - bids[0]?.price).toFixed(2) : "0.00"}
            </span>
          </div>

          {/* Bids */}
          <div className="flex flex-col gap-px">
            {bids.map((bid, i) => (
              <div key={`bid-${i}`} className="grid grid-cols-3 px-2.5 py-0.5 relative items-center hover:bg-[var(--bg-subtle)]">
                <span className="z-[2] font-semibold text-[var(--color-up)]">{bid.price.toFixed(bid.price > 10 ? 2 : 4)}</span>
                <span className="z-[2] text-right text-[var(--text-primary)]">{bid.qty.toFixed(3)}</span>
                <span className="z-[2] text-right text-[var(--text-secondary)]">{bid.total.toFixed(3)}</span>
                <div className="absolute top-0 bottom-0 right-0 z-[1] pointer-events-none" style={{ width: `${Math.min((bid.total / 15) * 100, 100)}%`, backgroundColor: "rgba(0, 230, 118, 0.07)" }} />
              </div>
            ))}
          </div>
        </div>
      </div>
    </aside>
  );
}
