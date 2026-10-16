from silero_vad import load_silero_vad


class BoundedVAD:
    def __init__(self):
        self.models = [load_silero_vad(onnx=True) for _ in range(3)]
        self.reset_states()

    def reset_states(self):
        for model in self.models:
            model.reset_states()
        self.step = 0
        self.ages = [0, 0, 0]

    def __call__(self, block, rate):
        if rate != 16000 or len(block) != 512:
            raise ValueError("Expected mono 16 kHz PCM in 32 ms blocks")
        values = []
        for i, model in enumerate(self.models):
            if i and self.step and ((self.step - (i - 1) * 80) % 160 == 0):
                model.reset_states()
                self.ages[i] = 0
            p = model(block, rate)
            self.ages[i] += 1
            if i == 0 or self.ages[i] >= 8 or self.step < 8:
                values.append(p)
        self.step += 1
        return max(values, key=lambda p: float(p.item()))
