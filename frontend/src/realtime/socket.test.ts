/**
 * The socket's protocol obligations, against a fake WebSocket.
 *
 * These are the rules the *server* enforces by closing the connection, so getting them wrong does
 * not throw — it produces a UI that quietly stops updating, which is the failure mode a test has to
 * catch because a person will not.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CLOSE_SESSION_REVOKED, CLOSE_UNAUTHENTICATED, StreamClient } from "./socket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly OPEN = 1;

  readyState = FakeWebSocket.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(payload: string) {
    this.sent.push(payload);
  }

  close() {
    this.onclose?.({ code: 1000 });
  }

  /** Drive the handshake the way the server would. */
  open() {
    this.onopen?.();
  }

  deliver(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  serverClose(code: number) {
    this.onclose?.({ code });
  }

  frames(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
  }
}

const authOk = (expiresInMs = 15 * 60_000) => ({
  type: "auth.ok",
  user_id: 1,
  username: "alice",
  expires_at: new Date(Date.now() + expiresInMs).toISOString(),
});

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function connect(token: string | null = "access-1") {
  const onEvent = vi.fn();
  const onSessionLost = vi.fn();
  const client = new StreamClient({ getToken: () => token, onEvent, onSessionLost });
  client.connect();
  return { client, onEvent, onSessionLost, socket: () => FakeWebSocket.instances.at(-1)! };
}

describe("StreamClient", () => {
  it("sends the auth frame first, before anything else", () => {
    // The server accepts anonymously and starts a 5s deadline; any other opening frame is a close.
    const { socket } = connect();
    socket().open();

    expect(socket().frames()[0]).toEqual({ type: "auth", token: "access-1" });
  });

  it("re-authenticates before the access token expires", () => {
    // Without this the server closes the socket at expiry and the UI silently stops updating every
    // fifteen minutes.
    const { socket } = connect();
    socket().open();
    socket().deliver(authOk(60_000));

    vi.advanceTimersByTime(31_000);

    const auths = socket().frames().filter((frame) => frame["type"] === "auth");
    expect(auths).toHaveLength(2);
  });

  it("re-subscribes after a reconnect, because the server remembers nothing", () => {
    const { client, socket } = connect();
    socket().open();
    socket().deliver(authOk());
    client.subscribe(["AAPL"]);

    socket().serverClose(1006);
    vi.advanceTimersByTime(60_000);

    const reconnected = socket();
    reconnected.open();
    reconnected.deliver(authOk());

    expect(reconnected.frames()).toContainEqual({ type: "subscribe", symbols: ["AAPL"] });
  });

  it("does not reconnect after 4403 — the session is gone, not the connection", () => {
    const { onSessionLost, socket } = connect();
    socket().open();
    socket().deliver(authOk());

    socket().serverClose(CLOSE_SESSION_REVOKED);
    vi.advanceTimersByTime(120_000);

    expect(onSessionLost).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("does reconnect after an unauthenticated close, which may just be an expired token", () => {
    const { socket } = connect();
    socket().open();
    socket().serverClose(CLOSE_UNAUTHENTICATED);

    vi.advanceTimersByTime(60_000);

    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it("keeps subscriptions under the server's ceiling rather than being closed for exceeding it", () => {
    const { client, socket } = connect();
    socket().open();
    socket().deliver(authOk());

    client.subscribe(Array.from({ length: 40 }, (_, i) => `SYM${i}`));

    const subscribed = socket()
      .frames()
      .filter((frame) => frame["type"] === "subscribe")
      .flatMap((frame) => frame["symbols"] as string[]);
    expect(subscribed).toHaveLength(25);
  });

  it("does not connect without a token", () => {
    connect(null);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
