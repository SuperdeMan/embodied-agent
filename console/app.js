// hri.v0 WebSocket 控制台客户端，协议说明见服务端文档字符串。
// Console client for the hri.v0 WS contract (see src/embodied/hri/server.py docstring).
import { PcmPlayer, PcmRecorder } from "/console/pcm.js";
import { sendCheckedText } from "/console/text-transport.js";

const $ = (id) => document.getElementById(id);
const statusEl = $("status"), logEl = $("log"), tlEl = $("timeline");
const sceneEl = $("scene"), textEl = $("text"), talkBtn = $("talk");
const dlg = $("confirm-dlg");

let ws = null;
let player = new PcmPlayer();
let recorder = null;
let pendingConfirm = null;
let session = null;
let reconnectTimer = null;
let checkingText = false;
let nextRequest = 0;
let voiceSocket = null;
const outstanding = new Set();

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
  clearTimeout(reconnectTimer);
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/session`);
  ws = socket;
  let settle;
  const own = {socket, canProbe: false, ready: new Promise(resolve => { settle = resolve; })};
  session = own;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => { if (session === own) statusEl.textContent = "正在建立会话…"; };
  ws.onclose = () => {
    settle(false);
    if (session !== own) return;
    if (outstanding.size) logMsg("连接", `有 ${outstanding.size} 条已发送的指令尚无回执，执行结果待查。`, "err");
    outstanding.clear();
    pendingConfirm = null;
    if (dlg.open) dlg.close();
    statusEl.textContent = "连接断开，3s 后重连…";
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onmessage = (e) => {
    if (session !== own) return;
    if (e.data instanceof ArrayBuffer) { player.feed(e.data); return; }
    const m = JSON.parse(e.data);
    if (m.type === "reply" || m.type === "error") outstanding.delete(m.request_id);
    switch (m.type) {
      case "hello":
        own.canProbe = Array.isArray(m.capabilities) && m.capabilities.includes("text_probe");
        settle(true);
        statusEl.textContent = `已连接 · ${m.embodiment} · ${m.skills.length} skills`;
        if (m.runtime?.planner === "offline") statusEl.textContent += " · 仿真 / 离线规划";
        if (m.runtime?.audio === "mock") statusEl.textContent += " · 模拟音频（请用文本）";
        break;
      case "asr": logMsg("you (voice)", m.text, "user"); break;
      case "turn_start": break;
      case "step": addStep(m); break;
      case "reply":
        logMsg("agent", m.text, "agent");
        if (m.recording) {
          const r = m.recording;
          const status = { complete: "已保存", partial: "部分保存", unavailable: "未能保存", interrupted: "已中断" };
          logMsg("录制", [status[r.status] || r.status, r.episode_id, r.dropped ? `缺失 ${r.dropped} 条` : ""].filter(Boolean).join(" · "), "sys");
        }
        break;
      case "tts_start": player.begin(m.sample_rate); break;
      case "tts_end": break;
      case "confirm_request": showConfirm(m); break;
      case "error": logMsg("error", m.message, "err"); break;
    }
  };
  return own;
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

async function sendText() {
  const t = textEl.value.trim();
  if (!t || checkingText) return;
  const id = `text-${++nextRequest}`;
  checkingText = true;
  try {
    const outcome = await sendCheckedText({
      current: () => session,
      wanted: () => textEl.value.trim() === t,
      reconnect: old => {
        if (outstanding.size || pendingConfirm || recorder) return null;
        old?.socket.close();
        return connect();
      },
      dispatch: own => { if (own.canProbe) outstanding.add(id); },
    }, t, id);
    if (outcome === "sent") {
      logMsg("you", t, "user");
      if (textEl.value.trim() === t) textEl.value = "";
    } else if (outcome === "unavailable") {
      logMsg("连接", "未能确认连接，本条指令尚未发送。", "err");
    }
  } catch {
    outstanding.delete(id);
    logMsg("连接", "发送遇到错误，执行状态待查。", "err");
  } finally {
    checkingText = false;
  }
}
$("send").onclick = sendText;
textEl.addEventListener("keydown", (e) => { if (e.key === "Enter") sendText(); });

// 按住开始录制与传输，松开后结束输入。
// push-to-talk: hold down = record & stream; release = finalize
async function talkStart(e) {
  e.preventDefault();
  if (ws?.readyState !== 1 || recorder) return;
  try {
    const channel = ws;
    voiceSocket = channel;
    recorder = new PcmRecorder((buf) => { if (channel.readyState === 1 && channel === ws) channel.send(buf); });
    channel.send(JSON.stringify({ type: "audio_start" }));
    await recorder.start();
    talkBtn.classList.add("rec");
    talkBtn.textContent = "松开结束";
  } catch (err) {
    logMsg("error", `麦克风不可用: ${err.message}`, "err");
    recorder = null;
    voiceSocket = null;
  }
}
async function talkEnd() {
  if (!recorder) return;
  await recorder.stop();
  recorder = null;
  talkBtn.classList.remove("rec");
  talkBtn.textContent = "按住说话";
  const channel = voiceSocket;
  voiceSocket = null;
  if (channel?.readyState === 1 && channel === ws) channel.send(JSON.stringify({ type: "audio_end" }));
}
talkBtn.addEventListener("pointerdown", talkStart);
talkBtn.addEventListener("pointerup", talkEnd);
talkBtn.addEventListener("pointerleave", talkEnd);

  // 上一帧完成后再刷新，避免慢渲染时反复中止图片请求。
  // Refresh after the previous frame finishes; do not abort slow image requests repeatedly.
  function refreshScene() {
    const next = () => {
      sceneEl.onload = sceneEl.onerror = null;
      setTimeout(refreshScene, 200);
    };
    sceneEl.onload = sceneEl.onerror = next;
    sceneEl.src = `/api/scene.jpg?t=${Date.now()}`;
  }
  refreshScene();

connect();
