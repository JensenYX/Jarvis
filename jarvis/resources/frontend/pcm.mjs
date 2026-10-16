export function decodePCM(encoded, { format = 'f32le', sampleCount } = {}) {
  const raw = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
  const bytes = format === 'f32le' ? 4 : format === 's16le' ? 2 : 0;
  if (
    !bytes ||
    raw.byteLength % bytes ||
    (sampleCount !== undefined && raw.byteLength / bytes !== sampleCount)
  )
    throw new Error('Invalid PCM format or sample count');
  const data = new DataView(raw.buffer),
    pcm = new Float32Array(raw.byteLength / bytes);
  for (let i = 0; i < pcm.length; i++) {
    const value =
      format === 's16le' ? data.getInt16(i * 2, true) / 32768 : data.getFloat32(i * 4, true);
    if (!Number.isFinite(value)) throw new Error('Invalid PCM sample');
    pcm[i] = value;
  }
  return pcm;
}
