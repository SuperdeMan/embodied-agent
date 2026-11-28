import assert from "node:assert/strict";
import test from "node:test";
import { sendCheckedText } from "../../console/text-transport.js";

function session(handle, canProbe = true) {
  const socket = new EventTarget();
  socket.readyState = 1;
  socket.frames = [];
  socket.send = raw => {
    const frame = JSON.parse(raw);
    socket.frames.push(frame);
    handle(frame, socket);
  };
  return {socket, canProbe, ready: Promise.resolve(true)};
}

function respond(frame, socket) {
  if (frame.type === "ping") {
    socket.dispatchEvent(new MessageEvent("message", {data: JSON.stringify({type: "pong", request_id: frame.request_id})}));
  }
}

function setup(first, second = null) {
  let current = first;
  return {current: () => current, reconnect: () => (current = second), wanted: () => true, dispatch: () => {}};
}

test("only the new socket sends text after an idle socket drops a probe", async () => {
  const stale = session((_, s) => { s.readyState = 3; s.dispatchEvent(new Event("close")); });
  const next = session(respond);
  assert.equal(await sendCheckedText(setup(stale, next), "hello", "r1", 20), "sent");
  assert.deepEqual(stale.socket.frames.map(f => f.type), ["ping"]);
  assert.deepEqual(next.socket.frames, [{type: "ping", request_id: "r1"}, {type: "text", text: "hello", request_id: "r1"}]);
});

test("mismatched pongs and unavailable replacements leave all text unsent", async () => {
  const wrong = () => session((_, s) => respond({type: "ping", request_id: "another"}, s));
  const first = wrong(), next = wrong();
  assert.equal(await sendCheckedText(setup(first, next), "hello", "r2", 5), "unavailable");
  assert.deepEqual([...first.socket.frames, ...next.socket.frames].map(f => f.type), ["ping", "ping"]);
});

test("a text send error cannot trigger a second dispatch", async () => {
  const first = session((f, s) => { if (f.type === "text") throw Error("socket closed"); respond(f, s); });
  const next = session(respond);
  await assert.rejects(sendCheckedText(setup(first, next), "hello", "r3", 20), /socket closed/);
  assert.deepEqual(first.socket.frames.map(f => f.type), ["ping", "text"]);
  assert.deepEqual(next.socket.frames, []);
});

test("edits during a probe cancel the old input; old peers retain hri.v0 text", async () => {
  let wanted = true;
  const first = session((f, s) => { wanted = false; respond(f, s); });
  const link = setup(first);
  link.wanted = () => wanted;
  assert.equal(await sendCheckedText(link, "old", "r4", 20), "changed");
  assert.deepEqual(first.socket.frames.map(f => f.type), ["ping"]);
  const legacy = session(() => {}, false);
  assert.equal(await sendCheckedText(setup(legacy), "hello", "r5", 20), "sent");
  assert.deepEqual(legacy.socket.frames, [{type: "text", text: "hello"}]);
});
