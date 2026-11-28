import io
import logging
import unittest

from slam_backend import LoopClosingLogFilter


class LoopClosingLoggingTests(unittest.TestCase):
    def test_idle_polls_do_not_hide_failures_or_loop_events(self):
        output = io.StringIO()
        logger = logging.Logger("loop_closing_logger")
        logger.addHandler(logging.StreamHandler(output))
        logger.addFilter(LoopClosingLogFilter())
        logger.info("LoopClosing: waiting for loop-detection output...")
        logger.info("LoopClosing: No loop candidates detected")
        logger.info("LoopClosing: loop closing thread elapsed time: %s", 0.01)
        logger.info("LoopClosing: EXCEPTION: stale keyframe")
        logger.info("LoopClosing: correcting loop")
        logger.warning("LoopClosing: No loop candidates detected")
        self.assertEqual(output.getvalue().splitlines(), [
            "LoopClosing: EXCEPTION: stale keyframe",
            "LoopClosing: correcting loop",
            "LoopClosing: No loop candidates detected",
        ])
