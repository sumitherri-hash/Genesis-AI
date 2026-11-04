"""Optional neural model toolkit for GENESIS.

PyTorch is an optional extra so the core service remains lightweight on free hosting.
No user data is used for training automatically; model training must be explicitly requested.
"""
from __future__ import annotations

from typing import Any

try:
    import torch
    from torch import nn
    TORCH_AVAILABLE = True
except Exception:  # Keep GENESIS bootable without the large optional dependency.
    torch = None
    nn = None
    TORCH_AVAILABLE = False


MODEL_CATALOG = {
    "autoencoder": {
        "purpose": "Learn a compact representation and flag unusual records using reconstruction error.",
        "genesis_uses": ["quality anomaly detection", "unusual business metrics", "data compression"],
        "caution": "Needs representative numeric training data; an anomaly score is not proof of fraud or failure.",
    },
    "recurrent_neural_network": {
        "purpose": "Model ordered sequences where earlier values influence later predictions.",
        "genesis_uses": ["time-series forecasting", "demand trends", "sequence classification"],
        "caution": "Forecasts require enough clean historical observations and must be evaluated against baselines.",
    },
    "variational_autoencoder": {
        "purpose": "Learn a probabilistic latent representation and sample from that learned distribution.",
        "genesis_uses": ["latent-space exploration", "research simulations", "synthetic numeric examples"],
        "caution": "Generated samples are synthetic and must never be presented as real evidence or customer data.",
    },
}


if TORCH_AVAILABLE:
    class AutoEncoder(nn.Module):
        def __init__(self, input_dim: int, latent_dim: int = 8):
            super().__init__()
            if input_dim < 1 or latent_dim < 1:
                raise ValueError("input_dim and latent_dim must be positive")
            hidden = max(16, min(256, input_dim * 2))
            self.encoder = nn.Sequential(nn.Linear(input_dim, hidden), nn.ReLU(), nn.Linear(hidden, latent_dim))
            self.decoder = nn.Sequential(nn.Linear(latent_dim, hidden), nn.ReLU(), nn.Linear(hidden, input_dim))

        def forward(self, x):
            z = self.encoder(x)
            return self.decoder(z)

    class SequenceRNN(nn.Module):
        def __init__(self, input_dim: int, hidden_dim: int = 32, output_dim: int = 1, num_layers: int = 1):
            super().__init__()
            if min(input_dim, hidden_dim, output_dim, num_layers) < 1:
                raise ValueError("All dimensions must be positive")
            self.rnn = nn.RNN(input_dim, hidden_dim, num_layers=num_layers, batch_first=True, nonlinearity="tanh")
            self.head = nn.Linear(hidden_dim, output_dim)

        def forward(self, x):
            sequence, _ = self.rnn(x)
            return self.head(sequence[:, -1, :])

    class VariationalAutoEncoder(nn.Module):
        def __init__(self, input_dim: int, latent_dim: int = 8):
            super().__init__()
            if input_dim < 1 or latent_dim < 1:
                raise ValueError("input_dim and latent_dim must be positive")
            hidden = max(16, min(256, input_dim * 2))
            self.encoder = nn.Sequential(nn.Linear(input_dim, hidden), nn.ReLU())
            self.mu = nn.Linear(hidden, latent_dim)
            self.logvar = nn.Linear(hidden, latent_dim)
            self.decoder = nn.Sequential(nn.Linear(latent_dim, hidden), nn.ReLU(), nn.Linear(hidden, input_dim))

        def encode(self, x):
            h = self.encoder(x)
            return self.mu(h), self.logvar(h)

        def reparameterize(self, mu, logvar):
            std = torch.exp(0.5 * logvar)
            return mu + torch.randn_like(std) * std

        def forward(self, x):
            mu, logvar = self.encode(x)
            z = self.reparameterize(mu, logvar)
            reconstruction = self.decoder(z)
            return reconstruction, mu, logvar

        @staticmethod
        def loss(reconstruction, original, mu, logvar):
            reconstruction_loss = nn.functional.mse_loss(reconstruction, original, reduction="sum")
            kl_loss = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
            return reconstruction_loss + kl_loss
else:
    class _TorchRequired:
        def __init__(self, *args: Any, **kwargs: Any):
            raise RuntimeError("PyTorch is not installed. Install the optional requirements-neural.txt extra.")

    AutoEncoder = SequenceRNN = VariationalAutoEncoder = _TorchRequired


def status() -> dict:
    return {
        "module": "GENESIS Neural Models",
        "runtime": "pytorch" if TORCH_AVAILABLE else "not_installed",
        "ready": TORCH_AVAILABLE,
        "models": MODEL_CATALOG,
        "training": "manual_and_explicit_only",
        "data_policy": "No automatic training on personal data. Use approved, minimized datasets.",
        "install_hint": "pip install -r requirements-neural.txt" if not TORCH_AVAILABLE else None,
    }


def install(app):
    from fastapi import Header
    import app as core

    @app.get("/api/internal/neural/status")
    def neural_status(x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        return status()

    @app.get("/api/internal/neural/architectures")
    def neural_architectures(x_genesis_key: str | None = Header(default=None)):
        core.require_admin(x_genesis_key)
        return {"architectures": MODEL_CATALOG, "runtime_ready": TORCH_AVAILABLE}
