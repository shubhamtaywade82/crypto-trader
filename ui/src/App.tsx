import { createSignal, createResource, For, onMount, onCleanup } from 'solid-js';
import './App.css';

const API_BASE = "http://localhost:8088";

interface Position {
  symbol: string;
  side: string;
  qty: number;
  avg_price: number;
  status: string;
  updated_at: string;
}

interface Order {
  exchange_order_id: string;
  symbol: string;
  side: string;
  qty: number;
  status: string;
  created_at: number;
}

function App() {
  const [mode, setMode] = createSignal("paper");
  const [events, setEvents] = createSignal<any[]>([]);
  
  const fetchPositions = async (m: string): Promise<Position[]> => {
    console.log("[API] Fetching positions for mode:", m);
    try {
        const res = await fetch(`${API_BASE}/positions?mode=${m}`);
        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
        const data = await res.json();
        console.log("[API] Positions data:", data);
        return data;
    } catch (e) {
        console.error("[API] Fetch positions failed:", e);
        return [];
    }
  };

  const fetchOrders = async (m: string): Promise<Order[]> => {
    console.log("[API] Fetching orders for mode:", m);
    try {
        const res = await fetch(`${API_BASE}/orders?mode=${m}`);
        if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
        const data = await res.json();
        console.log("[API] Orders data:", data);
        return data;
    } catch (e) {
        console.error("[API] Fetch orders failed:", e);
        return [];
    }
  };

  const [positions, { refetch: refetchPositions }] = createResource(mode, fetchPositions);
  const [orders, { refetch: refetchOrders }] = createResource(mode, fetchOrders);

  // SSE Setup
  onMount(() => {
    console.log("[SSE] Connecting to:", `${API_BASE}/events/stream`);
    const eventSource = new EventSource(`${API_BASE}/events/stream`);
    
    eventSource.onopen = () => console.log("✅ [SSE] Connected");
    eventSource.onerror = (err) => console.error("❌ [SSE] Error:", err);
    
    eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        console.log("[SSE] Message:", data);
        setEvents(prev => [data, ...prev].slice(0, 50));
        
        // Match both 'ORDER_FILLED' and 'POSITION_OPENED' types
        const et = (data.event_type || "").toUpperCase();
        if (et.includes("ORDER") || et.includes("TRADE") || et.includes("POSITION") || et.includes("FILL")) {
          console.log(`🔄 [SSE] Triggering refetch (Event: ${et})`);
          refetchPositions();
          refetchOrders();
        }
      } catch (err) {
        console.error("[SSE] Parse error:", err);
      }
    };
    onCleanup(() => {
        console.log("[SSE] Closing connection");
        eventSource.close();
    });
  });

  const formatTs = (e: any) => {
    // Handle both 'ts' (ms) and 'timestamp' (s)
    const val = e.ts || (e.timestamp ? e.timestamp * 1000 : null);
    if (!val) return "—";
    return new Date(val).toLocaleTimeString();
  };

  return (
    <div class="container">
      <header>
        <h1>Crypto Trader v4 Dashboard</h1>
        <div class="mode-toggle">
          <button 
            class={`mode-btn ${mode() === 'paper' ? 'active' : ''}`} 
            onClick={() => setMode("paper")}
          >
            PAPER
          </button>
          <button 
            class={`mode-btn ${mode() === 'live' ? 'active' : ''}`} 
            onClick={() => setMode("live")}
          >
            LIVE
          </button>
        </div>
      </header>

      <div class="grid">
        <div class="card">
          <h3>Active Positions</h3>
          <div class="value">{positions()?.length || 0}</div>
        </div>
        <div class="card">
          <h3>Live Feed</h3>
          <div class="value">{events().length} Events</div>
        </div>
      </div>

      <section>
        <h2>Positions</h2>
        <div class="card">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Avg Price</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              <For each={positions() || []}>
                {(p: any) => (
                  <tr>
                    <td>{p.symbol}</td>
                    <td class={p.side?.toLowerCase() || ""}>{p.side}</td>
                    <td>{p.qty}</td>
                    <td>{p.avg_price}</td>
                    <td><span class={`status-tag status-${p.status?.toLowerCase() || ""}`}>{p.status}</span></td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
          {positions.loading && <div style="text-align: center; padding: 1rem; color: #666;">Loading positions...</div>}
          {!positions.loading && positions()?.length === 0 && <div style="text-align: center; padding: 1rem; color: #666;">No active positions</div>}
        </div>
      </section>

      <div class="grid" style="margin-top: 2rem; grid-template-columns: 2fr 1fr;">
        <section>
          <h2>Recent Orders</h2>
          <div class="card">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Qty</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <For each={orders() || []}>
                  {(o: any) => (
                    <tr>
                      <td style="font-size: 0.7rem;">{o.exchange_order_id}</td>
                      <td>{o.symbol}</td>
                      <td class={o.side?.toLowerCase() || ""}>{o.side}</td>
                      <td>{o.qty}</td>
                      <td>{o.status}</td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
            {orders.loading && <div style="text-align: center; padding: 1rem; color: #666;">Loading orders...</div>}
            {!orders.loading && orders()?.length === 0 && <div style="text-align: center; padding: 1rem; color: #666;">No recent orders</div>}
          </div>
        </section>

        <section>
          <h2>Live Events</h2>
          <div class="event-list">
            <For each={events()}>
              {(e) => (
                <div class="event-item">
                  <span class="event-ts">[{formatTs(e)}]</span>{" "}
                  <span class="event-type">{e.event_type || 'Event'}</span>
                  <div style="color: #ccc; margin-top: 2px; font-size: 0.7rem; white-space: pre-wrap; overflow-x: hidden;">
                    {JSON.stringify(e.payload || e, (k,v) => (k === 'ts' || k === 'timestamp' || k === 'event_type' || k === 'namespace') ? undefined : v, 2)}
                  </div>
                </div>
              )}
            </For>
            {events().length === 0 && <div style="text-align: center; padding: 1rem; color: #666;">Waiting for events...</div>}
          </div>
        </section>
      </div>
    </div>
  );
}

export default App;
