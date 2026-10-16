from pathlib import Path


def load_weights(path):
    from safetensors.torch import load_file

    path = Path(path)
    if path.suffix != ".safetensors":
        raise ValueError("Adapter weights must use safetensors")
    return load_file(str(path), device="cpu")
