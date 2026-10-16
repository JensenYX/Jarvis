class GameAnalysisAudio extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.clock = 0;
    this.next = 0;
    this.previous = 0;
    this.block = [];
    this.emitSilence = !!options?.processorOptions?.emitSilence;
  }
  process(inputs, outputs) {
    const channels = inputs[0];
    for (const out of outputs[0] || []) out.fill(0);
    if (!channels?.length && !this.emitSilence) return true;
    for (let i = 0; i < (channels?.[0]?.length ?? outputs[0][0].length); i++) {
      const sample = channels?.length
        ? channels.reduce((sum, c) => sum + (c[i] || 0), 0) / channels.length
        : 0;
      while (this.next <= this.clock) {
        const fraction = Math.max(0, Math.min(1, this.next - (this.clock - 1)));
        this.block.push(this.previous + (sample - this.previous) * fraction);
        this.next += sampleRate / 16000;
        if (this.block.length === 320) {
          const pcm = Float32Array.from(this.block);
          this.block = [];
          this.port.postMessage({ pcm, input_context_end_s: currentTime + (i + 1) / sampleRate }, [
            pcm.buffer,
          ]);
        }
      }
      this.previous = sample;
      this.clock++;
    }
    return true;
  }
}
registerProcessor('screen-game-analysis', GameAnalysisAudio);
