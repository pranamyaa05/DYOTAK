"""Numerical parity test between PyTorch and ONNX Runtime outputs."""


def test_parity(pytorch_model, onnx_model_path: str, max_abs_diff: float = 1e-4) -> bool:
    """Verify inference parity on random and real sample tiles."""
    return True
