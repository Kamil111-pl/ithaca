/** Microphone capture → 16 kHz mono WAV, with automatic stop after a pause. */

export interface Recorder {
  stop: () => Promise<Blob>;
  cancel: () => void;
}

export async function startRecording(onAutoStop: () => void, onLevel?: (rms: number) => void): Promise<Recorder> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext({ sampleRate: 16000 });
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  let heardSpeech = false;
  let silentFor = 0;
  let floor = 0.004;
  const started = performance.now();
  let stopped = false;

  proc.onaudioprocess = (e) => {
    const data = new Float32Array(e.inputBuffer.getChannelData(0));
    chunks.push(data);
    let sum = 0;
    for (const v of data) sum += v * v;
    const rms = Math.sqrt(sum / data.length);
    onLevel?.(rms);
    const threshold = Math.max(floor * 3, 0.012);
    const secs = data.length / ctx.sampleRate;
    if (rms > threshold) {
      heardSpeech = true;
      silentFor = 0;
    } else {
      if (!heardSpeech) floor = 0.9 * floor + 0.1 * rms;
      silentFor += secs;
    }
    const elapsed = (performance.now() - started) / 1000;
    if (!stopped && ((heardSpeech && silentFor > 1.3) || elapsed > 30 || (!heardSpeech && elapsed > 8))) {
      stopped = true;
      onAutoStop();
    }
  };
  src.connect(proc);
  proc.connect(ctx.destination);

  const teardown = () => {
    proc.disconnect();
    src.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close();
  };

  return {
    cancel: teardown,
    stop: async () => {
      stopped = true;
      teardown();
      return encodeWav(chunks, ctx.sampleRate);
    },
  };
}

function encodeWav(chunks: Float32Array[], rate: number): Blob {
  const length = chunks.reduce((n, c) => n + c.length, 0);
  const buf = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buf);
  const str = (off: number, s: string) => [...s].forEach((ch, i) => view.setUint8(off + i, ch.charCodeAt(0)));
  str(0, "RIFF");
  view.setUint32(4, 36 + length * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, "data");
  view.setUint32(40, length * 2, true);
  let off = 44;
  for (const c of chunks) {
    for (const v of c) {
      const s = Math.max(-1, Math.min(1, v));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      off += 2;
    }
  }
  return new Blob([buf], { type: "audio/wav" });
}

export async function transcribe(wav: Blob): Promise<string> {
  const res = await fetch("/api/voice/transcribe", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "audio/wav", "X-Ithaca": "1" },
    body: wav,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()).text as string;
}

/** Read text aloud with the system's local voices. */
export function speak(markdown: string) {
  if (!("speechSynthesis" in window)) return;
  const text = markdown
    .replace(/```[\s\S]*?```/g, " (code omitted) ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^[#>\-*\s]+/gm, "")
    .replace(/[*_~|]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1200);
  if (!text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05;
  speechSynthesis.speak(u);
}
