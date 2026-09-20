"""Safety regressions; all command execution is replaced by unittest mocks."""
import contextlib
import io
import signal
import subprocess
import sys
import unittest
from unittest import mock

import ipmi_fan_control as controller


ROWS = [
    'Inlet Temp | 23.000 | degrees C | ok',
    'Exhaust Temp | 34.000 | degrees C | ok',
    'Temp | 42.000 | degrees C | ok',
    'Temp | 45.000 | degrees C | ok',
] + [f'Fan{i} RPM | 8000.000 | RPM | ok' for i in range(1, 7)] + [
    'Fan Redundancy | 0x1 | discrete | 0x0180',
]


class FanControlTests(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(controller.subprocess, 'run',
                                  side_effect=AssertionError('Unexpected real command'))
        self.command = guard.start()
        self.addCleanup(guard.stop)

    def read_sensors(self, rows, returncode=0):
        self.command.side_effect = None
        self.command.return_value = subprocess.CompletedProcess(
            ['ipmitool'], returncode, '\n'.join(rows), '')
        return controller.read_all_sensors()

    def test_both_duplicate_cpu_rows_survive(self):
        inlet, exhaust, cpu1, cpu2 = self.read_sensors(ROWS)
        self.assertEqual((inlet, exhaust), (23, 34))
        self.assertEqual(sorted((cpu1, cpu2)), [42, 45])

    def test_missing_cpu_is_not_treated_as_cold(self):
        with self.assertRaises(controller.SensorError):
            self.read_sensors(ROWS[:2] + ROWS[3:])

    def test_invalid_temperature_is_not_treated_as_cold(self):
        rows = ROWS.copy()
        rows[0] = rows[0].replace('23.000', 'na')
        with self.assertRaises(controller.SensorError):
            self.read_sensors(rows)

    def test_failed_ipmi_response_is_not_used(self):
        with self.assertRaises(controller.SensorError):
            self.read_sensors(ROWS, returncode=1)

    def test_ipmi_timeout_is_a_sensor_failure(self):
        self.command.side_effect = subprocess.TimeoutExpired(['ipmitool'], 20)
        with self.assertRaises(controller.SensorError):
            controller.read_all_sensors()

    def test_missing_fan_is_not_hidden_by_redundancy_row(self):
        with self.assertRaises(controller.SensorError):
            self.read_sensors(ROWS[:4] + ROWS[5:])

    def test_non_ok_fan_status_is_rejected(self):
        rows = ROWS.copy()
        rows[4] = rows[4].replace('ok', 'cr')
        with self.assertRaises(controller.SensorError):
            self.read_sensors(rows)

    def test_stalled_fan_is_rejected_even_with_ok_status(self):
        rows = ROWS.copy()
        rows[4] = rows[4].replace('8000.000', '0.000')
        with self.assertRaises(controller.SensorError):
            self.read_sensors(rows)

    def test_ata_uses_normalized_celsius_not_packed_raw_value(self):
        payload = {'temperature': {'current': 35}, 'ata_smart_attributes': {
            'table': [{'name': 'Temperature_Celsius', 'raw': {'value': 655360035}}]}}
        self.assertEqual(controller.extract_drive_temp(payload, 'hdd'), 35)
        del payload['temperature']
        self.assertIsNone(controller.extract_drive_temp(payload, 'hdd'))

    def test_nvme_uses_hottest_valid_normalized_sensor(self):
        payload = {'temperature': {'current': 31}, 'nvme_smart_health_information_log': {
            'temperature': 36, 'temperature_sensors': [40, 55, 0, float('nan'), 9000]}}
        self.assertEqual(controller.extract_drive_temp(payload, 'nvme'), 55)

    def test_nonfinite_normalized_temperature_is_unavailable(self):
        self.assertIsNone(controller.extract_drive_temp(
            {'temperature': {'current': float('nan')}}, 'ssd'))

    def test_stop_does_not_hide_failed_automatic_restore(self):
        handlers = {}
        def interrupted_sample():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        with (
            mock.patch.object(sys, 'argv', ['fan-control', '--control']),
            mock.patch.object(controller.signal, 'signal', side_effect=handlers.__setitem__),
            mock.patch.object(controller, 'sample_drive_temperatures', side_effect=interrupted_sample),
            mock.patch.object(controller, 'fan_restore_auto', side_effect=OSError('BMC unavailable')),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(controller.main(), 1)


if __name__ == '__main__':
    unittest.main()
