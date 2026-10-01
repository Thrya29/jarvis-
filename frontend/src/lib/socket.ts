// WebSocket to the daemon: authenticates in the first frame, reconnects with backoff.

import { TOKEN } from "./api";
import type { JsonEvent } from "./types";

type Listener = (event: JsonEvent) => void;
type StateListener = (connected: boolean, unauthorised: boolean) => void;

export class JarvisSocket {
  private ws: WebSocket | null = null;
  private retry = 0;
  private timer = 0;
  private closed = false;

  constructor(
    private onEvent: Listener,
    private onState: StateListener,
  ) {}

  connect(): void {
    if (this.closed) return;
    const ws = new WebSocket(`ws://${location.host}/v1/ws`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: TOKEN }));
    ws.onmessage = (e) => {
      let msg: JsonEvent;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      if (msg.type === "ready") {
        this.retry = 0;
        this.onState(true, false);
      }
      this.onEvent(msg);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.onState(false, e.code === 4401);
      if (e.code === 4401 || this.closed) return;
      this.timer = window.setTimeout(() => this.connect(), Math.min(10000, 500 * 2 ** this.retry++));
    };
  }

  send(msg: JsonEvent): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  close(): void {
    this.closed = true;
    window.clearTimeout(this.timer);
    this.ws?.close();
  }
}
