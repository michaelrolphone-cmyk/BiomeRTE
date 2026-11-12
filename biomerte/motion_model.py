"""History-dependent, observational rover response model. Standard library only.

The memory traces are latent steering states at several time scales, not
measured wheel angles. They survive neutral pulses. Predictions are estimates,
never commands, and unavailable axes remain null rather than zero.
"""
import copy
import math
import time

TIMES = (.15, .5, 1.5, 5.)
N = 17

def number(v):
    return type(v) in (int, float) and math.isfinite(v)

def wrap(v):
    return (v + 180.) % 360. - 180.

def controls(mask):
    if type(mask) is not int or not 0 <= mask <= 63:
        raise ValueError("mask must be an integer 0..63")
    if mask & 3 == 3 or mask & 12 == 12:
        raise ValueError("opposed drive/steering channels")
    return int(bool(mask & 1))-int(bool(mask & 2)), int(bool(mask & 4))-int(bool(mask & 8))

class Regression:
    def __init__(self, saved=None):
        self.weights = [0.] * N
        self.p = [[100. if i == j else 0. for j in range(N)] for i in range(N)]
        self.count = 0
        self.error2 = 0.
        self.drift_events = 0
        self.last_update = None
        self.domains = {}
        if saved:
            self.__dict__.update(copy.deepcopy(saved))

    def predict(self, x):
        if not self.count:
            return None, None
        y = sum(a*b for a,b in zip(x,self.weights))
        leverage = sum(x[i]*self.p[i][j]*x[j] for i in range(N) for j in range(N))
        return y, math.sqrt(max(0., self.error2)*(1.+max(0.,leverage)))

    def update(self, x, y, dt, domain):
        if not number(y):
            return
        before, unused = self.predict(x)
        residual = y-(before or 0.)
        old = self.error2
        # Forget by elapsed observation time, not frame frequency. A changed
        # response inflates covariance so old power/load conditions can be left.
        forgetting = math.exp(-dt/1800.)
        if self.count > N and residual*residual > 9.*max(old,1e-8):
            self.drift_events += 1
            forgetting = min(forgetting,.90)
        px = [sum(row[j]*x[j] for j in range(N)) for row in self.p]
        denom = forgetting + sum(x[i]*px[i] for i in range(N))
        gain = [v/denom for v in px]
        self.weights = [w+g*residual for w,g in zip(self.weights,gain)]
        self.p = [[(self.p[i][j]-gain[i]*px[j])/forgetting for j in range(N)] for i in range(N)]
        alpha = 1.-math.exp(-dt/30.)
        self.error2 = (1.-alpha)*old+alpha*residual*residual if self.count else residual*residual
        self.count += 1
        self.domains[domain] = self.domains.get(domain,0)+1
        self.last_update = time.time()

    def export(self):
        return copy.deepcopy(self.__dict__)

