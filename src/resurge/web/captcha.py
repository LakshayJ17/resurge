"""Self-hosted proof-of-work bot check for the public forms (sign-up and log-in), with no third-party service.

The server hands out a signed puzzle: a random salt and the SHA-256 of salt + a secret number below ``max_number``.
The browser finds the number by trying them in order (about half a second on a laptop), then sends it back with the
form. The server checks the hash, its own HMAC signature, the expiry and that the puzzle was not used before.

This is the scheme ALTCHA uses. People never see a puzzle; scripts have to spend CPU time on every attempt, which
together with the per-network limits makes mass sign-ups and password guessing slow and expensive. It is not proof
of a human: a determined attacker can still pay that cost, so it complements the rate limits rather than replacing
them. Cloudflare Turnstile remains available as an extra layer (TURNSTILE_* settings).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict

ALGORITHM = "SHA-256"
DEFAULT_MAX_NUMBER = 100_000
TTL_S = 600


class ProofOfWork:
    """Issues and verifies single-use puzzles. The signing key lives only in this process, so a restart simply makes
    the browser fetch a new puzzle."""

    MAX_USED = 100_000

    def __init__(self, max_number: int = DEFAULT_MAX_NUMBER, ttl_s: int = TTL_S, key: bytes | None = None) -> None:
        self.max_number = max(1_000, int(max_number))
        self.ttl_s = ttl_s
        self._key = key or secrets.token_bytes(32)
        self._used: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _sign(self, salt: str, challenge: str, max_number: int) -> str:
        return hmac.new(self._key, f"{salt}|{challenge}|{max_number}".encode(), hashlib.sha256).hexdigest()

    def issue(self) -> dict:
        salt = f"{int(time.time()) + self.ttl_s}.{secrets.token_hex(12)}"
        number = secrets.randbelow(self.max_number + 1)
        challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
        return {"algorithm": ALGORITHM, "salt": salt, "challenge": challenge, "maxnumber": self.max_number,
                "signature": self._sign(salt, challenge, self.max_number)}

    def verify(self, solution: str | dict | None) -> bool:
        """True for a correct, unexpired, signed, not yet used solution. Accepts the JSON the browser sends."""
        try:
            data = json.loads(solution) if isinstance(solution, str) else solution
            if not isinstance(data, dict):
                return False
            salt, challenge, signature = str(data["salt"]), str(data["challenge"]), str(data["signature"])
            number, max_number = int(data["number"]), int(data["maxnumber"])
        except (KeyError, TypeError, ValueError):
            return False
        if len(salt) > 64 or len(challenge) != 64 or len(signature) != 64 or not 0 <= number <= max_number:
            return False
        try:
            expires = int(salt.split(".", 1)[0])
        except ValueError:
            return False
        now = time.time()
        if expires < now or expires > now + self.ttl_s + 5:
            return False
        if not hmac.compare_digest(self._sign(salt, challenge, max_number), signature):
            return False
        if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() != challenge:
            return False
        with self._lock:  # single use: a solved puzzle cannot be replayed for a second attempt
            while self._used and next(iter(self._used.values())) < now:
                self._used.popitem(last=False)
            if challenge in self._used:
                return False
            self._used[challenge] = float(expires)
            while len(self._used) > self.MAX_USED:
                self._used.popitem(last=False)
        return True


def solve(puzzle: dict) -> dict:
    """Reference solver (tests and scripts): the browser does the same in frontend/lib/pow.ts."""
    for n in range(int(puzzle["maxnumber"]) + 1):
        if hashlib.sha256(f"{puzzle['salt']}{n}".encode()).hexdigest() == puzzle["challenge"]:
            return {**puzzle, "number": n}
    raise ValueError("no solution")
