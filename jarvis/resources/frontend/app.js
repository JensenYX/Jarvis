import { audioCaptureTimestamp, audioPlaybackTimestamp } from './audio-clock.mjs';
const $ = (id) => document.getElementById(id);
let socket,
  context,
  stream,
  node,
  epoch = 0,
  connected = false,
  ending = false,
  flushed;
let pendingStops = 0;
const userId = localStorage.getItem('duplex-user-id') || crypto.randomUUID();
let spokenLanguage = 'zh';
function ui(zh, en) {
  return $('language').value === 'en' || ($('language').value === 'auto' && spokenLanguage === 'en')
    ? en
    : zh;
}
const resultCards = new Map();
const taskData = new Map();
export function renderResult(result, transmit = send) {
  const key = `${result.task_id}:${result.version}`;
  result = { ...taskData.get(key), ...result };
  taskData.set(key, result);
  let card = resultCards.get(key);
  if (!card) {
    card = document.createElement('article');
    card.style.cssText =
      'background:white;border-radius:16px;padding:20px;margin:16px 0;overflow-wrap:anywhere';
    resultCards.set(key, card);
    $('results').prepend(card);
  }
  card.replaceChildren();
  const heading = document.createElement('strong');
  const pending = ['QUEUED', 'RUNNING'].includes(result.status);
  heading.textContent =
    (result.objective || ui('任务', 'Task')) +
    ' · ' +
    (pending
      ? result.execution_mode === 'fast'
        ? ui('正在回答', 'Answering')
        : ui('处理中', 'In progress')
      : ['failed', 'FAILED'].includes(result.status)
        ? ui('未完成', 'Incomplete')
        : ui('已完成', 'Complete'));
  const answer = document.createElement('p');
  answer.textContent =
    result.spoken_answer || ui('完成后会告诉你结果。', 'I will tell you when the result is ready.');
  card.append(heading, answer);
  if (!pending) {
    const speak = document.createElement('button');
    speak.textContent = ui('播放结果', 'Read result');
    speak.onclick = () =>
      transmit('present_result', { task_id: result.task_id, version: result.version });
    card.append(speak);
  }
  if (result.details) {
    const details = document.createElement('details');
    const title = document.createElement('summary');
    title.textContent = ui('查看详情', 'Details');
    const body = document.createElement('pre');
    body.textContent = result.details;
    details.append(title, body);
    card.append(details);
  }
  const urls = new Set();
  for (const evidence of result.evidence || []) {
    const observation = evidence.observation || evidence;
    const sources =
      observation.results ||
      (observation.source_url
        ? [{ url: observation.source_url, title: observation.source || 'Source' }]
        : []);
    for (const source of sources.slice(0, 5)) {
      let url;
      try {
        url = new URL(source.url);
      } catch {
        continue;
      }
      if (!['https:', 'http:'].includes(url.protocol) || urls.has(url.href)) continue;
      urls.add(url.href);
      const a = document.createElement('a');
      a.href = url.href;
      a.textContent = source.title || url.hostname;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.style.cssText = 'display:block;margin-top:8px';
      card.append(a);
    }
  }
  for (const artifact of result.artifacts || []) {
    if (!/^\/artifacts\/[a-f0-9]{32}\/[A-Za-z0-9_.-]+$/.test(artifact.url)) continue;
    const row = document.createElement('p');
    const a = document.createElement('a');
    a.href = artifact.url;
    a.download = artifact.filename;
    a.textContent = `${ui('下载', 'Download')} ${artifact.filename}`;
    row.append(a);
    card.append(row);
    if (artifact.previewable) {
      const preview = document.createElement('button');
      preview.textContent = ui('打开游戏 / 预览', 'Play / Preview');
      preview.onclick = () => {
        const frame = document.createElement('iframe');
        frame.setAttribute('sandbox', 'allow-scripts');
        frame.title = artifact.title;
        frame.src = artifact.url + '?preview=true';
        frame.style.cssText =
          'display:block;width:100%;height:540px;border:1px solid #c7d1cb;border-radius:12px;margin-top:12px';
        card.append(frame);
        preview.remove();
      };
      card.append(preview);
    }
  }
}
localStorage.setItem('duplex-user-id', userId);
function log(value) {
  $('debug').textContent = (JSON.stringify(value) + '\n' + $('debug').textContent).slice(0, 16000);
}
export function send(type, payload) {
  if (socket?.readyState === 1) socket.send(JSON.stringify({ type, payload }));
}
export function clearGameAudio() {
  node?.port.postMessage({ type: 'clear', epoch });
}
export function voiceCaptureTracks() {
  if (!connected || !context || !node) throw new Error('请先连接对话');
  const assistant = context.createMediaStreamDestination();
  node.connect(assistant);
  return {
    context,
    microphone: stream,
    assistant: assistant.stream,
    close() {
      try {
        node.disconnect(assistant);
      } catch {}
      assistant.stream.getTracks().forEach((t) => t.stop());
    },
  };
}
export function recordingAudio(extraStream = null) {
  if (!connected || !context || !node || !stream) throw new Error('请先连接麦克风');
  const destination = context.createMediaStreamDestination();
  const microphone = context.createMediaStreamSource(stream);
  const gain = context.createGain();
  gain.gain.value = 0.5;
  microphone.connect(gain);
  node.connect(gain);
  gain.connect(destination);
  const extra = extraStream ? context.createMediaStreamSource(extraStream) : null;
  extra?.connect(gain);
  return {
    stream: destination.stream,
    close() {
      microphone.disconnect();
      try {
        node?.disconnect(gain);
      } catch {}
      extra?.disconnect();
      gain.disconnect();
      destination.stream.getTracks().forEach((t) => t.stop());
    },
  };
}
function decodePCM(encoded) {
  const bytes = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
  return new Float32Array(bytes.buffer);
}
async function end() {
  if (ending) return;
  ending = true;
  connected = false;
  if (node && context?.state !== 'closed') {
    await new Promise((resolve) => {
      flushed = resolve;
      node.port.postMessage({ type: 'clear', epoch: ++epoch, flush: true });
      setTimeout(resolve, 200);
    });
    flushed = undefined;
  }
  send('end', {});
  stream?.getTracks().forEach((track) => track.stop());
  node?.disconnect();
  if (context && context.state !== 'closed') await context.close();
  if (socket) socket.onclose = null;
  socket?.close();
  socket = context = stream = node = undefined;
  $('connect').disabled = false;
  $('language').disabled = false;
  $('disconnect').disabled = $('stop').disabled = true;
  $('status').textContent = '对话已结束';
  window.dispatchEvent(new CustomEvent('duplex.closed'));
  ending = false;
}
$('connect').onclick = async () => {
  $('connect').disabled = true;
  $('language').disabled = true;
  try {
    const useMic = !$('useMicrophone') || $('useMicrophone').checked;
    stream = useMic
      ? await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          video: false,
        })
      : null;
    context = new AudioContext({ sampleRate: 24000 });
    await context.resume();
    await context.audioWorklet.addModule('/static/audio-worklet.js');
    node = new AudioWorkletNode(context, 'duplex-audio');
    if (stream) context.createMediaStreamSource(stream).connect(node);
    node.connect(context.destination);
    socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`,
    );
    socket.onopen = () => {
      socket.send(
        JSON.stringify({
          type: 'start',
          user_id: userId,
          language: $('language').value,
          microphone: !!stream,
        }),
      );
      $('status').textContent = ui('正在连接', 'Connecting');
    };
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (
        !window.dispatchEvent(
          new CustomEvent('duplex.message', { detail: message, cancelable: true }),
        )
      )
        return;
      if (message.type === 'session') {
        connected = true;
        epoch = message.generation_epoch;
        pendingStops = 0;
        node.port.postMessage({ type: 'reset', epoch });
        $('status').textContent = ui('正在听', 'Listening');
        $('disconnect').disabled = $('stop').disabled = false;
        window.dispatchEvent(new CustomEvent('duplex.ready', { detail: message }));
      } else if (message.type === 'audio') {
        if (pendingStops || message.generation_epoch < epoch) return;
        const pcm = decodePCM(message.pcm);
        const expires =
          message.expires_at_client_ms === undefined
            ? undefined
            : context.currentTime +
              (message.expires_at_client_ms - (performance.timeOrigin + performance.now())) / 1000;
        node.port.postMessage({ type: 'audio', ...message, pcm, expires_at_context_s: expires }, [
          pcm.buffer,
        ]);
      } else if (message.type === 'playback.clear') {
        if (message.reason === 'stop_button') pendingStops = Math.max(0, pendingStops - 1);
        epoch = Math.max(epoch, message.generation_epoch);
        node.port.postMessage({ type: 'clear', epoch });
      } else if (message.type === 'playback.pause' || message.type === 'playback.resume') {
        if (pendingStops && message.type === 'playback.resume') return;
        node.port.postMessage({
          type: message.type.split('.')[1],
          epoch: message.generation_epoch,
        });
      } else if (message.type === 'event') {
        const ev = message.event;
        if (ev.type === 'asr.partial' || ev.type === 'asr.final') {
          $('transcript').textContent = ev.payload.text;
          spokenLanguage = ['English', 'en'].includes(ev.payload.language)
            ? 'en'
            : /[\u3400-\u9fff]/.test(ev.payload.text)
              ? 'zh'
              : 'en';
        }
        if (ev.type === 'task.started')
          $('status').textContent = ui(
            '后台处理中 · 可以继续说',
            'Working in background · Keep talking',
          );
        if (
          [
            'task.queued',
            'task.started',
            'task.fast_started',
            'task.completed',
            'task.failed',
            'task.restored',
          ].includes(ev.type)
        )
          renderResult(ev.payload);
        if (ev.type === 'task.delivered')
          $('status').textContent = [...taskData.values()].some((t) =>
            ['QUEUED', 'RUNNING'].includes(t.status),
          )
            ? ui('后台处理中 · 可以继续说', 'Working in background · Keep talking')
            : ui('正在听', 'Listening');
        if (
          ev.type === 'task.delivery_held' ||
          (ev.type === 'task.delivery_pending' && !ev.payload.automatic_retry)
        )
          $('status').textContent = [
            'user_interrupted',
            'user_dismissed',
            'user_stopped_announcements',
          ].includes(ev.payload.reason)
            ? ui(
                '已停止播报，结果保留在卡片中，可点击播放。',
                'Announcements stopped. Results remain in their cards; use Read result to hear them.',
              )
            : ui(
                '有结果尚未播完，可说“逐项告诉我结果”或点击播放。',
                'A result was not fully heard. Ask to hear each result, or use Read result.',
              );
        if (ev.type === 'history.cleared') {
          resultCards.clear();
          taskData.clear();
          $('results').replaceChildren();
          $('transcript').textContent = '';
          $('debug').textContent = '';
          $('status').textContent = ui('历史已清空', 'History cleared');
        }
        if (ev.type === 'requests.not_started')
          $('status').textContent =
            ui('这些要求尚未开始：', 'These requests have not started: ') +
            ev.payload.requests.join('；');
        if (!ev.type.startsWith('venus.')) log(ev);
        if (ev.type === 'feedback.saved') $('feedbackStatus').textContent = '反馈已保存。';
      } else if (message.type === 'error') {
        log(message);
        $('status').textContent = message.message;
      }
    };
    socket.onclose = () => end();
    node.port.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === 'input' && connected && socket.readyState === 1) {
        const captureTs = audioCaptureTimestamp(context, msg.input_context_end_s);
        if (
          window.dispatchEvent(
            new CustomEvent('duplex.input', {
              detail: { pcm: msg.pcm, capture_ts_ms: captureTs },
              cancelable: true,
            }),
          )
        )
          socket.send(msg.pcm.buffer);
      }
      if (msg.type === 'ack')
        send('playback.ack', {
          ...msg.payload,
          ...audioPlaybackTimestamp(context, msg.payload.playback_context_end_s),
        });
      if (msg.type === 'event') send('playback.event', msg.payload);
      if (msg.type === 'flush.complete') flushed?.();
    };
  } catch (error) {
    log({ error: String(error) });
    await end();
    $('status').textContent = String(error);
  }
};
$('stop').onclick = () => {
  pendingStops += 1;
  epoch += 1;
  node?.port.postMessage({ type: 'clear', epoch });
  send('stop_speaking', {});
};
$('disconnect').onclick = end;
$('sendFeedback').onclick = () => {
  const text = $('feedback').value.trim();
  if (!text || !connected) {
    $('feedbackStatus').textContent = '请在对话连接时保存反馈。';
    return;
  }
  send('feedback', { text });
  $('feedback').value = '';
};
