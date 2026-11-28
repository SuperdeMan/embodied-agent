import assert from "node:assert/strict";
import test from "node:test";
import { startScenePreview } from "../../console/scene-preview.js";

function fixture() {
  const timers = new Map();
  let nextId = 0;
  const doc = new EventTarget();
  doc.hidden = false;
  doc.defaultView = {
    performance,
    setTimeout(fn, ms) { const id = ++nextId; timers.set(id, {fn, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const frames = [];
  const visible = [];
  function image() {
    return {id: "scene", alt: "scene", ownerDocument: doc, replaceWith(next) { visible.push(next); }};
  }
  doc.createElement = () => { const next = image(); frames.push(next); return next; };
  const notice = {hidden: false, textContent: "正在等待仿真画面…"};
  const stop = startScenePreview(image(), notice);
  function tick(ms) {
    const item = [...timers].find(([, v]) => v.ms === ms);
    assert.ok(item, "expected timer for " + ms);
    timers.delete(item[0]); item[1].fn();
  }
  return {doc, frames, visible, notice, tick, timers, stop};
}

test("initial unavailability and a later failed frame never replace a good image", () => {
  const f = fixture();
  f.frames[0].onerror();
  assert.equal(f.visible.length, 0);
  assert.equal(f.notice.hidden, false);
  f.tick(400); f.frames[1].onload();
  assert.equal(f.visible[0], f.frames[1]);
  assert.equal(f.notice.hidden, true);
  f.tick(200); f.frames[2].onerror();
  assert.deepEqual(f.visible, [f.frames[1]]);
  assert.match(f.notice.textContent, /上次收图/);
  assert.equal(f.notice.hidden, false);
  f.tick(400); f.frames[3].onload();
  assert.equal(f.visible[1], f.frames[3]);
  assert.equal(f.notice.hidden, true);
  f.stop();
  assert.equal(f.timers.size, 0);
});

test("a hidden page sends no new requests and a pending request is never duplicated", () => {
  const f = fixture();
  f.doc.hidden = true;
  f.doc.dispatchEvent(new Event("visibilitychange"));
  f.frames[0].onload();
  assert.equal(f.timers.size, 0);
  f.doc.hidden = false;
  f.doc.dispatchEvent(new Event("visibilitychange"));
  f.tick(0);
  f.doc.dispatchEvent(new Event("visibilitychange"));
  assert.equal(f.frames.length, 2);
  assert.equal([...f.timers.values()].some(v => v.ms === 0), false);
  f.stop();
  assert.equal(f.frames[1].onload, null);
});
