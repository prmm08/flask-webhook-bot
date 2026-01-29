# utils.py
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import time

def round_qty(qty: float, step_size: float) -> float:
    q = Decimal(str(qty))
    step = Decimal(str(step_size))
    if step == 0:
        return float(q)
    rounded = (q // step) * step
    return float(rounded.quantize(step, rounding=ROUND_DOWN))

def make_client_ref(payload: dict) -> str:
    s = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(s.encode()).hexdigest()[:12]
    return f"cli-{int(time.time())}-{h}"
