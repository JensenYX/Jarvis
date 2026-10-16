class DuplexAudio extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.epoch = 0;
    this.input = [];
    this.clock = 0;
    this.nextInput = 0;
    this.previous = 0;
    this.outputClock = 0;
    this.paused = false;
    this.port.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === 'reset') {
        this.outputClock = 0;
        this.queue = [];
        this.epoch = msg.epoch;
        this.paused = false;
        return;
      }
      if (['pause', 'resume'].includes(msg.type) && msg.epoch === this.epoch) {
        this.paused = msg.type === 'pause';
        return;
      }
      if (msg.type === 'clear' && msg.epoch >= this.epoch) {
        for (const item of this.queue) this.ack(item, true);
        this.queue = [];
        this.epoch = msg.epoch;
        this.paused = false;
        this.event('assistant.playback.cleared', { generation_epoch: this.epoch });
        if (msg.flush) this.port.postMessage({ type: 'flush.complete' });
      } else if (msg.type === 'audio' && msg.generation_epoch === this.epoch) {
        this.queue.push({ ...msg, cursor: 0, reported: 0, started: false });
      }
    };
  }
  event(type, payload) {
    this.port.postMessage({ type: 'event', payload: { type, payload } });
  }
  ack(item, interrupted = false, clock = this.outputClock) {
    item.reported = Math.min(item.pcm.length, Math.floor(item.cursor));
    const contextEnd = currentTime + (clock - this.outputClock) / sampleRate;
    this.port.postMessage({
      type: 'ack',
      payload: {
        generation_epoch: item.generation_epoch,
        chunk_id: item.chunk_id,
        played_samples: item.reported,
        interrupted,
        playback_end_sample: Math.round((clock * 24000) / sampleRate),
        playback_context_end_s: contextEnd,
      },
    });
  }
  process(inputs, outputs) {
    const mic = inputs[0]?.[0];
    if (mic)
      for (let inputIndex = 0; inputIndex < mic.length; inputIndex++) {
        const value = mic[inputIndex];
        while (this.nextInput <= this.clock) {
          const fraction = this.nextInput - (this.clock - 1);
          this.input.push(
            this.previous + (value - this.previous) * Math.max(0, Math.min(1, fraction)),
          );
          this.nextInput += sampleRate / 16000;
          if (this.input.length === 320) {
            const pcm = Float32Array.from(this.input);
            this.input = [];
            this.port.postMessage(
              {
                type: 'input',
                pcm,
                input_context_end_s: currentTime + (inputIndex + 1) / sampleRate,
              },
              [pcm.buffer],
            );
          }
        }
        this.previous = value;
        this.clock++;
      }
    const out = outputs[0][0];
    out.fill(0);
    if (this.paused) {
      this.outputClock += out.length;
      return true;
    }
    for (let i = 0; i < out.length; i++) {
      const item = this.queue[0];
      if (!item) break;
      if (
        item.expires_at_context_s !== undefined &&
        currentTime + i / sampleRate >= item.expires_at_context_s
      ) {
        this.ack(item, true, this.outputClock + i);
        this.queue.shift();
        this.event('assistant.playback.expired', {
          generation_epoch: item.generation_epoch,
          chunk_id: item.chunk_id,
          response_id: item.response_id,
          screen_decision_id: item.screen_decision_id,
        });
        continue;
      }
      if (!item.started) {
        item.started = true;
        this.event('assistant.playback.started', {
          generation_epoch: item.generation_epoch,
          chunk_id: item.chunk_id,
          response_id: item.response_id,
        });
      }
      const index = Math.floor(item.cursor),
        fraction = item.cursor - index;
      out[i] =
        item.pcm[index] * (1 - fraction) +
        (item.pcm[Math.min(index + 1, item.pcm.length - 1)] || 0) * fraction;
      item.cursor += 24000 / sampleRate;
      if (item.cursor >= item.pcm.length) {
        item.cursor = item.pcm.length;
        this.ack(item, false, this.outputClock + i + 1);
        this.queue.shift();
        this.event('assistant.playback.ended', {
          generation_epoch: item.generation_epoch,
          chunk_id: item.chunk_id,
          played_samples: item.pcm.length,
        });
      } else if (item.cursor - item.reported >= 1200)
        this.ack(item, false, this.outputClock + i + 1);
    }
    this.outputClock += out.length;
    return true;
  }
}
registerProcessor('duplex-audio', DuplexAudio);
