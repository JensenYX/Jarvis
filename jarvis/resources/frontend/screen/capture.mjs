import { audioCaptureTimestamp } from '../audio-clock.mjs';
function encode(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 32768)
    binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
  return btoa(binary);
}
const now = () => performance.timeOrigin + performance.now();
const id = () => crypto.randomUUID().replaceAll('-', '');
const audioModules = new WeakMap();
export class ScreenCapture {
  constructor({
    send,
    onState = () => {},
    onSegment = async () => {},
    inferenceFps = 1,
    ringSeconds = 60,
    ringBytes = 64000000,
    recordingFps = 30,
    preview = null,
  }) {
    this.send = send;
    this.onState = onState;
    this.onSegment = onSegment;
    this.fps = inferenceFps;
    this.ringSeconds = ringSeconds;
    this.recordingFps = recordingFps;
    this.sourceClock = id();
    this.gameSession = id();
    this.sceneEpoch = 0;
    this.sequences = {};
    this.ring = [];
    this.active = false;
    this.closed = false;
    this.recorders = new Set();
    this.syncWaiters = [];
    this.recording = false;
    this.encodedFrames = 0;
    this.observedFrames = 0;
    this.skippedFrames = 0;
    this.preview = preview;
    this.emptySegments = [];
    this.maxRingBytes = ringBytes;
    this.emptySegmentCount = 0;
    this.privacyEpoch = 0;
  }
  receive(message) {
    if (message.type === 'screen.clock.pong') {
      this.send('screen.clock.reply', {
        nonce: message.payload.nonce,
        client_receive_ms: now(),
      });
      this.syncWaiters.splice(0).forEach((waiter) => waiter.resolve());
    }
  }
  async sync() {
    await new Promise((resolve, reject) => {
      const waiter = {
        resolve: () => {
          clearTimeout(timer);
          resolve();
        },
      };
      const timer = setTimeout(() => {
        this.syncWaiters = this.syncWaiters.filter((item) => item !== waiter);
        reject(new Error('时间同步失败\uFF0C请重连'));
      }, 10000);
      this.syncWaiters.push(waiter);
      this.send('screen.clock.ping', {
        source_clock: this.sourceClock,
        client_send_ms: now(),
      });
    });
  }
  metadata(source, captureTs) {
    return {
      game_session_id: this.gameSession,
      scene_epoch: this.sceneEpoch,
      source_clock: this.sourceClock,
      sequence_id: (this.sequences[source] = (this.sequences[source] ?? -1) + 1),
      capture_ts_ms: captureTs,
    };
  }
  async start({
    game = 'cs2',
    context,
    microphone,
    assistant,
    includeSelf = false,
    includeAudio = true,
  }) {
    this.onState({
      active: false,
      stage: 'audio_worklet_prepare',
      contextState: context.state,
    });
    if (!audioModules.has(context))
      audioModules.set(
        context,
        context.audioWorklet.addModule('/static/screen/game-audio-worklet.js'),
      );
    await audioModules.get(context);
    this.onState({
      active: false,
      stage: 'clock_sync',
    });
    await this.sync();
    this.onState({
      active: false,
      stage: 'display_request',
    });
    this.display = await navigator.mediaDevices.getDisplayMedia({
      video: {
        frameRate: { ideal: this.recordingFps },
        ...(includeSelf ? { displaySurface: 'browser' } : {}),
      },
      audio: includeAudio
        ? {
            channelCount: { ideal: 2 },
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
          }
        : false,
      systemAudio: 'include',
      selfBrowserSurface: includeSelf ? 'include' : 'exclude',
      preferCurrentTab: includeSelf,
    });
    this.onState({
      active: false,
      stage: 'display_granted',
      tracks: this.display.getTracks().map((t) => ({
        kind: t.kind,
        settings: t.getSettings(),
      })),
    });
    this.context = context;
    this.microphone = microphone;
    this.assistant = assistant;
    this.video = this.preview || document.createElement('video');
    this.video.muted = true;
    this.video.playsInline = true;
    this.video.srcObject = new MediaStream(this.display.getVideoTracks());
    this.video.play().catch((error) => {
      this.onState({ error: String(error) });
      this.stop();
    });
    this.active = true;
    this.game = game;
    const binding = {
      game_session_id: this.gameSession,
      scene_epoch: this.sceneEpoch,
    };
    const current = () =>
      this.active &&
      this.gameSession === binding.game_session_id &&
      this.sceneEpoch === binding.scene_epoch;
    const videoTrack = this.display.getVideoTracks()[0],
      gameTrack = this.display.getAudioTracks()[0];
    this.send('screen.begin', {
      game_session_id: this.gameSession,
      scene_epoch: this.sceneEpoch,
      source_clock: this.sourceClock,
      game,
      tracks: {
        screen: videoTrack.getSettings(),
        game_audio: gameTrack?.getSettings() || null,
        microphone: microphone?.getAudioTracks()[0]?.getSettings() || null,
      },
    });
    videoTrack.addEventListener('ended', () => {
      if (current()) this.stop();
    });
    for (const track of this.display.getTracks())
      for (const event of ['mute', 'unmute', 'ended'])
        track.addEventListener(event, () => {
          if (!current()) return;
          this.send('screen.track_state', {
            ...binding,
            source: track.kind === 'video' ? 'screen' : 'game_audio',
            state: event,
            capture_ts_ms: now(),
          });
          this.status();
        });
    if (!microphone?.getAudioTracks().length) {
      this.clockNode = new AudioWorkletNode(context, 'screen-game-analysis', {
        processorOptions: { emitSilence: true },
      });
      this.clockNode.connect(context.destination);
      this.clockNode.port.onmessage = ({ data }) => {
        if (current())
          this.audio(
            'clock_silence',
            data.pcm,
            audioCaptureTimestamp(context, data.input_context_end_s),
          );
      };
    }
    if (gameTrack) {
      this.onState({
        active: true,
        stage: 'game_worklet',
        contextState: context.state,
      });
      this.onState({
        active: true,
        stage: 'game_worklet_ready',
        contextState: context.state,
      });
      this.gameSource = context.createMediaStreamSource(new MediaStream([gameTrack]));
      this.gameNode = new AudioWorkletNode(context, 'screen-game-analysis');
      this.gameSource.connect(this.gameNode);
      this.gameNode.connect(context.destination);
      this.gameNode.port.onmessage = ({ data }) => {
        if (!current()) return;
        const time = audioCaptureTimestamp(context, data.input_context_end_s);
        this.audio('game_audio', data.pcm, time);
      };
    }
    this.canvas = document.createElement('canvas');
    this.nextFrame = 0;
    this.frameLoop();
    this.status();
    return this.display;
  }
  audio(source, pcm, captureTs) {
    if (!this.active) return false;
    this.send(`screen.${source}`, {
      ...this.metadata(source, captureTs),
      data: encode(new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength)),
      mime_type: 'audio/pcm-f32le',
    });
    return true;
  }
  frameLoop() {
    if (!this.active) return;
    const binding = this.sceneEpoch;
    const schedule = () => {
      if (this.active && binding === this.sceneEpoch)
        this.frameCallback = this.video.requestVideoFrameCallback((_, metadata) => {
          this.observedFrames++;
          const time = now();
          if (time >= this.nextFrame) {
            this.nextFrame = time + 1000 / this.fps;
            this.encodeFrame(time, metadata);
          }
          schedule();
        });
    };
    schedule();
  }
  async encodeFrame(time, metadata) {
    if (this.encoding) {
      this.skippedFrames++;
      return;
    }
    this.encoding = true;
    const binding = { ...this.metadata('screen', time) };
    try {
      const ratio = Math.min(1, 1280 / this.video.videoWidth, 720 / this.video.videoHeight);
      this.canvas.width = Math.max(1, Math.round(this.video.videoWidth * ratio));
      this.canvas.height = Math.max(1, Math.round(this.video.videoHeight * ratio));
      this.canvas
        .getContext('2d')
        .drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);
      const blob = await new Promise((resolve) => this.canvas.toBlob(resolve, 'image/jpeg', 0.82));
      if (!this.active || binding.scene_epoch !== this.sceneEpoch || !blob) return;
      this.send('screen.frame', {
        ...binding,
        data: encode(new Uint8Array(await blob.arrayBuffer())),
        mime_type: 'image/jpeg',
        width: this.canvas.width,
        height: this.canvas.height,
        media_time_s: metadata.mediaTime,
      });
      this.encodedFrames++;
    } finally {
      this.encoding = false;
    }
  }
  status() {
    this.onState({
      active: this.active,
      gameAudio: !!this.display?.getAudioTracks().some((t) => t.readyState === 'live' && !t.muted),
      ringSegments: this.ring.length,
      ringBytes: this.ring.reduce((sum, s) => sum + s.blob.size, 0),
      emptySegments: this.emptySegments.length,
      observedFrames: this.observedFrames,
      inferenceFrames: this.encodedFrames,
      skippedFrames: this.skippedFrames,
    });
  }
  forget() {
    this.privacyEpoch++;
    this.recording = false;
    this.stop();
    this.ring = [];
    this.emptySegments = [];
    this.status();
  }
  async stop() {
    if (!this.active) return;
    this.active = false;
    clearTimeout(this.rotateTimer);
    if (this.frameCallback !== undefined) this.video.cancelVideoFrameCallback(this.frameCallback);
    this.send('screen.ended', {
      game_session_id: this.gameSession,
      scene_epoch: this.sceneEpoch,
    });
    this.gameSource?.disconnect();
    this.gameNode?.disconnect();
    this.clockNode?.disconnect();
    this.display?.getTracks().forEach((t) => t.stop());
    this.video.pause();
    this.video.srcObject = null;
    this.sceneEpoch++;
    this.status();
  }
}
