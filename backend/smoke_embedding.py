"""Real CPU acceptance check. Run from backend with configured .env."""

import json
from math import isfinite

from app.adapters.embedding import embed_texts


if __name__ == "__main__":
    vectors = embed_texts(["\u5165\u5883\u65f6\u8bf7\u5411\u6d77\u5173\u7533\u62a5\u643a\u5e26\u7684\u7269\u54c1\u3002", "\uc785\uad6d \uc2dc \ud734\ub300\ud488\uc744 \uc138\uad00\uc5d0 \uc2e0\uace0\ud558\uc138\uc694."])
    assert len(vectors) == 2 and all(len(v) == 1024 for v in vectors)
    assert all(isfinite(x) for v in vectors for x in v)
    assert all(any(x != 0 for x in v) for v in vectors)
    print(json.dumps({"count": len(vectors), "dimensions": [len(v) for v in vectors],
                      "norms": [sum(x*x for x in v) ** .5 for v in vectors],
                      "first_values": [v[:3] for v in vectors]}, indent=2))
