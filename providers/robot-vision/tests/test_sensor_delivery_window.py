import unittest
from sensor_delivery import RecentReadings

class DeliveryWindowTests(unittest.TestCase):
    def test_slow_consumer_keeps_only_readings_inside_its_window(self):
        now=[1.0]
        fast=RecentReadings(0.2,lambda:now[0])
        slow=RecentReadings(0.2,lambda:now[0])
        for when in (1.0,1.15,1.3):
            now[0]=when
            for consumer in (fast,slow):consumer.put(when,when)
            self.assertEqual(fast.get(),(when,when))
        slow.put(0.2,'already old at admission')
        self.assertEqual(slow.get(),(1.15,1.15))
        self.assertEqual(slow.get(),(1.3,1.3))
        self.assertEqual(slow.expired,2)
        slow.close()
        self.assertIsNone(slow.get())

    def test_clock_uncertainty_cannot_admit_a_reading_as_fresh(self):
        from types import SimpleNamespace
        from sensor_push_process import _QueueSink
        readings = RecentReadings(.2, lambda: 10.)
        sink = SimpleNamespace(readings=readings)
        _QueueSink.publish(sink, {}, 10., 10., capture_interval=[9., 10.])
        self.assertEqual(readings.expired, 1)
        _QueueSink.publish(sink, {}, 10., 10., capture_interval=[9.95, 10.])
        self.assertEqual(readings.get()[0], 9.95)
