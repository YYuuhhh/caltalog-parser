"""Pinned, experimental CPU port of Baidu Unlimited-OCR (not a semantic reviewer)."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time


def verify_dependencies(path=Path("/opt/baidu-python.lock.json")):
    if not path.is_file():
        return  # Source-only contract tests do not have the optional runtime.
    locked = json.loads(path.read_text())
    installed = {
        d.metadata["Name"].lower().replace("_", "-"): d.version
        for d in importlib.metadata.distributions()
    }
    if installed != locked:
        raise RuntimeError(
            "Baidu CPU dependency versions differ from the prepared runtime; rebuild from the saved dependency image."
        )


MODEL = "baidu/Unlimited-OCR"
REVISION = "8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3"
HASHES = {
    "modeling_unlimitedocr.py": "3894696d68197ad00e89f0963880645dfce49b1933a9f27e78fbc05983547a78",
    "modeling_deepseekv2.py": "74e36e6bd0ba7bc565ef76464a99baa8e6bccb710ae9c1007b54ac30b855fa4c",
}


def port_source(name, text):
    if hashlib.sha256(text.encode()).hexdigest() != HASHES[name]:
        raise ValueError(
            f"Unexpected {name} source; refusing to patch an unreviewed revision."
        )
    if name == "modeling_unlimitedocr.py":
        replacements = [
            (".cuda()", ".to(device=self.device)", 17),
            ('torch.autocast("cuda", dtype=torch.bfloat16)', "nullcontext()", 3),
            (".to(torch.bfloat16)", ".to(dtype=self.dtype)", 5),
            ("max_length=max_length", "max_new_tokens=max_length", 3),
        ]
        for old, new, expected in replacements:
            if text.count(old) != expected:
                raise ValueError("CPU port patch count mismatch.")
            text = text.replace(old, new)
        return "from contextlib import nullcontext\n" + text
    return ("from transformers.generation import GenerationMixin\n" + text).replace(
        "class DeepseekV2ForCausalLM(DeepseekV2PreTrainedModel):",
        "class DeepseekV2ForCausalLM(DeepseekV2PreTrainedModel, GenerationMixin):",
    )


def prepare(root):
    from huggingface_hub import snapshot_download

    original = Path(
        snapshot_download(
            MODEL,
            revision=REVISION,
            cache_dir=root / "hub",
            allow_patterns=["*.json", "*.py", "*.safetensors", "*.jinja", "LICENSE"],
            max_workers=2,
        )
    )
    target = root / "cpu-model"
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for path in original.iterdir():
        if not path.is_file():
            continue
        dest = target / path.name
        if path.name in HASHES:
            code = port_source(path.name, path.read_text())
            dest.write_text(code)
            hashes[path.name] = hashlib.sha256(code.encode()).hexdigest()
        elif not dest.exists():
            dest.symlink_to(path.resolve())
    manifest = {
        "model": MODEL,
        "revision": REVISION,
        "device": "cpu",
        "experimental": True,
        "patch_sha256": hashes,
        "installed_at": time.time(),
    }
    (root / "installation.json").write_text(json.dumps(manifest))
    return manifest


def verify(root):
    manifest = json.loads((root / "installation.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Invalid Baidu CPU installation manifest.")
    if manifest.get("revision") != REVISION or manifest.get("device") != "cpu":
        raise ValueError("Baidu CPU installation does not match the pinned revision.")
    for name in HASHES:
        expected = hashlib.sha256(
            port_source(
                name,
                (
                    root
                    / "hub"
                    / ("models--baidu--Unlimited-OCR")
                    / "snapshots"
                    / REVISION
                    / name
                ).read_text(),
            ).encode()
        ).hexdigest()
        actual = hashlib.sha256((root / "cpu-model" / name).read_bytes()).hexdigest()
        if expected != actual or manifest["patch_sha256"].get(name) != expected:
            raise ValueError("Baidu CPU source checksum mismatch.")
    if not list((root / "cpu-model").glob("*.safetensors")):
        raise ValueError("Baidu model weights are missing.")
    return manifest


def infer(root, image, output, max_tokens=2048, threads=0):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from transformers import AutoModel, AutoTokenizer
    import system_diagnostics as diagnostics

    verify_dependencies()
    verify(root)
    if "+cpu" not in torch.__version__ or torch.version.cuda is not None:
        raise RuntimeError("The Baidu runtime requires a CPU-only PyTorch build.")
    if threads:
        torch.set_num_threads(threads)
    started = time.monotonic()
    progress = output.with_name("progress.json")
    diagnostics.atomic(
        progress,
        {
            "status": "loading",
            "started_at": time.time(),
            "model": MODEL,
            "device": "cpu",
        },
    )
    path = root / "cpu-model"
    tokenizer = AutoTokenizer.from_pretrained(
        path, trust_remote_code=True, local_files_only=True
    )
    model = (
        AutoModel.from_pretrained(
            path,
            trust_remote_code=True,
            local_files_only=True,
            torch_dtype=torch.float32,
            attn_implementation="eager",
            low_cpu_mem_usage=True,
        )
        .eval()
        .to("cpu")
    )
    load_seconds = time.monotonic() - started
    if any(p.device.type != "cpu" for p in model.parameters()):
        raise RuntimeError("Baidu model parameters must remain on CPU.")
    state = {
        "status": "prefill",
        "started_at": time.time() - load_seconds,
        "model": MODEL,
        "device": "cpu",
        "load_seconds": load_seconds,
        "tokens": 0,
    }
    diagnostics.atomic(progress, state)

    class Streamer:
        first = None
        prompt = True
        count = 0

        def put(self, value):
            if self.prompt:
                self.prompt = False
                return
            self.first = self.first or time.monotonic()
            self.count += value.numel()
            state.update(
                status="generating",
                tokens=self.count,
                tokens_per_second=round(
                    max(0, self.count - 1) / max(0.001, time.monotonic() - self.first),
                    3,
                ),
            )
            diagnostics.atomic(progress, state)

        def end(self):
            pass

    streamer = Streamer()
    original_generate = model.generate
    complete = False

    def generate(*args, **kwargs):
        nonlocal complete
        kwargs["streamer"] = streamer
        result = original_generate(*args, **kwargs)
        complete = result[0, -1].item() == tokenizer.eos_token_id
        return result

    model.generate = generate
    text = model.infer(
        tokenizer,
        prompt="<image>\n<|grounding|>Convert the document to markdown.",
        image_file=str(image),
        output_path=str(output.parent / "rendered"),
        eval_mode=True,
        save_results=False,
        max_length=max_tokens,
        temperature=0.0,
        base_size=1024,
        image_size=640,
        crop_mode=True,
    )
    result = {
        "model": MODEL,
        "revision": REVISION,
        "device": "cpu",
        "experimental": True,
        "source_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "complete": complete,
        "text": text,
        "tokens": streamer.count,
        "load_seconds": load_seconds,
        "elapsed_seconds": time.monotonic() - started,
        "tokens_per_second": state.get("tokens_per_second"),
    }
    diagnostics.atomic(output, result)
    diagnostics.atomic(
        progress, {**state, "status": "completed" if complete else "truncated"}
    )
