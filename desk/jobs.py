"""Long-running work: an agent analysis or an agent backtest, one at a time.

A run makes many LLM calls and costs money, so runs queue behind one worker
rather than racing each other.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

REPORTS = (
    ("market_report", "Market / technicals"),
    ("sentiment_report", "Sentiment"),
    ("news_report", "News"),
    ("fundamentals_report", "Fundamentals"),
    ("investment_plan", "Research manager"),
    ("trader_investment_plan", "Trader"),
    ("final_trade_decision", "Portfolio manager: final decision"),
)


class _Progress(BaseCallbackHandler):
    """Turns the graph's LLM and tool activity into a readable activity log."""

    def __init__(self, job: Job):
        self.job, self._node = job, None

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        node = (metadata or {}).get("langgraph_node")
        if node and node != self._node:
            self._node = node
            self.job.log(f"{node} is working")
        self.job.count("llm_calls")

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.job.log(f"  tool: {(serialized or {}).get('name', 'tool')}")
        self.job.count("tool_calls")


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.title = kind, title
        self.status = "queued"
        self.created = time.time()
        self.finished: float | None = None
        self.events: list[dict] = []
        self.counters: dict[str, int] = {}
        self.result: dict | None = None
        self.error: str | None = None
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        with self._lock:
            self.events.append({"t": round(time.time() - self.created, 1), "message": message})
            del self.events[:-400]

    def count(self, name: str) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + 1

    def view(self, full: bool = True) -> dict:
        with self._lock:
            out = {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                   "elapsed": round((self.finished or time.time()) - self.created, 1),
                   "counters": dict(self.counters), "error": self.error}
            if full:
                out["events"] = list(self.events)
                out["result"] = self.result
            return out


class Jobs:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desk-job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, title: str, fn) -> Job:
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            for stale in sorted(self._jobs.values(), key=lambda j: j.created)[:-30]:
                if stale.status in ("done", "error"):
                    self._jobs.pop(stale.id, None)

        def run():
            job.status = "running"
            try:
                job.result = fn(job)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - shown to the user as the job's error
                logger.exception("job %s failed", job.id)
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "error"
            finally:
                job.finished = time.time()

        self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return [j.view(full=False) for j in jobs]


def analyze(job: Job, config: dict, analysts: list[str], symbol: str, date: str, portfolio=None) -> dict:
    """One agent run for ``symbol`` on ``date``: the rating and every report."""
    from tradingagents.agents.rating import run_rating
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    job.log(f"Building agents ({config['llm_provider']})")
    graph = TradingAgentsGraph(tuple(analysts), config=config, callbacks=[_Progress(job)])
    job.log(f"Analysing {symbol} as of {date}")
    try:
        state, signal = graph.propagate(symbol, date, portfolio=portfolio)
    except Exception as exc:
        if "connection" in f"{type(exc).__name__} {exc}".lower():
            where = config.get("backend_url") or f"the default address of {config['llm_provider']}"
            raise RuntimeError(
                f"Could not reach the AI service at {where}. Check the Endpoint URL in Settings (it must be "
                f"reachable from this server; 'localhost' there means the server itself) and that the service "
                f"is running. Use Settings > Test AI connection. ({type(exc).__name__}: {exc})") from exc
        raise
    report_dir = None
    try:
        report_dir = str(graph.save_reports(state, symbol))
    except Exception as exc:  # noqa: BLE001 - the analysis stands without the saved copy
        job.log(f"Report not saved: {exc}")
    return {
        "symbol": symbol, "date": date, "rating": run_rating(state) or signal,
        "reports": [{"key": key, "title": title, "text": state.get(key) or ""}
                    for key, title in REPORTS if state.get(key)],
        "report_dir": report_dir, "settings": graph.run_settings(),
    }


def agent_backtest(job: Job, config: dict, analysts: list[str], symbols: list[str], dates: list[str]) -> dict:
    """The agents over a grid of symbols and dates, scored against the Nifty."""
    from tradingagents.backtest import run_backtest, summarize

    def progress(done, total, ticker, date):
        job.log(f"[{done}/{total}] {ticker} on {date}")

    result = run_backtest(symbols, dates, config, selected_analysts=tuple(analysts), progress=progress)
    summary = summarize(result)
    return {
        "run_id": result.run_id, "cells_run": result.cells_run, "skipped": result.skipped,
        "failures": [list(f) for f in result.failures],
        "settlement_failures": [list(f) for f in result.settlement_failures],
        "resolved": summary.resolved, "pending": summary.pending, "unscored": summary.unscored,
        "by_rating": [{"rating": rating, "count": score.count,
                       "hit_rate_pct": None if score.hit_rate is None else round(score.hit_rate * 100, 1),
                       "mean_alpha_pct": round(score.mean_alpha * 100, 2)}
                      for rating, score in summary.by_rating.items()],
        "text": summary.render(), "log_path": str(result.log_path),
    }
