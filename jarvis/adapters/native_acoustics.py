import copy
import numpy as np
import torch


class SpeechAudio:
    def __init__(self, model, duplex):
        self.model = model
        self.duplex = duplex
        self.processor = copy.copy(duplex.processor)
        self.processor._streaming_mel_processor = copy.deepcopy(
            duplex.processor._streaming_mel_processor
        )
        self.reset()

    def reset(self):
        self.processor.reset_streaming()
        self.buffer = np.zeros(0, dtype=np.float32)
        self.index = 0
        self.cache = None

    def feed(self, pcm):
        d = self.duplex
        self.buffer = np.concatenate([self.buffer, pcm])
        if self.index == 0:
            required = int(d.FIRST_CHUNK_MS * d.SAMPLE_RATE / 1000)
            if len(self.buffer) < required:
                self.buffer = np.concatenate(
                    [
                        np.zeros(required - len(self.buffer), dtype=np.float32),
                        self.buffer,
                    ]
                )
        needed = self.processor.get_streaming_chunk_size()
        if len(self.buffer) < needed:
            raise ValueError("Incomplete native routing audio unit")
        feature = self.processor.process_audio_streaming(
            self.buffer[:needed], reset=False, return_batch_feature=True
        )
        feature = feature.to(d.device)
        previous = self.model.audio_past_key_values
        try:
            self.model.audio_past_key_values = self.cache
            nested = self.model.get_audio_embedding_streaming(
                feature,
                use_extra_context=True,
                prefix_extra_frames=0 if self.index == 0 else 2,
                suffix_extra_frames=2,
            )
            self.cache = self.model.audio_past_key_values
        finally:
            self.model.audio_past_key_values = previous
        unit = d.decoder.embed_token(d.unit_token_id)
        if unit.ndim == 1:
            unit = unit.unsqueeze(0)
        embeds = torch.cat([unit, *(t for group in nested for t in group)])
        consumed = int(
            (
                self.processor._streaming_mel_processor.get_config().get(
                    "effective_first_chunk_ms", d.FIRST_CHUNK_MS
                )
                if self.index == 0
                else d.CHUNK_MS
            )
            * d.SAMPLE_RATE
            / 1000
        )
        self.buffer = self.buffer[consumed:]
        self.index += 1
        return embeds.detach().cpu()
