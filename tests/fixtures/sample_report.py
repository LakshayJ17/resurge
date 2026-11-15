"""Re-export of the packaged demo fixtures (they also power `resurge demo`)."""

from resurge.sample_report import (
    HOSTILE_URLS,
    INJECTION_PAYLOADS,
    make_adversarial_report,
    make_minimal_report,
    make_sample_report,
)

__all__ = [
    "HOSTILE_URLS",
    "INJECTION_PAYLOADS",
    "make_adversarial_report",
    "make_minimal_report",
    "make_sample_report",
]
