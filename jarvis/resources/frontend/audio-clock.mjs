const anchors = new WeakMap();

export function audioPlaybackTimestamp(context, endSeconds) {
  const stamp = context.getOutputTimestamp?.();
  if (
    stamp &&
    Number.isFinite(stamp.contextTime) &&
    stamp.contextTime > 0 &&
    Number.isFinite(stamp.performanceTime) &&
    stamp.performanceTime > 0
  ) {
    return {
      playback_end_client_ms:
        performance.timeOrigin + stamp.performanceTime + (endSeconds - stamp.contextTime) * 1000,
      playback_clock_method: 'audio_context_output_timestamp',
    };
  }
  return {
    playback_end_client_ms:
      performance.timeOrigin + performance.now() + (endSeconds - context.currentTime) * 1000,
    playback_clock_method: 'audio_context_callback_estimate',
  };
}

export function audioCaptureTimestamp(context, endSeconds, durationMs = 20) {
  let anchor = anchors.get(context);
  if (!anchor) {
    anchor = { epochMs: performance.timeOrigin + performance.now() - context.currentTime * 1000 };
    anchors.set(context, anchor);
    context.addEventListener(
      'statechange',
      () => {
        if (context.state !== 'running') anchors.delete(context);
      },
      { once: true },
    );
  }

  return Math.min(
    anchor.epochMs + endSeconds * 1000 - durationMs,
    performance.timeOrigin + performance.now() - durationMs,
  );
}
