"""Was the best setting skill or luck? The overfitting score and the settings heatmap.

Overfitting score (probability of backtest overfitting, Bailey, Borwein, Lopez de
Prado and Zhu, 2015). Every setting's day-by-day returns are cut into equal
blocks. For each way of picking half the blocks, the setting that did best on
that half is found, then we look where it ranks on the other half. If the
"best" setting often lands in the bottom half on data it did not see, picking
the best was mostly luck. The score is how often that happens.

Settings heatmap: the score of every combination of two settings, so you can
see whether the winner sits on a broad plateau (good) or a lone peak (luck).
"""

from __future__ import annotations

import itertools
import math
import random

import numpy as np
import pandas as pd

BLOCKS = 10
MAX_SPLITS = 400
MIN_DAYS_PER_BLOCK = 8


def _sharpe(block: np.ndarray) -> np.ndarray:
    mean, std = block.mean(axis=0), block.std(axis=0, ddof=1)
    out = np.full(block.shape[1], -np.inf)
    ok = std > 1e-12
    out[ok] = mean[ok] / std[ok]
    return out


def overfitting_score(returns: pd.DataFrame, blocks: int = BLOCKS, seed: int = 7) -> dict | None:
    """``returns``: one column per setting, one row per day (missing days count as 0). None when there is
    too little to judge: fewer than two settings that traded, or too few days."""
    frame = returns.fillna(0.0)
    frame = frame.loc[:, frame.std(ddof=1) > 1e-12]
    n_settings = frame.shape[1]
    if n_settings < 2:
        return None
    blocks = int(blocks) - int(blocks) % 2
    days = len(frame)
    while blocks >= 4 and days // blocks < MIN_DAYS_PER_BLOCK:
        blocks -= 2
    if blocks < 4:
        return None
    values = frame.to_numpy()
    edges = np.linspace(0, days, blocks + 1).astype(int)
    parts = [values[edges[i]:edges[i + 1]] for i in range(blocks)]
    splits = list(itertools.combinations(range(blocks), blocks // 2))
    if len(splits) > MAX_SPLITS:
        splits = random.Random(seed).sample(splits, MAX_SPLITS)
    logits, ranks = [], []
    for chosen in splits:
        rest = [i for i in range(blocks) if i not in chosen]
        seen = _sharpe(np.vstack([parts[i] for i in chosen]))
        unseen = _sharpe(np.vstack([parts[i] for i in rest]))
        best = int(np.argmax(seen))
        # Where the in-sample winner ranks out of sample: 1 = worst, n = best; ties share the middle rank.
        rank = (unseen < unseen[best]).sum() + 0.5 * ((unseen == unseen[best]).sum() - 1) + 1
        omega = rank / (n_settings + 1)
        logits.append(math.log(omega / (1 - omega)))
        ranks.append(omega)
    score = float(np.mean(np.array(logits) <= 0))
    pct = round(score * 100, 1)
    if pct < 20:
        verdict, text = "low", "Low. The best setting also tends to do well on data it did not see."
    elif pct <= 50:
        verdict, text = "medium", "Medium. The best setting is often only average on data it did not see."
    else:
        verdict, text = "high", ("High. The best setting usually does worse than most others on data it did not see: "
                                 "its win is mostly luck.")
    return {"score_pct": pct, "verdict": verdict, "text": text, "settings": n_settings, "blocks": blocks,
            "splits": len(splits), "median_unseen_rank_pct": round(float(np.median(ranks)) * 100, 1)}


def heatmap(rows: list[dict], names: list[str]) -> dict | None:
    """Scores laid out on the one or two settings that vary. ``rows`` are ``{"params", "score"}``
    (score None or -inf: too few trades). None when no setting varies or more than two do."""
    varying = [n for n in names if len({r["params"].get(n) for r in rows}) > 1]
    if not varying or len(varying) > 2:
        return None

    def value(r):
        s = r.get("score")
        return None if s is None or not math.isfinite(s) else round(float(s), 3)

    x = varying[0]
    xs = sorted({r["params"][x] for r in rows})
    if len(varying) == 1:
        by = {r["params"][x]: value(r) for r in rows}
        return {"x": x, "xs": xs, "y": None, "ys": [None], "cells": [[by.get(v) for v in xs]]}
    y = varying[1]
    ys = sorted({r["params"][y] for r in rows})
    by = {(r["params"][x], r["params"][y]): value(r) for r in rows}
    return {"x": x, "xs": xs, "y": y, "ys": ys, "cells": [[by.get((a, b)) for a in xs] for b in ys]}
