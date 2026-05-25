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
    const res = await fetch(`${API_BASE}/positions?mode=${m}`);
    return res.json();
  };

  const fetchOrders = async (m: string): Promise<Order[]> => {
    const res = await fetch(`${API_BASE}/orders?mode=${m}`);
    return res.json();
  };

  const [positions, { refetch: refetchPositions }] = createResource(mode, fetchPositions);
  const [orders, { refetch: refetchOrders }] = createResource(mode, fetchOrders);

  // SSE Setup
  onMount(() => {
    const eventSource = new EventSource(`${API_BASE}/events/stream`);
    eventSource.onmessage = (e) => {
      const data = JSON.parse(e.data);
      setEvents(prev => [data, ...prev].slice(0, 50));
      
      // Trigger refetch on certain events
      if (data.event_type?.includes("Order") || data.event_type?.includes("Trade")) {
        refetchPositions();
        refetchOrders();
      }
    };
    onCleanup(() => eventSource.close());
  });

  return (
    <div class="container">
      <header>
        <h1>Crypto Trader v4</h1>
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
          <h3>Total Realized PnL</h3>
          <div class="value">$0.00</div>
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
              <For each={positions()}>
                {(p: any) => (
                  <tr>
                    <td>{p.symbol}</td>
                    <td class={p.side.toLowerCase()}>{p.side}</td>
                    <td>{p.qty}</td>
                    <td>{p.avg_price}</td>
                    <td><span class={`status-tag status-${p.status.toLowerCase()}`}>{p.status}</span></td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
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
                <For each={orders()}>
                  {(o: any) => (
                    <tr>
                      <td style="font-size: 0.7rem;">{o.exchange_order_id}</td>
                      <td>{o.symbol}</td>
                      <td class={o.side.toLowerCase()}>{o.side}</td>
                      <td>{o.qty}</td>
                      <td>{o.status}</td>
                    </tr>
                  )}
                </For>
              </tbody>
            </table>
          </div>
        </section>

        <section>
          <h2>Live Events</h2>
          <div class="event-list">
            <For each={events()}>
              {(e) => (
                <div class="event-item">
                  <span class="event-ts">[{new Date(e.timestamp * 1000).toLocaleTimeString()}]</span>{" "}
                  <span class="event-type">{e.event_type || 'Event'}</span>
                  <div style="color: #ccc; margin-top: 2px;">{JSON.stringify(e, (k,v) => k === 'timestamp' || k === 'event_type' ? undefined : v)}</div>
                </div>
              )}
            </For>
          </div>
        </section>
      </div>
    </div>
  );
}

export default App;
