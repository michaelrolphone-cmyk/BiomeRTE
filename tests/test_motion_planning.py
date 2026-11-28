import copy
import unittest
from biomerte.motion_model import MotionModel
from biomerte.motion_planning import plan_motion


def calibrated(latched=False):
    model=MotionModel()
    model.state["steering_known"]=True
    for regression in (model.angular,model.metric):
        regression.count=100
        regression.domains={str(mask):100 for mask in (1,2,5,6,9,10)}
    model.metric.weights[1]=.2
    model.angular.weights[2]=30.
    if latched:
        model.angular.weights[2]=10.
        model.angular.weights[17]=20.
    return model


class InverseTests(unittest.TestCase):
    def test_roundtrip_turn_and_reverse(self):
        model=calibrated()
        before=copy.deepcopy(model.export())
        for distance,yaw in ((.2,30.),(-.2,-30.)):
            result=plan_motion(model,dict(distance_m=distance,yaw_deg=yaw))
            self.assertTrue(result["within_tolerance"],result)
            again=model.predict(result["pulses"],step_s=.1)
            self.assertEqual(again,result["prediction"])
            self.assertFalse(result["executes_motion"])
        self.assertEqual(before,model.export())

    def test_retained_steering_changes_corrective_plan(self):
        model=calibrated(latched=True)
        centered=plan_motion(model,dict(distance_m=.2,yaw_deg=0.))
        model.state.update(steering_latch=.9,steering_memory=[.9]*4)
        turned=plan_motion(model,dict(distance_m=.2,yaw_deg=0.))
        self.assertNotEqual(centered["pulses"],turned["pulses"])
        self.assertTrue(any(p["mask"] in (9,10) for p in turned["pulses"]),turned)

    def test_untrained_and_unsupported_do_not_make_up_pulses(self):
        for model in (MotionModel(),calibrated()):
            model.metric.domains={}
            result=plan_motion(model,dict(distance_m=.2,yaw_deg=0.))
            self.assertEqual(result["status"],"insufficient_calibration")
            self.assertIsNone(result["pulses"])

    def test_unreachable_request_returns_explicit_residual(self):
        result=plan_motion(calibrated(),dict(distance_m=10.,yaw_deg=90.),
            constraints=dict(max_total_s=.2,max_pulse_s=.2,max_evaluations=50))
        self.assertEqual(result["status"],"best_effort_outside_tolerance")
        self.assertFalse(result["within_tolerance"])
        self.assertLessEqual(sum(p["duration_s"] for p in result["pulses"]),.2+1e-9)
        self.assertLessEqual(result["evaluations"],50)

    def test_state_override_and_invalid_inputs(self):
        model=calibrated();model.state["steering_known"]=False
        result=plan_motion(model,dict(distance_m=.1,yaw_deg=0.),initial_state=dict(steering_known=True))
        self.assertFalse(result["initial_state_uncertain"])
        self.assertFalse(result["prediction"]["initial_state_uncertain"])
        for target,constraints in (({},{}),(dict(distance_m=float('nan'),yaw_deg=0.),{}),
                (dict(distance_m=.1,yaw_deg=0.),dict(max_evaluations=0)),
                (dict(distance_m=.1,yaw_deg=0.),dict(min_pulse_s=2.,max_pulse_s=1.))):
            with self.assertRaises(ValueError):plan_motion(model,target,constraints=constraints)

    def test_reverse_permission_and_supported_domains(self):
        model=calibrated();model.angular.domains={"1":1};model.metric.domains={"1":1}
        result=plan_motion(model,dict(distance_m=-.2,yaw_deg=0.),constraints=dict(allow_reverse=False,max_evaluations=50))
        self.assertFalse(result["within_tolerance"])
        self.assertTrue(all(p["mask"]==1 for p in result["pulses"]))
