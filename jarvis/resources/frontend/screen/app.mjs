import { send, voiceCaptureTracks } from "/static/app.js";
import { audioCaptureTimestamp } from "../audio-clock.mjs";
import { ScreenCapture } from "./capture.mjs";
import { coachPanel } from "./coach.mjs";
const $ = (id) => document.getElementById(id);
const now = () => performance.timeOrigin + performance.now();
let capture,
  tracks,
  voiceSequence = 0,
  ready = false,
  silenceNode,
  syncTimer;
const bytes64 = (pcm) => {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 32768)
    binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(binary);
};
function voice(pcm, time) {
  if (!ready || capture?.active) return;
  send("screen.voice", {
    source_clock: capture.sourceClock,
    sequence_id: voiceSequence++,
    capture_ts_ms: time,
    data: bytes64(pcm),
  });
}
function state(update) {
  window.dispatchEvent(
    new CustomEvent("screen.capture.state", {
      detail: {
        ...update,
      },
    }),
  );
  if (update.error) $("viewStatus").textContent = update.error;
  else if ("active" in update) {
    $("viewStatus").textContent = update.active ? "正在观看" : "尚未共享画面";
    $("soundStatus").textContent = update.gameAudio
      ? "游戏声已共享"
      : "游戏声未共享";
    $("stopShare").disabled = !update.active;
    $("share").disabled = !ready || update.active;
    $("game").disabled = !!update.active;
    $("commentaryStyle").disabled = !update.active;
    if (!update.active) {
      $("commentaryStyle").value = "balanced";
      $("styleStatus").textContent = "";
    }
  }
}
window.addEventListener("duplex.input", (event) => {
  event.preventDefault();
  if (!ready) return;
  if (!capture.audio("user_mic", event.detail.pcm, event.detail.capture_ts_ms))
    voice(event.detail.pcm, event.detail.capture_ts_ms);
});
window.addEventListener("duplex.message", (event) => {
  capture?.receive(event.detail);
  if (event.detail.type === "event")
    coachPanel($("coachResult"), event.detail.event);
  if (
    event.detail.type === "event" &&
    event.detail.event.type === "screen.preference.updated"
  ) {
    $("styleStatus").textContent = "已应用到这次游戏";
  }
  if (event.detail.type === "screen.history_erased") capture?.forget();
});
window.addEventListener("duplex.ready", async (event) => {
  try {
    voiceSequence = 0;
    $("useMicrophone").disabled = true;
    tracks = voiceCaptureTracks();
    capture = new ScreenCapture({
      send,
      preview: $("preview"),
      onState: state,
    });
    const playbackStart = now();
    await capture.sync();
    ready = true;
    clearInterval(syncTimer);
    syncTimer = setInterval(
      () => capture.sync().catch((error) => state({ error: String(error) })),
      30000,
    );
    send("screen.playback_clock", {
      source_clock: capture.sourceClock,
      start_ts_ms: playbackStart,
    });
    if (!tracks.microphone) {
      await tracks.context.audioWorklet.addModule(
        "/static/screen/game-audio-worklet.js",
      );
      silenceNode = new AudioWorkletNode(
        tracks.context,
        "screen-game-analysis",
        {
          processorOptions: { emitSilence: true },
        },
      );
      silenceNode.connect(tracks.context.destination);
      silenceNode.port.onmessage = ({ data }) =>
        voice(
          data.pcm,
          audioCaptureTimestamp(tracks.context, data.input_context_end_s),
        );
    }
    $("share").disabled = false;
  } catch (error) {
    $("viewStatus").textContent = String(error);
  }
});
$("share").onclick = async () => {
  $("share").disabled = true;
  try {
    await capture.start({
      ...tracks,
      game: $("game").value,
    });
  } catch (error) {
    $("viewStatus").textContent = String(error);
    $("share").disabled = false;
  }
};
$("stopShare").onclick = () => capture?.stop();
$("commentaryStyle").onchange = () =>
  send("screen.style", { style: $("commentaryStyle").value });
window.addEventListener("duplex.closed", () => {
  coachPanel($("coachResult"), { type: "screen.capture.stopped" });
  ready = false;
  capture?.stop();
  silenceNode?.disconnect();
  silenceNode = null;
  tracks?.close();
  clearInterval(syncTimer);
  $("share").disabled = $("stopShare").disabled = true;
  $("useMicrophone").disabled = false;
  $("viewStatus").textContent = "已结束观看";
});
