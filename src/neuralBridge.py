"""
neuralBridge.py — Cortical Labs CL SDK integration for erebus
------------------------------------------------------------
Runs a background thread that samples live neural metrics
from the CL SDK (simulated or real CL1 hardware) and
exposes them for injection into every decision cycle.

If CL SDK is unavailable or errors out, all calls degrade
gracefully — erebus keeps running without neural context.
"""

import threading
import time
import logging
import os

logger = logging.getLogger("neuralBridge")

# ── Mode thresholds ───────────────────────────────────────────────
# These map network dynamics to erebus behavioral states.
# Tuned for the SDK's default random spike generator.
MODE_THRESHOLDS = {
    "critical":  {"min_bursts": 5, "min_entropy": 0.65},
    "bursting":  {"min_bursts": 2, "min_entropy": 0.0},
    "searching": {"min_bursts": 0, "min_entropy": 0.55},
    "active":    {"min_bursts": 0, "min_entropy": 0.0},
    "silent":    {},   # fallback
}

SAMPLE_WINDOW_SEC  = 8    # record this many seconds per sample
SAMPLE_INTERVAL    = 45   # re-sample every N seconds
HISTORY_MAXLEN     = 120  # 120 samples × 45s ≈ 90 min of history

# ── Environment detection ─────────────────────────────────────────
# The CL SDK requires a subprocess data producer that cannot run in
# sandboxed cloud environments (Render, Railway, Heroku, etc.).
# Detect and go straight to stub — no attempt, no timeout, no spam.
_SANDBOXED = bool(
    os.environ.get("RENDER") or
    os.environ.get("RAILWAY_ENVIRONMENT") or
    os.environ.get("HEROKU_APP_NAME") or
    os.environ.get("DYNO")
)

_CL_AVAILABLE = False
if _SANDBOXED:
    logger.info("[neural] sandboxed environment (Render) — using stub, skipping cl.open()")
else:
    try:
        import cl  # noqa: E402
        _CL_AVAILABLE = True
        logger.info("[neural] cl-sdk available — real sampling enabled")
    except ImportError:
        logger.warning("[neural] cl-sdk not installed — using stub data")


