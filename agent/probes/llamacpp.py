"""
Probe for llama.cpp service in Dohtar Monitor Agent.
Queries /health, /slots, /metrics, and /v1/models endpoints.

Uses Prometheus metrics counter deltas (tokens_predicted_total) combined
with generation speed gauge to detect completed runs with exact per-request
token counts and speeds.  Previous approach (slot transition timing fields)
failed because llama.cpp zeroes per-slot timing data when a slot goes idle.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class LlamaCppProbe:
    """Probes llama.cpp server endpoints with per-request run tracking."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.base_url = f"http://{endpoint}"
        self.timeout = aiohttp.ClientTimeout(total=5.0)

        # Completed runs detected this poll cycle
        self._pending_completed_runs: List[Dict[str, Any]] = []

        # ── Metrics-based run detection state ──
        # Cumulative Prometheus counters (never decrease except on server restart)
        self._prev_predicted_total: Optional[float] = None
        self._prev_evaluated_total: Optional[float] = None
        # Speed gauges saved while generating (used to compute duration after gen ends)
        self._last_speed_gen: Optional[float] = None
        self._last_speed_pp: Optional[float] = None
        # Whether the previous poll saw status == "generating"
        self._was_generating: bool = False
        # Whether metrics endpoint is available (skip gracefully if not)
        self._metrics_available: bool = True

    async def probe(self, session: aiohttp.ClientSession = None) -> Dict[str, Any]:
        result = {
            "type": "llm",
            "probe": "llamacpp",
            "endpoint": self.endpoint,
            "status": "down",
            "model": None,
            "slot": None,
            "kv_usage": None,
            "kv_tok": None,
            "total_slots": None,
            "busy_slots": None,
            "queue": None,
            "speed_gen": None,
            "speed_pp": None,
            "tok_pred": None,
            "tok_eval": None,
            "gen_progress": None,
            "n_predict": None,
            "active_tokens": None,
            "completed_runs": [],  # Exact per-request data from metrics deltas
        }

        own_session = session is None
        try:
            if own_session:
                session = aiohttp.ClientSession(timeout=self.timeout)

            # 1. Check health (fast, lightweight)
            health_data = await self._check_health(session)
            if health_data is None:
                self._was_generating = False
                return result

            result["status"] = "idle"

            # Use /health for slot counts (always available, even under load)
            if isinstance(health_data, dict) and "slots_idle" in health_data:
                idle = health_data.get("slots_idle", 0)
                processing = health_data.get("slots_processing", 0)
                result["total_slots"] = idle + processing
                result["busy_slots"] = processing
                result["queue"] = processing
                if processing > 0:
                    result["status"] = "generating"
                    result["slot"] = "processing"

            # 2. Get slots info (live status display — no transition detection)
            slots_data = await self._get_slots(session)
            if slots_data:
                result.update(slots_data)
            elif result["status"] == "generating":
                pass  # Keep generating status from health

            # 3. Get model name
            if not result.get("model"):
                models = await self._get_models(session)
                if models:
                    result["model"] = models[0]

            # 4. ALWAYS fetch metrics — needed for run detection + live speed
            if self._metrics_available:
                metrics_data = await self._get_metrics(session)
                if metrics_data is not None:
                    self._detect_completed_runs(metrics_data, result)
                else:
                    # Metrics endpoint not available — log once
                    if self._metrics_available:
                        logger.warning(
                            f"Metrics endpoint not available on {self.endpoint}. "
                            "Run detection requires --metrics flag on llama-server."
                        )
                        self._metrics_available = False

            # Update generation state for next poll
            is_generating = result["status"] == "generating"
            self._was_generating = is_generating

            # 5. Attach any completed runs
            if self._pending_completed_runs:
                for run in self._pending_completed_runs:
                    if not run.get("model") and result.get("model"):
                        run["model"] = result["model"]
                result["completed_runs"] = self._pending_completed_runs.copy()
                self._pending_completed_runs.clear()

            return result

        except Exception as e:
            logger.warning(f"llama.cpp probe failed for {self.endpoint}: {e}")
            return result
        finally:
            if own_session and session:
                await session.close()

    # ──────────────────────────────────────────────────────────────────────
    #  Run detection via Prometheus counter deltas
    # ──────────────────────────────────────────────────────────────────────

    def _detect_completed_runs(
        self, metrics: Dict[str, Any], result: Dict[str, Any]
    ) -> None:
        """Detect completed generation runs using /metrics counter deltas.

        Logic:
        - Track cumulative tokens_predicted_total counter across polls.
        - While generating, save the live speed gauge (tok/s).
        - When the counter increases AND status is now idle, record a
          completed run using: delta tokens + saved speed → duration.
        - Baseline counter is only updated when idle, so partial
          in-progress tokens don't cause premature run detection.
        """
        is_generating = result["status"] == "generating"

        # ── Live speed display (always apply if generating) ──
        speed_gen_gauge = metrics.get("speed_gen_gauge")
        speed_pp_gauge = metrics.get("speed_pp_gauge")

        if is_generating:
            if speed_gen_gauge and speed_gen_gauge > 0:
                result["speed_gen"] = speed_gen_gauge
                self._last_speed_gen = speed_gen_gauge
            if speed_pp_gauge and speed_pp_gauge > 0:
                result["speed_pp"] = speed_pp_gauge
                self._last_speed_pp = speed_pp_gauge

        # ── Counter-based run detection ──
        predicted = metrics.get("predicted_total")
        evaluated = metrics.get("evaluated_total")

        if predicted is None:
            # No counter data available (server may not expose it)
            return

        if self._prev_predicted_total is None:
            # First poll — establish baseline, no run detection yet
            self._prev_predicted_total = predicted
            self._prev_evaluated_total = evaluated
            return

        delta_predicted = predicted - self._prev_predicted_total
        delta_evaluated = 0
        if evaluated is not None and self._prev_evaluated_total is not None:
            delta_evaluated = evaluated - self._prev_evaluated_total

        # Counter went down → server restarted, reset baseline
        if delta_predicted < 0:
            logger.info(
                f"Metrics counter reset detected on {self.endpoint} "
                f"({self._prev_predicted_total} → {predicted}), resetting baseline"
            )
            self._prev_predicted_total = predicted
            self._prev_evaluated_total = evaluated
            self._last_speed_gen = None
            self._last_speed_pp = None
            return

        # Tokens were generated and we are NOW idle → generation completed
        if delta_predicted > 0 and not is_generating:
            if self._last_speed_gen and self._last_speed_gen > 0:
                t_gen_ms = delta_predicted / self._last_speed_gen * 1000
                t_pp_ms = (
                    delta_evaluated / self._last_speed_pp * 1000
                    if self._last_speed_pp and self._last_speed_pp > 0 and delta_evaluated > 0
                    else 0
                )

                completed_run = {
                    "ts": int(time.time() * 1000),
                    "endpoint": self.endpoint,
                    "model": result.get("model"),
                    "slot_idx": 0,
                    "tok_generated": int(delta_predicted),
                    "tok_prompt": int(max(0, delta_evaluated)),
                    "speed_gen": round(self._last_speed_gen, 1),
                    "speed_pp": round(self._last_speed_pp, 1) if self._last_speed_pp else None,
                    "t_generation_ms": round(t_gen_ms, 1),
                    "t_prompt_ms": round(t_pp_ms, 1),
                    "t_total_ms": round(t_gen_ms + t_pp_ms, 1),
                }
                self._pending_completed_runs.append(completed_run)
                logger.info(
                    f"Completed run on {self.endpoint}: "
                    f"{int(delta_predicted)} tok @ {self._last_speed_gen} tok/s, "
                    f"prompt {int(max(0, delta_evaluated))} tok"
                    + (f" @ {self._last_speed_pp} tok/s" if self._last_speed_pp else "")
                )
            else:
                logger.warning(
                    f"Generation completed on {self.endpoint} "
                    f"({int(delta_predicted)} tok predicted) but no saved speed gauge — "
                    "run not recorded. Was the agent restarted mid-generation?"
                )

        # Update baseline only when idle — during generation the counter is
        # still incrementing and we want the FULL delta when it finishes
        if not is_generating:
            self._prev_predicted_total = predicted
            self._prev_evaluated_total = evaluated

    # ──────────────────────────────────────────────────────────────────────
    #  Endpoint queries
    # ──────────────────────────────────────────────────────────────────────

    async def _check_health(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Check if service is healthy. Returns health data or None."""
        try:
            async with session.get(f"{self.base_url}/health") as resp:
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except Exception:
                        return {}
                return None
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            return None

    async def _get_slots(self, session: aiohttp.ClientSession) -> Dict[str, Any]:
        """Get slots info for live status display (KV cache, progress, speed).

        Note: Run detection is handled by _detect_completed_runs via /metrics
        counters, NOT by slot state transitions.  Slot timing fields are
        unreliable because llama.cpp zeroes them when a slot goes idle.
        """
        try:
            async with session.get(f"{self.base_url}/slots") as resp:
                if resp.status != 200:
                    logger.debug(f"Slots endpoint returned {resp.status}")
                    return {}

                data = await resp.json()

                if isinstance(data, list):
                    slots = data
                elif isinstance(data, dict):
                    slots = data.get("slots", [])
                else:
                    return {}

                if not slots:
                    return {}

                total_slots = len(slots)
                busy_slots = 0
                kv_usage_sum = 0
                kv_tok_sum = 0
                has_kv_data = False
                model_name = None
                any_processing = False

                total_n_predict = 0
                active_n_decoded = 0

                for slot in slots:
                    # Determine if this slot is currently processing
                    is_busy = False
                    if "is_processing" in slot:
                        is_busy = bool(slot["is_processing"])
                    else:
                        state = slot.get("state")
                        is_busy = state == 1 or state == "processing"

                    if is_busy:
                        busy_slots += 1
                        any_processing = True

                    # KV cache info
                    cache_tokens = (
                        slot.get("cache_tokens")
                        or slot.get("common", {}).get("cache_tokens")
                        or 0
                    )
                    n_ctx = slot.get("n_ctx", 0)

                    if cache_tokens:
                        kv_tok_sum += cache_tokens
                    if n_ctx > 0 and cache_tokens:
                        has_kv_data = True
                        kv_usage_sum += cache_tokens / n_ctx

                    # Model name
                    if not model_name:
                        m = slot.get("model") or slot.get("common", {}).get("model")
                        if m:
                            if "/" in str(m):
                                m = str(m).rsplit("/", 1)[-1]
                            if "\\" in str(m):
                                m = str(m).rsplit("\\", 1)[-1]
                            model_name = m

                    # n_decoded for progress display
                    next_token = slot.get("next_token")
                    n_decoded = 0
                    if isinstance(next_token, list) and next_token:
                        n_decoded = next_token[0].get("n_decoded", 0)
                    elif isinstance(next_token, dict):
                        n_decoded = next_token.get("n_decoded", 0)
                    if not n_decoded:
                        n_decoded = slot.get("n_decoded", 0)

                    # n_predict (max tokens) from params sub-object
                    params = slot.get("params", {})
                    n_predict = (
                        slot.get("n_predict")
                        or params.get("n_predict")
                        or 0
                    )

                    if is_busy:
                        active_n_decoded += n_decoded
                        total_n_predict += n_predict

                # Build live status result
                kv_usage = None
                if has_kv_data and total_slots > 0:
                    kv_usage = round(kv_usage_sum / total_slots, 3)

                gen_progress = None
                if any_processing and total_n_predict > 0 and active_n_decoded > 0:
                    gen_progress = round(min(1.0, active_n_decoded / total_n_predict), 3)

                return {
                    "status": "generating" if any_processing else "idle",
                    "model": model_name,
                    "slot": "processing" if any_processing else "idle",
                    "kv_usage": kv_usage,
                    "kv_tok": kv_tok_sum if kv_tok_sum > 0 else None,
                    "total_slots": total_slots,
                    "busy_slots": busy_slots,
                    "queue": busy_slots,
                    "speed_gen": None,  # Speed comes from /metrics now
                    "speed_pp": None,
                    "tok_pred": active_n_decoded if active_n_decoded > 0 else None,
                    "tok_eval": None,
                    "gen_progress": gen_progress,
                    "n_predict": total_n_predict if total_n_predict > 0 else None,
                    "active_tokens": active_n_decoded if active_n_decoded > 0 else None,
                }

        except Exception as e:
            logger.warning(f"Failed to get slots: {e}")
            return {}

    async def _get_metrics(self, session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
        """Get Prometheus metrics: speed gauges + cumulative token counters.

        Returns dict with:
          speed_gen_gauge: float — current generation speed (tok/s)
          speed_pp_gauge:  float — current prompt processing speed (tok/s)
          predicted_total: float — cumulative tokens predicted (counter)
          evaluated_total: float — cumulative tokens evaluated/prompt (counter)
        """
        try:
            async with session.get(f"{self.base_url}/metrics") as resp:
                if resp.status != 200:
                    return None

                text = await resp.text()
                result = {}

                # ── Speed gauges (instantaneous, for live display) ──
                speed_gen_gauge = (
                    self._parse_prom_metric(text, "llamacpp:predicted_tokens_seconds")
                    or self._parse_prom_metric(text, "llama_predicted_tokens_seconds")
                )
                speed_pp_gauge = (
                    self._parse_prom_metric(text, "llamacpp:prompt_tokens_seconds")
                    or self._parse_prom_metric(text, "llama_prompt_tokens_seconds")
                )

                if speed_gen_gauge and speed_gen_gauge > 0:
                    result["speed_gen_gauge"] = round(speed_gen_gauge, 1)
                if speed_pp_gauge and speed_pp_gauge > 0:
                    result["speed_pp_gauge"] = round(speed_pp_gauge, 1)

                # ── Cumulative counters (for run detection) ──
                # llama.cpp exposes these as monotonically increasing counters
                predicted_total = (
                    self._parse_prom_metric(text, "llamacpp:tokens_predicted_total")
                    or self._parse_prom_metric(text, "llama_tokens_predicted_total")
                    or self._parse_prom_metric(text, "llamacpp:predicted_tokens_total")
                    or self._parse_prom_metric(text, "llama_predicted_tokens_total")
                )
                evaluated_total = (
                    self._parse_prom_metric(text, "llamacpp:tokens_evaluated_total")
                    or self._parse_prom_metric(text, "llama_tokens_evaluated_total")
                    or self._parse_prom_metric(text, "llamacpp:prompt_tokens_total")
                    or self._parse_prom_metric(text, "llama_prompt_tokens_total")
                )

                if predicted_total is not None:
                    result["predicted_total"] = predicted_total
                if evaluated_total is not None:
                    result["evaluated_total"] = evaluated_total

                return result if result else None

        except Exception as e:
            logger.debug(f"Failed to get metrics: {e}")
            return None

    @staticmethod
    def _parse_prom_metric(text: str, name: str) -> Optional[float]:
        """Parse a single Prometheus metric value from text."""
        pattern = rf'^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([\d.eE+-]+)'
        for line in text.split('\n'):
            m = re.match(pattern, line.strip())
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        return None

    async def _get_models(self, session: aiohttp.ClientSession) -> List[str]:
        """Get loaded models from /v1/models endpoint."""
        try:
            async with session.get(f"{self.base_url}/v1/models") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                models = data.get("data", [])
                model_ids = []
                for model in models:
                    if isinstance(model, dict):
                        model_ids.append(model.get("id"))
                    elif isinstance(model, str):
                        model_ids.append(model)
                return [m for m in model_ids if m]
        except Exception as e:
            logger.debug(f"Failed to get models: {e}")
            return []
