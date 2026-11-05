from dataclasses import asdict, dataclass, field, fields
import math


@dataclass
class FieldConfig:
    enabled: bool = False
    read_only: bool = False
    eta: float = 1e-4
    amplitude_threshold: float = 2.5
    frequency_floor: float = .001
    frequency_half_life_tokens: float = 256
    step_fraction_max: float = .001
    baseline_pull: float = 0
    commit_every_tokens: int = 1
    context_length: int = 4096
    role_scales: dict = field(default_factory=lambda: dict(user=1., assistant=1., tool=1., system=1.))
    template_token_write_scale: float = 0
    spectral_enabled: bool = True
    persistent_homology: bool = False
    window_tokens: int = 128
    stride_tokens: int = 32
    seed: int = 42
    writer: str = "metal"

    @classmethod
    def parse(cls, value=None):
        value = value or {}
        unknown = set(value) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError("Unknown configuration: " + ", ".join(sorted(unknown)))
        out = cls(**value)
        for name in ["eta", "amplitude_threshold", "frequency_floor", "frequency_half_life_tokens",
                     "step_fraction_max", "baseline_pull", "template_token_write_scale"]:
            v = getattr(out, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(name + " must be finite and nonnegative")
        if out.frequency_half_life_tokens == 0 or out.frequency_floor > 1:
            raise ValueError("Invalid frequency settings")
        for name in ["commit_every_tokens", "context_length", "window_tokens", "stride_tokens"]:
            v = getattr(out, name)
            if type(v) is not int or v < 1:
                raise ValueError(name + " must be a positive integer")
        if out.window_tokens < 10:
            raise ValueError("window_tokens must be at least 10")
        if type(out.seed) is not int or not 0 <= out.seed < 2**32:
            raise ValueError("seed must be uint32")
        if set(out.role_scales) != {"user", "assistant", "tool", "system"}:
            raise ValueError("All four substantive roles must be configured")
        if any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in out.role_scales.values()):
            raise ValueError("Role scales must be finite and nonnegative")
        for name in ["enabled", "read_only", "spectral_enabled", "persistent_homology"]:
            if type(getattr(out, name)) is not bool:
                raise ValueError(name + " must be boolean")
        if out.writer not in ("metal", "reference"):
            raise ValueError("Unknown writer")
        return out

    def to_dict(self):
        return asdict(self)