class NeuralBridge:
    """
    Background thread that samples the CL SDK and exposes:
      .get_state()         -> dict of latest metrics
      .format_for_prompt() -> str injected into erebus's prompt
      .get_history()       -> list of recent snapshots
    """

    def __init__(self):
        self._state: dict   = {}
        self._history: list = []
        self._lock          = threading.Lock()
        self._running       = False
        self._thread        = None
        self._error_count   = 0
        self._last_sample   = 0.0

    # ── Public API ────────────────────────────────────────────────

    def start(self):
        """Start background sampling thread. Safe to call multiple times."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="NeuralBridgeThread"
        )
        self._thread.start()
        logger.info("[neural] background sampler started")

    def stop(self):
        self._running = False

    def get_state(self) -> dict:
        """Return latest neural state snapshot (or empty dict if not ready)."""
        with self._lock:
            return dict(self._state)

    def get_history(self) -> list:
        """Return recent history snapshots (newest last)."""
        with self._lock:
            return list(self._history)

    def is_ready(self) -> bool:
        return bool(self._state)

    def format_for_prompt(self) -> str:
        """
        Return a compact neural state string ready to inject into erebus's prompt.
        Returns empty string if no data available yet.
        """
        s = self.get_state()
        if not s:
            return ""

        mode    = s.get("mode", "unknown").upper()
        sr      = s.get("spike_rate_hz", 0)
        bursts  = s.get("burst_count", 0)
        entropy = s.get("entropy_mean", 0)
        isi     = s.get("isi_mean_s", 0)
        age_s   = int(time.time() - s.get("sampled_at", 0))

        return (
            f"NEURAL STATE [{mode}] (sampled {age_s}s ago):\n"
            f"spike_rate={sr:.2f}hz  bursts={bursts}  "
            f"entropy={entropy:.3f}  isi={isi:.3f}s"
        )

    # ── Background loop ───────────────────────────────────────────

    def _loop(self):
        # Stagger first sample so it doesn't block startup
        time.sleep(5)
        while self._running:
            try:
                state = self._sample()
                if state:
                    with self._lock:
                        self._state = state
                        self._history.append(state)
                        if len(self._history) > HISTORY_MAXLEN:
                            self._history = self._history[-HISTORY_MAXLEN:]
                    self._error_count = 0
                    logger.info(
                        f"[neural] {state['mode'].upper()} | "
                        f"spikes={state['spike_rate_hz']:.2f}hz "
                        f"bursts={state['burst_count']} "
                        f"entropy={state['entropy_mean']:.3f}"
                    )
            except Exception as e:
                self._error_count += 1
                # If cl.open() fails even once, permanently fall back to stub
                # so we don't spam logs with repeated timeouts
                if self._error_count >= 1 and _CL_AVAILABLE:
                    logger.warning(f"[neural] cl.open() failed: {e} — permanently switching to stub")
                    self._force_stub = True
                else:
                    logger.warning(f"[neural] sample error #{self._error_count}: {e}")

            time.sleep(SAMPLE_INTERVAL)

    def _sample(self) -> dict | None:
        """
        Open a CL session, record SAMPLE_WINDOW_SEC of activity,
        compute metrics, return state dict.
        Falls back to stub data if cl-sdk is unavailable or failed.
        """
        if not _CL_AVAILABLE or getattr(self, "_force_stub", False):
            return self._stub_state()

        return self._sample_cl()

    def _sample_cl(self) -> dict | None:
        """Run actual CL SDK sampling."""
        import cl  # local import so startup is never blocked

        # CL_SDK_ACCELERATED_TIME=1 is set in .env for fast simulation
        # CL_SDK_REPLAY_PATH points to a replay file if available
        with cl.open() as neurons:
            # Record a short window
            recording = neurons.record(
                stop_after_seconds=SAMPLE_WINDOW_SEC,
                include_spikes=True,
                include_stims=False,
                include_raw_samples=False,  # saves memory — we don't need waveforms
            )

            # Run the closed loop during the recording window
            for tick in neurons.loop(
                ticks_per_second=50,
                stop_after_seconds=SAMPLE_WINDOW_SEC,
                ignore_jitter=True,
            ):
                pass

            recording.wait_until_stopped()
            rv = recording.open()

            # ── Compute metrics ───────────────────────────────────
            firing = rv.analyse_firing_stats(bin_size_sec=0.5)
            bursts = rv.analyse_network_bursts(
                bin_size_sec=0.1,
                onset_freq_hz=2.0,
                offset_freq_hz=0.5,
            )
            entropy = rv.analyse_information_entropy(bin_size_sec=0.1)

            # Per-channel spike rates for heatmap
            channel_rates = [round(r, 3) for r in firing.channel_mean_firing_rates]

            state = {
                "spike_rate_hz":   round(float(firing.culture_mean_firing_rates), 3),
                "burst_count":     int(bursts.network_burst_count),
                "burst_duration_s": round(float(bursts.total_network_burst_duration_sec), 3),
                "entropy_mean":    round(float(entropy.information_entropy_per_time_bin.mean()), 4),
                "entropy_max":     round(float(entropy.information_entropy_per_time_bin.max()), 4),
                "isi_mean_s":      round(float(firing.culture_ISI_mean or 0), 4),
                "channel_rates":   channel_rates,  # list of 64 floats for MEA heatmap
                "mode":            self._classify_mode(bursts, entropy),
                "sampled_at":      time.time(),
                "source":          "cl_sdk",
            }

        return state

    def _stub_state(self) -> dict:
        """
        Fallback when cl-sdk is not installed.
        Generates plausible-looking values so the rest of the
        system works without the SDK present.
        """
        import random
        import math

        t     = time.time()
        cycle = (t % 300) / 300   # 5-min sinusoidal cycle

        spike_rate = 2.0 + 6.0 * abs(math.sin(cycle * math.pi))
        entropy    = 0.3 + 0.5 * abs(math.sin(cycle * math.pi * 1.3))
        bursts     = int(spike_rate / 2)

        # 64 channel rates (8x8 MEA layout)
        channel_rates = [
            round(max(0, spike_rate + random.gauss(0, 1.5)), 3)
            for _ in range(64)
        ]

        return {
            "spike_rate_hz":    round(spike_rate, 3),
            "burst_count":      bursts,
            "burst_duration_s": round(bursts * 0.4, 3),
            "entropy_mean":     round(entropy, 4),
            "entropy_max":      round(min(1.0, entropy + 0.15), 4),
            "isi_mean_s":       round(1.0 / max(0.1, spike_rate), 4),
            "channel_rates":    channel_rates,
            "mode":             self._classify_mode_raw(bursts, entropy),
            "sampled_at":       t,
            "source":           "stub",
        }

    # ── Mode classification ───────────────────────────────────────

    def _classify_mode(self, bursts, entropy) -> str:
        import numpy as np
        e = float(np.mean(entropy.information_entropy_per_time_bin))
        b = int(bursts.network_burst_count)
        return self._classify_mode_raw(b, e)

    def _classify_mode_raw(self, burst_count: int, entropy_mean: float) -> str:
        b, e = burst_count, entropy_mean
        if   b >= 5  and e >= 0.65: return "critical"
        elif b >= 2:                 return "bursting"
        elif          e >= 0.55:    return "searching"
        elif b == 0  and e < 0.25:  return "silent"
        else:                       return "active"
