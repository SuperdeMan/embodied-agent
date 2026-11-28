// 仅连接和只读探测可以重做；文本发送独立执行一次。
// Only connection setup and read-only probes are retryable; send each text frame once.
function readyWithin(session, timeoutMs) {
  return new Promise(resolve => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    session.ready.then(value => {
      clearTimeout(timer);
      resolve(value);
    });
  });
}

function probe(socket, requestId, timeoutMs) {
  return new Promise(resolve => {
    function finish(ok) {
      clearTimeout(timer);
      socket.removeEventListener("message", receive);
      socket.removeEventListener("close", unavailable);
      socket.removeEventListener("error", unavailable);
      resolve(ok);
    }
    function unavailable() { finish(false); }
    function receive(event) {
      if (typeof event.data !== "string") return;
      let frame;
      try { frame = JSON.parse(event.data); } catch { return; }
      if (frame?.type === "pong" && frame.request_id === requestId) finish(true);
    }
    const timer = setTimeout(unavailable, timeoutMs);
    socket.addEventListener("message", receive);
    socket.addEventListener("close", unavailable);
    socket.addEventListener("error", unavailable);
    try { socket.send(JSON.stringify({type: "ping", request_id: requestId})); }
    catch { unavailable(); }
  });
}

export async function sendCheckedText({current, reconnect, wanted, dispatch}, text, requestId, timeoutMs = 4000) {
  let session = current() || reconnect(null);
  let verified = false;
  for (let attempt = 0; session && attempt < 2; attempt++) {
    if (!wanted()) return "changed";
    if (await readyWithin(session, timeoutMs) && session === current() && session.socket.readyState === 1) {
      verified = !session.canProbe || await probe(session.socket, requestId, timeoutMs);
      if (verified && session === current() && session.socket.readyState === 1) break;
      verified = false;
    }
    if (attempt === 0) session = current() === session ? reconnect(session) : current();
  }
  if (!verified) return "unavailable";
  if (!wanted()) return "changed";
  dispatch(session);
  const frame = session.canProbe ? {type: "text", text, request_id: requestId} : {type: "text", text};
  session.socket.send(JSON.stringify(frame));
  return "sent";
}
