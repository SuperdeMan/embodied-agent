// PCM capture/playback over Web Audio, design carried from car-agent hmi/src/pcmRecorder.mjs
// + pcmPlayer.mjs (16k mono s16le up, streaming playback down). Rewritten lean for the v0
// console: AudioWorklet-free capture via ScriptProcessor fallback is deliberately avoided —
// worklet only, modern browsers assumed.

const CAPTURE_RATE = 16000;

const WORKLET_SRC = `
class CaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
`;

export class PcmRecorder {
  constructor(onChunk) {
    this.onChunk = onChunk;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this._resampleCarry = [];
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    this.ctx = new AudioContext();
    const blob = new Blob([WORKLET_SRC], { type: "application/javascript" });
    await this.ctx.audioWorklet.addModule(URL.createObjectURL(blob));
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "capture-processor");
    this.node.port.onmessage = (e) => this._push(e.data, this.ctx.sampleRate);
    src.connect(this.node);
  }

  _push(f32, srcRate) {
    // naive linear resample srcRate -> 16k, fine for command speech
    const ratio = srcRate / CAPTURE_RATE;
    const out = new Int16Array(Math.floor(f32.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const v = f32[Math.floor(i * ratio)];
      out[i] = Math.max(-1, Math.min(1, v)) * 0x7fff;
    }
    if (out.length) this.onChunk(out.buffer);
  }

  async stop() {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    await this.ctx?.close();
    this.ctx = this.stream = this.node = null;
  }
}

export class PcmPlayer {
  constructor() {
    this.ctx = null;
    this.rate = 24000;
    this.playhead = 0;
  }

  begin(sampleRate) {
    this.rate = sampleRate || 24000;
    if (!this.ctx) this.ctx = new AudioContext();
    this.playhead = Math.max(this.ctx.currentTime + 0.05, this.playhead);
  }

  feed(arrayBuffer) {
    if (!this.ctx || arrayBuffer.byteLength < 2) return;
    const i16 = new Int16Array(arrayBuffer);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
    const buf = this.ctx.createBuffer(1, f32.length, this.rate);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const at = Math.max(this.playhead, this.ctx.currentTime + 0.02);
    src.start(at);
    this.playhead = at + buf.duration;
  }

  stop() {
    // barge-in support arrives with VAD; v0 lets the tail play out
    this.playhead = 0;
  }
}