class MotionModel:
    def __init__(self, saved=None):
        saved = saved or {}
        self.angular = Regression(saved.get("angular"))
        self.metric = Regression(saved.get("metric"))
        self.state = dict(steering_memory=[0.]*4, steering_known=False,
                          speed_m_s=None, yaw_rate_deg_s=None, drive=0,
                          steer=0, on_s=0., idle_s=0., previous_drive=0)
        # Steering memory survives a restart, but motion during the gap is unknown.
        if saved.get("state"):
            self.state.update(copy.deepcopy(saved["state"]))
            self.state.update(speed_m_s=None,yaw_rate_deg_s=None,steering_known=False)
        self.revision = saved.get("revision",0)
        self.observations = 0
        self.rejected = {}
        self.last_observation = None

    @staticmethod
    def advance(state, mask, dt):
        drive, steer = controls(mask)
        old_drive = state["drive"]
        if drive:
            state["on_s"] = state["on_s"]+dt if drive == old_drive else dt
            state["idle_s"] = 0.
        else:
            state["idle_s"] += dt
            state["on_s"] = 0.
        if drive != old_drive:
            state["previous_drive"] = old_drive
        # No instantaneous wheel straightening on release. At rest the traces
        # are held; with drive/coast they approach the requested steering.
        rolling = drive != 0 or abs(state.get("speed_m_s") or 0.) > .01
        if rolling:
            state["steering_memory"] = [
                steer+(z-steer)*math.exp(-dt/tau)
                for z,tau in zip(state["steering_memory"],TIMES)]
        state["drive"],state["steer"] = drive,steer

    @staticmethod
    def features(state, mask, dt):
        drive,steer = controls(mask)
        z = state["steering_memory"]
        v = state.get("speed_m_s") or 0.
        w = state.get("yaw_rate_deg_s") or 0.
        return [1.,float(drive),float(drive*steer),*[drive*a for a in z],
                v,w,v*z[0],v*z[2],math.log1p(state["on_s"]),
                float(state["previous_drive"]),float(drive != state["drive"]),
                math.log1p(state["idle_s"]),dt,float(steer)]

    def reject(self, reason):
        self.rejected[reason] = self.rejected.get(reason,0)+1

    def observe(self, mask, dt, yaw_delta_deg, distance_m=None, evidence=None):
        controls(mask)
        if not number(dt) or dt <= 0 or not number(yaw_delta_deg):
            raise ValueError("finite positive dt and yaw delta required")
        self.observations += 1
        state = self.state
        x = self.features(state,mask,dt)
        domain = str(mask & 15)
        omega = yaw_delta_deg/dt
        self.angular.update(x,omega,dt,domain)
        if number(distance_m):
            self.metric.update(x,distance_m/dt,dt,domain)
        self.advance(state,mask,dt)
        state["yaw_rate_deg_s"] = omega
        if number(distance_m):
            state["speed_m_s"] = distance_m/dt
        else:
            state["speed_m_s"] = None
        # This marks continuity, not a measured steering angle.
        state["steering_known"] = True
        self.revision += 1
        self.last_observation = dict(evidence or {}, dt_s=dt, mask=mask,
                                     yaw_delta_deg=yaw_delta_deg,distance_m=distance_m)

    def predict(self, pulses, initial_state=None, step_s=.05):
        if not isinstance(pulses,list) or not pulses:
            raise ValueError("pulses must be a nonempty list")
        if not number(step_s) or step_s <= 0:
            raise ValueError("step_s must be positive")
        state = copy.deepcopy(self.state)
        if initial_state is not None:
            if not isinstance(initial_state,dict):
                raise ValueError("initial_state must be an object")
            if set(initial_state)-set(state):
                raise ValueError("unknown initial_state fields")
            for k in state:
                if k in initial_state:
                    state[k] = copy.deepcopy(initial_state[k])
            z = state["steering_memory"]
            if not isinstance(z,list) or len(z)!=4 or not all(number(a) and -1 <= a <= 1 for a in z):
                raise ValueError("steering_memory requires four finite values in -1..1")
            for k in ("speed_m_s","yaw_rate_deg_s"):
                if state[k] is not None and not number(state[k]):
                    raise ValueError("invalid initial "+k)
            for k in ("on_s","idle_s"):
                if not number(state[k]) or state[k]<0:
                    raise ValueError("invalid initial "+k)
            for k in ("drive","steer","previous_drive"):
                if type(state[k]) is not int or state[k] not in (-1,0,1):
                    raise ValueError("invalid initial "+k)
        x_pos=y_pos=theta=distance=elapsed=0.
        yaw_error=distance_error=0.
        results=[]
        unsupported=[]
        metric_unsupported=[]
        for index,pulse in enumerate(pulses):
            if not isinstance(pulse,dict) or set(pulse)-{"mask","duration_s"}:
                raise ValueError("each pulse requires mask and duration_s")
            mask=pulse.get("mask")
            controls(mask)
            duration=pulse.get("duration_s")
            if not number(duration) or duration<=0:
                raise ValueError("duration_s must be finite and positive")
            if str(mask & 15) not in self.angular.domains:
                unsupported.append(index)
            if str(mask & 15) not in self.metric.domains:
                metric_unsupported.append(index)
            left=duration
            while left>1e-10:
                dt=min(step_s,left)
                x=self.features(state,mask,dt)
                omega,oe=self.angular.predict(x)
                velocity,ve=self.metric.predict(x)
                # Untrained axes stay unavailable in the returned result.
                if omega is not None:
                    theta += omega*dt
                    yaw_error += oe*dt
                if velocity is not None:
                    ds=velocity*dt
                    distance+=ds
                    x_pos+=ds*math.cos(math.radians(theta))
                    y_pos+=ds*math.sin(math.radians(theta))
                    distance_error+=ve*dt
                self.advance(state,mask,dt)
                state["speed_m_s"]=velocity
                state["yaw_rate_deg_s"]=omega
                elapsed+=dt
                left-=dt
            results.append(dict(index=index,mask=mask,elapsed_s=elapsed,
                yaw_deg=theta if self.angular.count and not unsupported else None,
                distance_m=distance if self.metric.count and not metric_unsupported else None,
                x_m=x_pos if self.metric.count and self.angular.count and not unsupported and not metric_unsupported else None,
                y_m=y_pos if self.metric.count and self.angular.count and not unsupported and not metric_unsupported else None,
                state=copy.deepcopy(state)))
        return dict(schema="rover.motion.prediction/1",model_revision=self.revision,
            qualification="observational_estimate_not_calibrated_probability",
            angular_available=bool(self.angular.count) and not unsupported,metric_available=bool(self.metric.count) and not metric_unsupported,
            initial_state_uncertain=not self.state["steering_known"],
            unsupported_pulse_indices=unsupported,metric_unsupported_pulse_indices=metric_unsupported,steps=results,
            uncertainty_proxy=dict(yaw_deg=yaw_error if self.angular.count else None,
                distance_m=distance_error if self.metric.count else None,
                interpretation="correlated accumulated residual/leverage proxy; not a confidence bound"),
            executes_motion=False)

    def export(self):
        return dict(schema=1,revision=self.revision,state=copy.deepcopy(self.state),
                    angular=self.angular.export(),metric=self.metric.export())

    def status(self):
        return dict(schema="rover.motion.model/1",revision=self.revision,
            state=copy.deepcopy(self.state),angular_samples=self.angular.count,
            metric_samples=self.metric.count,observations=self.observations,
            rejected=dict(self.rejected),last_observation=self.last_observation,
            drift_events=dict(angular=self.angular.drift_events,metric=self.metric.drift_events),
            residual_rms=dict(angular_deg_s=math.sqrt(self.angular.error2),speed_m_s=math.sqrt(self.metric.error2)),
            domains=dict(angular=self.angular.domains,metric=self.metric.domains),
            steering_representation="latent_multi_timescale_memory_not_measured_wheel_angle")
