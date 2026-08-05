// Console client for the hri.v0 WS contract (see src/embodied/hri/server.py docstring).
import { PcmPlayer, PcmRecorder } from "/console/pcm.js";

const $ = (id) => document.getElementById(id);
const statusEl = $("status"), logEl = $("log"), tlEl = $("timeline");
const sceneEl = $("scene"), textEl = $("text"), talkBtn = $("talk");
const dlg = $("confirm-dlg");

let ws = null;
let player = new PcmPlayer();
let recorder = null;
let pendingConfirm = null;

function logMsg(who, text, cls) {
  const div = document.createElement("div");
  div.className = `msg ${cls || who}`;
  div.innerHTML = `<div class="who">${who}</div><div class="body"></div>`;
  div.querySelector(".body").textContent = text;
  logEl.appendChild(div);
  logEl.scrollTop = logEl.scrollHeight;
}

function addStep(s) {
  const div = document.createElement("div");
  div.className = `step ${s.status}`;
  div.innerHTML = `<span class="dot"></span><span></span>`;
  div.lastElementChild.textContent = `${s.step_id} ${s.skill} — ${s.status}${s.detail ? "：" + s.detail : ""}`;
  tlEl.appendChild(div);
  tlEl.scrollTop = tlEl.scrollHeight;
}

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws/session`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => (statusEl.textContent = "已连接");
  ws.onclose = () => { statusEl.textContent = "连接断开，3s 后重连…"; setTimeout(connect, 3000); };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) { player.feed(e.data); return; }
    const m = JSON.parse(e.data);
    switch (m.type) {
      case "hello":
        statusEl.textContent = `已连接 · ${m.embodiment} · ${m.skills.length} skills`;
        break;
      case "asr": logMsg("you (voice)", m.text, "user"); break;
      case "turn_start": break;
      case "step": addStep(m); break;
      case "reply": logMsg("agent", m.text, "agent"); break;
      case "tts_start": player.begin(m.sample_rate); break;
      case "tts_end": break;
      case "confirm_request": showConfirm(m); break;
      case "error": logMsg("error", m.message, "err"); break;
    }
  };
}

function showConfirm(m) {
  pendingConfirm = m.rid;
  $("confirm-text").textContent =
    `危险技能 ${m.skill} 请求执行${Object.keys(m.params || {}).length ? "，参数 " + JSON.stringify(m.params) : ""}。确认？`;
  dlg.showModal();
}
$("approve").onclick = () => { answerConfirm(true); };
$("deny").onclick = () => { answerConfirm(false); };
dlg.addEventListener("cancel", () => answerConfirm(false));
function answerConfirm(approved) {
  if (pendingConfirm && ws?.readyState === 1) {
    ws.send(JSON.stringify({ type: "confirm_reply", rid: pendingConfirm, approved }));
  }
  pendingConfirm = null;
  if (dlg.open) dlg.close();
}

function sendText() {
  const t = textEl.value.trim();
  if (!t || ws?.readyState !== 1) return;
  logMsg("you", t, "user");
  ws.send(JSON.stringify({ type: "text", text: t }));
  textEl.value = "";
}
$("send").onclick = sendText;
textEl.addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });

// push-to-talk: hold down = record & stream; release = finalize
async function talkStart(e) {
  e.preventDefault();
  if (ws?.readyState !== 1 || recorder) return;
  try {
    recorder = new PcmRecorder((buf) => { if (ws?.readyState === 1) ws.send(buf); });
    ws.send(JSON.stringify({ type: "audio_start" }));
    await recorder.start();
    talkBtn.classList.add("rec");
    talkBtn.textContent = "松开结束";
  } catch (err) {
    logMsg("error", `麦克风不可用: ${err.message}`, "err");
    recorder = null;
  }
}
async function talkEnd() {
  if (!recorder) return;
  await recorder.stop();
  recorder = null;
  talkBtn.classList.remove("rec");
  talkBtn.textContent = "按住说话";
  if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "audio_end" }));
}
talkBtn.addEventListener("pointerdown", talkStart);
talkBtn.addEventListener("pointerup", talkEnd);
talkBtn.addEventListener("pointerleave", talkEnd);

// scene refresh ~5 fps
setInterval(() => { sceneEl.src = `/api/scene.jpg?t=${Date.now()}`; }, 200);

connect();
