import os
import tempfile
import unittest
import wave
from unittest import mock
import main
class WavConversionTests(unittest.TestCase):
    def test_conversion_wait_has_no_wall_clock_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            source=os.path.join(directory,'source.wav')
            dest=os.path.join(directory,'converted.wav')
            for path in (source,dest):
                with wave.open(path,'wb') as out:
                    out.setparams((1,2,22050,0,'NONE','not compressed'))
                    out.writeframes(b'\0\0'*22050)
            class SlowConverter:
                returncode=0
                def wait(self): return 0
            context=main.Transmitter({'root':os.path.join(directory,'jobs'),'audio_root':directory})
            with mock.patch.object(main.subprocess,'Popen',return_value=SlowConverter()):
                context.prepare_audio({'duration_seconds':1,'source':'wav','audio_path':source,'volume':.2},dest)
