"""Hardware-free capture regressions; no microphone or user audio is accessed."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import recorder as rec


class RecorderCaptureTests(TestCase):
    def test_mac_prefers_native_rate_windows_retains_product_rate(self):
        for platform, expected in (("darwin", 48000), ("win32", rec.SR)):
            with mock.patch.object(rec, "sys", SimpleNamespace(platform=platform)):
                rates = rec._candidate_sample_rates({"default_samplerate": 48000})
                self.assertEqual(rates[0], expected)
                self.assertEqual(len(rates), len(set(rates)))

    def test_native_rate_audio_has_valid_vad_frame_length(self):
        for rate in (44100, 48000, 16000):
            samples = np.full(rate // 50, 1234, dtype=np.int16)
            converted = rec._resample_frame(samples.tobytes(), len(samples), rate)
            self.assertEqual(len(converted), rec.FRAME_LEN * 2)
            self.assertTrue(np.all(np.frombuffer(converted, dtype=np.int16) == 1234))

    def test_live_device_selection_does_not_probe_formats(self):
        device = {"name": "Mac microphone", "max_input_channels": 1}
        with mock.patch.object(rec.sd, "query_devices", return_value=[device]), \
                mock.patch.object(rec.sd, "default", SimpleNamespace(device=(0, -1))), \
                mock.patch.object(rec, "_load_preferred", return_value={"mode": "auto"}), \
                mock.patch.object(rec, "_device_supports_capture", side_effect=AssertionError("live format probe")):
            self.assertEqual(rec.find_device(probe_formats=False)[0], 0)

    def exercise_session(self, active):
        stream = mock.Mock(active=active)
        main_thread = threading.get_ident()
        blocks = max(rec.MIN_SEG_FRAMES + 5, 40)
        vad_threads = []

        def speech(*args):
            vad_threads.append(threading.get_ident())
            return True

        def open_stream(index, callback):
            def capture():
                audio = np.full((960, 1), 1500, dtype=np.int16)
                for _ in range(blocks):
                    callback(audio, 960, None, None, 48000)
                # VAD and filesystem work must not execute on this thread.
            worker = threading.Thread(target=capture)
            worker.start()
            worker.join()
            self.assertEqual(vad_threads, [])
            return stream, 48000

        with mock.patch.object(rec, "_open_input_stream", side_effect=open_stream), \
                mock.patch.object(rec, "is_paused", return_value=False), \
                mock.patch.object(rec, "prevent_sleep"), \
                mock.patch.object(rec, "_write_state"), \
                mock.patch.object(rec, "write_recorder_status"), \
                mock.patch.object(rec, "day_dir", return_value=Path("unused")), \
                mock.patch.object(rec, "write_wav") as write, \
                mock.patch.object(rec, "VAD", SimpleNamespace(is_speech=speech)):
            if active:
                rec.run_once(0, "test microphone", duration=0)
            else:
                with self.assertRaisesRegex(RuntimeError, "音频流已停止"):
                    rec.run_once(0, "test microphone", duration=0)
            write.assert_called_once()
            self.assertEqual(len(write.call_args.args[1]), blocks)
        self.assertEqual(vad_threads, [main_thread] * blocks)
        stream.stop.assert_called_once()
        stream.close.assert_called_once()

    def test_speech_processed_outside_callback_and_drained_on_stop(self):
        self.exercise_session(active=True)

    def test_stopped_stream_reports_error_and_preserves_pending_speech(self):
        self.exercise_session(active=False)
