/**
 * Global WebSocket manager — single persistent connection per browser tab.
 * Components subscribe via `useWsEvent` hook; reconnects automatically on drop.
 *
 * Messages from server: { kind: "run_update" | "target_update", data: {...} }
 */
import { useEffect, type DependencyList } from "react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Handler<T = any> = (data: T) => void;

class WsManager {
  private ws: WebSocket | null = null;
  private handlers: Map<string, Set<Handler>> = new Map();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  /** Open the connection. Called once from App on mount. */
  connect(): void {
    this.stopped = false;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws.onmessage = (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data as string) as { kind: string; data: unknown };
        this.handlers.get(msg.kind)?.forEach(h => h(msg.data));
      } catch { /* ignore malformed */ }
    };
    this.ws.onopen = () => {
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
      }
    };
    this.ws.onclose = () => {
      this.ws = null;
      if (!this.stopped) this._scheduleReconnect();
    };
    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  /** Close the connection and stop reconnecting. */
  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  /**
   * Subscribe to messages of a given `kind`.
   * Returns an unsubscribe function — pass it as useEffect cleanup.
   */
  on<T>(kind: string, handler: Handler<T>): () => void {
    if (!this.handlers.has(kind)) this.handlers.set(kind, new Set());
    this.handlers.get(kind)!.add(handler as Handler);
    return () => {
      this.handlers.get(kind)?.delete(handler as Handler);
    };
  }

  private _scheduleReconnect(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), 3000);
  }
}

/** Singleton — shared across the whole app. */
export const wsManager = new WsManager();

/**
 * React hook — subscribes to WS events of the given `kind`.
 * Re-registers whenever `deps` change (same semantics as useEffect).
 *
 * @example
 *   useWsEvent("run_update", (data: Run) => {
 *     if (data.id === runId) setRun(data);
 *   }, [runId]);
 */
export function useWsEvent<T = Record<string, unknown>>(
  kind: string,
  handler: (data: T) => void,
  deps: DependencyList = [],
): void {
  useEffect(() => {
    return wsManager.on<T>(kind, handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
