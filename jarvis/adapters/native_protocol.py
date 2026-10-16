class NativeParser:
    def __init__(self, tokenizer, marker_only=False):
        self.tokenizer = tokenizer
        self.marker_only = marker_only
        self.open = tokenizer.convert_tokens_to_ids("<delegate>")
        self.close = tokenizer.convert_tokens_to_ids("</delegate>")
        self.end = tokenizer.convert_tokens_to_ids("<|turn_eos|>")
        self.im_end = tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.think = tokenizer.convert_tokens_to_ids("<think>")
        self.think_end = tokenizer.convert_tokens_to_ids("</think>")
        self.boundaries = {
            tokenizer.convert_tokens_to_ids(s)
            for s in ["<|speak|>", "<|chunk_eos|>", "<unit>", "</unit>", "<|tts_bos|>"]
        }
        self.special = (
            set(tokenizer.all_special_ids)
            | self.boundaries
            | {self.open, self.close, self.end, self.im_end, self.think, self.think_end}
        )
        self.inside = False
        self.invalid = False
        self.hidden = False
        self.payload = []
        self.rejections = []

    def feed(self, ids):
        visible = []
        requests = []
        for token in ids:
            if token == self.open:
                if self.inside:
                    self.invalid = True
                    self.payload = []
                else:
                    self.inside = True
                    self.invalid = False
                    self.payload = []
            elif token == self.close:
                if self.inside:
                    text = self.tokenizer.decode(
                        self.payload, skip_special_tokens=True
                    ).strip()
                    if (
                        not self.invalid
                        and (text or self.marker_only)
                        and (len(text) <= 2000)
                    ):
                        requests.append(text or "__native_delegate__")
                    else:
                        self.rejections.append("malformed_or_empty_delegate")
                self.inside = False
                self.invalid = False
                self.hidden = False
                self.payload = []
            elif token in {self.end, self.im_end}:
                if self.inside:
                    self.rejections.append("unclosed_delegate")
                self.inside = False
                self.invalid = False
                self.hidden = False
                self.payload = []
            elif token == self.think:
                self.hidden = True
            elif token == self.think_end:
                self.hidden = False
            elif self.inside:
                if token in self.boundaries:
                    continue
                if token in self.special:
                    self.invalid = True
                else:
                    self.payload.append(token)
                if len(self.payload) > 1024:
                    self.invalid = True
                    self.payload = []
            elif not self.hidden and token not in self.special:
                visible.append(token)
        return (self.tokenizer.decode(visible, skip_special_tokens=True), requests)
