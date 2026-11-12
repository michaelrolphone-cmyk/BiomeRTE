"""Read-only inverse response estimation using a pinned forward model.

Targets are signed travelled distance (not displacement) and unwrapped heading
change. Search is bounded and approximate; absence of a solution is not a proof
of physical infeasibility. This module does not acquire control or drive.
"""
import copy
from biomerte.motion_model import number


def plan_motion(model, target, initial_state=None, constraints=None):
    """Estimate pulse timings for target {distance_m, yaw_deg}.

    Both axes are required, including zero: turning a wheeled rover consumes
    distance. Constraints describe the search envelope, never an execution lease.
    """
    if not isinstance(target, dict) or set(target) != {"distance_m", "yaw_deg"}:
        raise ValueError("target requires exactly distance_m and yaw_deg")
    if not all(number(v) for v in target.values()):
        raise ValueError("target values must be finite")
    defaults = dict(distance_tolerance_m=.02, yaw_tolerance_deg=2.,
                    min_pulse_s=.05, max_pulse_s=3., max_total_s=10.,
                    max_pulses=4, beam_width=8, max_evaluations=600,
                    step_s=.1, allow_reverse=True)
    if constraints is None:
        constraints = {}
    if not isinstance(constraints, dict) or set(constraints)-set(defaults):
        raise ValueError("unknown planning constraints")
    cfg = dict(defaults, **constraints)
    for key in ("max_pulses", "beam_width", "max_evaluations"):
        if type(cfg[key]) is not int or cfg[key] <= 0:
            raise ValueError(key+" must be a positive integer")
    for key in set(defaults)-{"max_pulses", "beam_width", "max_evaluations", "allow_reverse"}:
        if not number(cfg[key]) or cfg[key] <= 0:
            raise ValueError(key+" must be finite and positive")
    if type(cfg["allow_reverse"]) is not bool:
        raise ValueError("allow_reverse must be Boolean")
    if cfg["min_pulse_s"] > cfg["max_pulse_s"]:
        raise ValueError("min_pulse_s exceeds max_pulse_s")
    # Validate scenario state even if calibration is absent.
    model.predict([dict(mask=1, duration_s=cfg["min_pulse_s"])],
                  initial_state, cfg["step_s"])
    state_known = (initial_state or {}).get("steering_known", model.state["steering_known"])
    result = dict(schema="rover.motion.plan/1", model_revision=model.revision,
                  target=copy.deepcopy(target), constraints=cfg,
                  initial_state_uncertain=not state_known,
                  qualification="observational_estimate_not_calibrated_probability",
                  executes_motion=False, pulses=None, prediction=None,
                  within_tolerance=False, evaluations=0,
                  search="beam_search_then_duration_refinement; no global optimality guarantee",
                  terminal_behavior="pulse horizon only; no guaranteed stop or obstacle clearance")
    masks = [1, 5, 9] + ([2, 6, 10] if cfg["allow_reverse"] else [])
    masks = [m for m in masks if str(m) in model.angular.domains
             and str(m) in model.metric.domains]
    result["supported_masks"] = masks
    if not model.angular.count or not model.metric.count or not masks:
        result.update(status="insufficient_calibration", reason="Both distance and yaw observations are required for supported drive/steer masks")
        return result
    lo, hi = cfg["min_pulse_s"], min(cfg["max_pulse_s"], cfg["max_total_s"])
    durations = sorted(set([lo, hi] + [lo*(hi/lo)**(i/4.) for i in range(1, 4)])) if hi >= lo else []
    best = None
    cache = set()

    def evaluate(pulses):
        nonlocal best
        key = tuple((p["mask"], round(p["duration_s"], 9)) for p in pulses)
        if key in cache or result["evaluations"] >= cfg["max_evaluations"]:
            return None
        cache.add(key)
        if sum(p["duration_s"] for p in pulses) > cfg["max_total_s"]+1e-9:
            return None
        result["evaluations"] += 1
        prediction = model.predict(pulses, initial_state, cfg["step_s"])
        end = prediction["steps"][-1]
        if not prediction["angular_available"] or not prediction["metric_available"]:
            return None
        ds, dy = end["distance_m"]-target["distance_m"], end["yaw_deg"]-target["yaw_deg"]
        if not all(number(v) for v in (ds, dy)):
            return None
        score = (ds/cfg["distance_tolerance_m"])**2 + (dy/cfg["yaw_tolerance_deg"])**2
        node = (score, sum(p["duration_s"] for p in pulses), copy.deepcopy(pulses), prediction, ds, dy)
        if best is None or node[:2] < best[:2]:
            best = node
        return node

    beam = [[]]
    # Reserve part of the caller's evaluation budget for continuous refinement.
    beam_budget = max(1, int(cfg["max_evaluations"]*.7))
    for depth in range(cfg["max_pulses"]):
        nodes = []
        for sequence in beam:
            for mask in masks:
                if sequence and sequence[-1]["mask"] == mask:
                    continue
                for duration in durations:
                    if result["evaluations"] >= beam_budget:
                        break
                    node = evaluate(sequence+[dict(mask=mask, duration_s=duration)])
                    if node is not None:
                        nodes.append(node)
        if not nodes:
            break
        nodes.sort(key=lambda n: n[:2])
        beam = [n[2] for n in nodes[:cfg["beam_width"]]]
        if result["evaluations"] >= beam_budget:
            break
    if best is not None:
        delta = (hi-lo)/2.
        while delta > .0001 and result["evaluations"] < cfg["max_evaluations"]:
            previous = best
            base = copy.deepcopy(best[2])
            for index in range(len(base)):
                for sign in (-1, 1):
                    candidate = copy.deepcopy(base)
                    value = candidate[index]["duration_s"] + sign*delta
                    if lo <= value <= hi:
                        candidate[index]["duration_s"] = value
                        evaluate(candidate)
            if best is previous:
                delta /= 2.
        result.update(pulses=best[2], prediction=best[3],
                      residual=dict(distance_m=best[4], yaw_deg=best[5]))
        result["within_tolerance"] = abs(best[4]) <= cfg["distance_tolerance_m"] and abs(best[5]) <= cfg["yaw_tolerance_deg"]
        result["status"] = "estimate_within_tolerance" if result["within_tolerance"] else "best_effort_outside_tolerance"
    else:
        result.update(status="no_supported_plan", reason="No supported finite prediction within the requested search envelope")
    return result
