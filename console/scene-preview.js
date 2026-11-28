// 成功加载才替换可见画面，一次只取一帧。 / Replace the displayed image only after one frame finishes loading.
export function startScenePreview(image, notice) {
  const doc = image.ownerDocument;
  const view = doc.defaultView;
  let request = null;
  let nextTimer;
  let staleTimer;
  let lastFrameAt = 0;
  let lastReceived = 0;
  let delay = 200;
  let stopped = false;

  function showDelay() {
    const message = lastFrameAt
      ? `画面暂未更新 · 上次收图 ${new Date(lastFrameAt).toLocaleTimeString("zh-CN", {hour12: false})}`
      : "正在等待仿真画面…";
    if (notice.textContent !== message) notice.textContent = message;
    notice.hidden = false;
  }

  function schedule(wait) {
    view.clearTimeout(nextTimer);
    if (!stopped && !doc.hidden && !request) nextTimer = view.setTimeout(load, wait);
  }

  function load() {
    if (stopped || doc.hidden || request) return;
    const frame = doc.createElement("img");
    request = frame;
    function finish(loaded) {
      if (request !== frame) return;
      frame.onload = frame.onerror = null;
      request = null;
      view.clearTimeout(staleTimer);
      if (loaded) {
        frame.id = image.id;
        frame.alt = image.alt;
        image.replaceWith(frame);
        image = frame;
        lastFrameAt = Date.now();
        lastReceived = view.performance.now();
        notice.hidden = true;
        delay = 200;
      } else {
        showDelay();
        delay = Math.min(delay * 2, 2400);
      }
      schedule(delay);
    }
    frame.onload = () => finish(true);
    frame.onerror = () => finish(false);
    if (lastFrameAt) staleTimer = view.setTimeout(showDelay, Math.max(0, 1500 - (view.performance.now() - lastReceived)));
    frame.src = `/api/scene.jpg?t=${Date.now()}`;
  }

  function visibilityChanged() {
    if (!doc.hidden && lastFrameAt && view.performance.now() - lastReceived >= 1500) showDelay();
    schedule(0);
  }

  function stop() {
    stopped = true;
    view.clearTimeout(nextTimer);
    view.clearTimeout(staleTimer);
    if (request) request.onload = request.onerror = null;
    request = null;
    doc.removeEventListener("visibilitychange", visibilityChanged);
  }

  doc.addEventListener("visibilitychange", visibilityChanged);
  load();
  return stop;
}
