"""Safety regressions; all command execution is replaced by unittest mocks."""
import contextlib
import io
import json
import signal
import subprocess
import sys
import socket
import unittest
import tempfile
from pathlib import Path
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
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(sys, 'argv', ['fan-control', '--control',
                                           '--socket', directory + '/control.sock',
                                           '--state-file', directory + '/profile',
                                           '--interval-file', directory + '/interval']),
            mock.patch.object(controller.signal, 'signal', side_effect=handlers.__setitem__),
            mock.patch.object(controller, 'sample_drive_temperatures', side_effect=interrupted_sample),
            mock.patch.object(controller, 'fan_restore_auto', side_effect=OSError('BMC unavailable')),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(controller.main(), 1)



class DriveDiscoveryTests(unittest.TestCase):
    def test_rediscovery_samples_current_hotplug_inventory_without_class_minimums(self):
        inventories = iter([
            [{'name': 'sda', 'type': 'disk', 'rota': True},
             {'name': 'loop0', 'type': 'loop', 'rota': False}],
            [{'name': 'sda', 'type': 'disk', 'rota': True},
             {'name': 'sdk', 'type': 'disk', 'rota': False},
             {'name': 'nvme0n1', 'type': 'disk', 'rota': False}],
            [{'name': 'nvme0n1', 'type': 'disk', 'rota': False}],
            [],
        ])
        temperatures = {'sda': 40, 'sdk': 45, 'nvme0n1': 55}

        def command(argv, **kwargs):
            if argv[0] == 'lsblk':
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({'blockdevices': next(inventories)}), '')
            if argv[0] == '/usr/sbin/smartctl':
                temp = temperatures[Path(argv[-1]).name]
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({'temperature': {'current': temp}}), '')
            raise AssertionError(f'Unexpected command: {argv}')

        with (
            mock.patch.object(controller.subprocess, 'run', side_effect=command),
            mock.patch.object(controller, 'stable_byid_path', side_effect=lambda path: path),
        ):
            summaries = [controller.sample_drive_temperatures() for _ in range(4)]

        self.assertEqual([summary['max_by_profile'] for summary in summaries], [
            {'hdd': 40},
            {'hdd': 40, 'ssd': 45, 'nvme': 55},
            {'nvme': 55},
            {},
        ])
        self.assertEqual([summary['count'] for summary in summaries], [1, 3, 1, 0])
        self.assertEqual(controller.compute_drive_fan_target(summaries[-1], 'balanced'), 45)

    def test_newly_discovered_disk_without_temperature_rejects_sample(self):
        devices = [{'name': 'sda', 'type': 'disk', 'rota': True},
                   {'name': 'nvme0n1', 'type': 'disk', 'rota': False}]

        def command(argv, **kwargs):
            if argv[0] == 'lsblk':
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({'blockdevices': devices}), '')
            if argv[0] == '/usr/sbin/smartctl':
                payload = {'temperature': {'current': 40}} if argv[-1] == '/dev/sda' else {}
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')
            raise AssertionError(f'Unexpected command: {argv}')

        with (
            mock.patch.object(controller.subprocess, 'run', side_effect=command),
            mock.patch.object(controller, 'stable_byid_path', side_effect=lambda path: path),
        ):
            with self.assertRaisesRegex(controller.SensorError, 'nvme0n1 temperature unavailable'):
                controller.sample_drive_temperatures()

    def test_invalid_discovery_is_not_mistaken_for_an_empty_server(self):
        for payload in ({}, {'blockdevices': None}, []):
            with self.subTest(payload=payload), mock.patch.object(
                controller.subprocess, 'run',
                return_value=subprocess.CompletedProcess(['lsblk'], 0, json.dumps(payload), ''),
            ):
                with self.assertRaises(controller.SensorError):
                    controller.discover_drive_devices()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        self.directory = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.directory / 'profile'
        self.interval_state = self.directory / 'interval'
        self.endpoint = str(self.directory / 'control.sock')
        self.automatic = True
        self.pwm_writes = []
        self.resources.enter_context(mock.patch.object(
            controller.subprocess, 'run', side_effect=self.fake_ipmi))
        self.resources.enter_context(mock.patch.object(controller.signal, 'signal'))
        self.resources.enter_context(mock.patch.object(controller, 'notify_watchdog'))
        self.resources.enter_context(mock.patch.object(controller, 'read_all_sensors',
                                                       return_value=(23, 34, 42, 45)))
        self.resources.enter_context(mock.patch.object(controller, 'sample_drive_temperatures',
            return_value=controller.summarize_drive_temperatures([
                {'name': 'sda', 'profile': 'hdd', 'temp': 30, 'status': 'ok'},
                {'name': 'sdk', 'profile': 'ssd', 'temp': 35, 'status': 'ok'},
                {'name': 'nvme0n1', 'profile': 'nvme', 'temp': 36, 'status': 'ok'},
            ])))
        self.resources.enter_context(mock.patch.object(sys, 'argv', [
            'fan-control', '--control', '--socket', self.endpoint,
            '--state-file', str(self.state), '--interval-file', str(self.interval_state)]))
        self.resources.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.resources.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def fake_ipmi(self, command, **kwargs):
        prefix = controller.IPMI_CMD + ['raw', '0x30', '0x30']
        if command == prefix + ['0x01', '0x00']:
            self.automatic = False
        elif command == prefix + ['0x01', '0x01']:
            self.automatic = True
        elif command[:-1] == prefix + ['0x02', '0xff']:
            self.pwm_writes.append(int(command[-1], 16))
        else:
            raise AssertionError(f'Unexpected command: {command}')
        return subprocess.CompletedProcess(command, 0, '', '')

    def test_profiles_request_distinct_duties_for_the_same_cpu_load(self):
        for profile, expected in {'silent': 10, 'quiet': 50, 'balanced': 60,
                                  'performance': 82.5, 'full-speed': 100}.items():
            with self.subTest(profile=profile):
                self.assertEqual(controller.compute_fan_target(23, 60, 34, profile), expected)

    def test_silent_cpu_ramp_crosses_the_legacy_handoff(self):
        self.assertAlmostEqual(controller.compute_fan_target(23, 70, 34, 'silent'), 160 / 3)

    def test_extreme_profiles_do_not_bypass_air_or_disk_handoff(self):
        for profile, cpu_limit in (('silent', 75), ('full-speed', 70)):
            with self.subTest(profile=profile):
                for inlet, cpu, exhaust in ((32, 45, 34), (23, 45, 50), (23, cpu_limit, 34)):
                    with self.subTest(inlet=inlet, cpu=cpu, exhaust=exhaust):
                        with self.assertRaises(controller.SensorError):
                            controller.compute_fan_target(inlet, cpu, exhaust, profile)
                with self.assertRaises(controller.SensorError):
                    controller.compute_drive_fan_target({'max_by_profile': {'hdd': 45}}, profile)

    def test_failed_state_replacement_preserves_previous_selection(self):
        controller.save_setting(str(self.state), 'balanced')
        with mock.patch.object(controller.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                controller.save_setting(str(self.state), 'silent')
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'balanced')

    def test_corrupt_saved_profile_prevents_manual_control(self):
        self.state.write_text('unknown\n')
        self.assertEqual(controller.main(), 1)
        self.assertTrue(self.automatic)
        self.assertEqual(self.pwm_writes, [])
        self.assertEqual(self.state.read_text(), 'unknown\n')

    def test_failed_persistence_restores_automatic_control(self):
        with mock.patch.object(controller, 'save_setting', side_effect=OSError('disk full')):
            self.assertEqual(controller.main(), 1)
        self.assertTrue(self.automatic)
        self.assertFalse(self.state.exists())

    def test_repeated_stop_signals_do_not_interrupt_automatic_restoration(self):
        handlers = {}

        def stopping_ipmi(command, **kwargs):
            if command == controller.IPMI_CMD + ['raw', '0x30', '0x30', '0x01', '0x01']:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    handler = handlers[signum]
                    if handler == signal.SIG_DFL:
                        raise SystemExit(128 + signum)
                    if handler != signal.SIG_IGN:
                        handler(signum, None)
            return self.fake_ipmi(command, **kwargs)

        with (
            mock.patch.object(controller.signal, 'signal', side_effect=handlers.__setitem__),
            mock.patch.object(controller.subprocess, 'run', side_effect=stopping_ipmi),
            mock.patch.object(controller, 'wait_for_control', side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertTrue(self.automatic)
        self.assertEqual(self.pwm_writes, [45])


    def test_second_controller_does_not_restore_underneath_socket_owner(self):
        self.automatic = False
        with controller.control_socket(self.endpoint):
            self.assertEqual(controller.main(), 1)
            self.assertFalse(self.automatic)
            self.assertEqual(self.pwm_writes, [])

    def test_overrunning_cycles_still_process_queued_profile_commands(self):
        now = [100]
        samples = [0]
        peer = self.resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET))

        def slow_sensors():
            samples[0] += 1
            if samples[0] > 2:
                raise KeyboardInterrupt
            now[0] += controller.DEFAULT_INTERVAL + 1
            if samples[0] == 1:
                peer.connect(self.endpoint)
                peer.sendall(b'{"command":"set","profile":"performance"}')
            return 23, 34, 42, 45

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'read_all_sensors', side_effect=slow_sensors),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertEqual(self.pwm_writes, [45, 65])
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'performance')

    def test_repeated_switches_cannot_accelerate_downward_ramping(self):
        now = [100]
        actions = iter([(100.2, 'full-speed'), (100.4, 'silent'), (100.6, 'quiet'),
                        (103, 'balanced'), (103.2, 'silent')])

        def switch(listener, timeout, status):
            try:
                now[0], profile = next(actions)
            except StopIteration:
                raise KeyboardInterrupt
            connection, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.resources.enter_context(peer)
            return connection, 'profile', profile

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'wait_for_control', side_effect=switch),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertEqual(self.pwm_writes, [45, 100, 80])
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'silent')
        self.assertTrue(self.automatic)

    def test_switch_finishes_at_target_then_restores_normal_ramping(self):
        now = [100]
        cpu = [45]
        actions = iter([(101, 'silent', 63.1), (103, None, 63.1), (106, None, 63.1),
                        (109, None, 63), (112, None, 60)])

        def advance(listener, timeout, status):
            try:
                now[0], profile, cpu[0] = next(actions)
            except StopIteration:
                raise KeyboardInterrupt
            if profile is None:
                return None
            connection, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.resources.enter_context(peer)
            return connection, 'profile', profile

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'read_all_sensors',
                              side_effect=lambda: (23, 34, 42, cpu[0])),
            mock.patch.object(controller, 'wait_for_control', side_effect=advance),
        ):
            self.assertEqual(controller.main(), 0)
        # The final one-point switch step must not stick in normal hysteresis.
        # Normal hysteresis then holds 24 above a target of 23; a larger drop reaches 10.
        self.assertEqual(self.pwm_writes, [45, 25, 24, 10])
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'silent')
        self.assertTrue(self.automatic)

    def test_sensor_failure_during_transition_restores_automatic_control(self):
        now = [100]
        actions = iter([(101, 'silent'), (103, None), (106, None)])

        def advance(listener, timeout, status):
            now[0], profile = next(actions)
            if profile is None:
                return None
            connection, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.resources.enter_context(peer)
            return connection, 'profile', profile

        def sensors():
            if now[0] >= 106:
                raise controller.SensorError('Required fan became unhealthy')
            return 23, 34, 42, 45

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'read_all_sensors', side_effect=sensors),
            mock.patch.object(controller, 'wait_for_control', side_effect=advance),
        ):
            self.assertEqual(controller.main(), 1)
        self.assertEqual(self.pwm_writes, [45, 25])
        self.assertTrue(self.automatic)

    def test_restart_loads_saved_profile_and_explicit_override_replaces_it(self):
        controller.save_setting(str(self.state), 'performance')
        with mock.patch.object(controller, 'wait_for_control', side_effect=KeyboardInterrupt):
            self.assertEqual(controller.main(), 0)
            with mock.patch.object(sys, 'argv', sys.argv + ['--profile', 'balanced']):
                self.assertEqual(controller.main(), 0)
        self.assertEqual(self.pwm_writes, [65, 45])
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'balanced')
        self.assertTrue(self.automatic)

    def test_read_only_profile_override_does_not_change_saved_selection(self):
        controller.save_setting(str(self.state), 'performance')
        controller.save_setting(str(self.interval_state), 10)
        with mock.patch.object(sys, 'argv', ['fan-control', '--check', '--profile', 'silent',
                                           '--state-file', str(self.state), '--interval', '0.5',
                                           '--interval-file', str(self.interval_state)]):
            self.assertEqual(controller.main(), 0)
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'performance')
        self.assertEqual(controller.read_setting(str(self.interval_state), controller.interval_value), 10)
        self.assertTrue(self.automatic)
        self.assertEqual(self.pwm_writes, [])

    def test_empty_drive_inventory_still_uses_air_and_cpu_sensors(self):
        output = io.StringIO()
        with (
            mock.patch.object(controller, 'sample_drive_temperatures',
                              return_value=controller.summarize_drive_temperatures([])),
            mock.patch.object(sys, 'argv', ['fan-control', '--check',
                                          '--state-file', str(self.state),
                                          '--interval-file', str(self.interval_state)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(controller.main(), 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status['drives']['count'], 0)
        self.assertEqual(status['profile_target_pwm'], 45)
        self.assertEqual(status['cpu'], [42, 45])
        self.assertEqual(self.pwm_writes, [])

    def test_hot_added_drive_is_reported_without_interrupting_control(self):
        def summary(*names):
            return controller.summarize_drive_temperatures([
                {'name': name, 'profile': 'hdd' if name == 'sda' else 'nvme',
                 'temp': 30, 'status': 'ok'} for name in names
            ])

        now = [100]

        def advance(listener, timeout, status):
            now[0] += controller.DRIVE_SAMPLE_SECONDS
            if now[0] > 160:
                raise KeyboardInterrupt

        errors = io.StringIO()
        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'sample_drive_temperatures',
                              side_effect=[summary('sda'), summary('sda', 'nvme0n1')]),
            mock.patch.object(controller, 'wait_for_control', side_effect=advance),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertIn('nvme0n1', errors.getvalue())
        self.assertIn('added', errors.getvalue())
        self.assertTrue(self.automatic)

    def test_previously_sampled_drive_disappearing_restores_automatic_control(self):
        def summary(*names):
            return controller.summarize_drive_temperatures([
                {'name': name, 'profile': 'hdd' if name == 'sda' else 'nvme',
                 'temp': 30, 'status': 'ok'} for name in names
            ])

        for remaining in (('nvme0n1',), ()):
            with self.subTest(remaining=remaining):
                now = [100]
                self.pwm_writes.clear()
                errors = io.StringIO()

                def advance(listener, timeout, status):
                    now[0] += controller.DRIVE_SAMPLE_SECONDS

                with (
                    mock.patch.object(controller.time, 'monotonic',
                                      side_effect=lambda: now[0]),
                    mock.patch.object(controller, 'sample_drive_temperatures',
                                      side_effect=[summary('sda', 'nvme0n1'),
                                                   summary(*remaining)]),
                    mock.patch.object(controller, 'wait_for_control', side_effect=advance),
                    contextlib.redirect_stderr(errors),
                ):
                    self.assertEqual(controller.main(), 1)
                self.assertEqual(self.pwm_writes, [45])
                self.assertTrue(self.automatic)
                self.assertIn('sda', errors.getvalue())


    def test_normal_cooling_uses_every_cycle_despite_sensor_latency(self):
        now = [100]
        temperatures = iter([69, 45, 45])
        sample_starts = []

        def sensors():
            sample_starts.append(now[0])
            now[0] += 1
            return 23, 34, 42, next(temperatures)

        def advance(listener, timeout, status):
            if len(sample_starts) == 3:
                raise KeyboardInterrupt
            now[0] += timeout

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'read_all_sensors', side_effect=sensors),
            mock.patch.object(controller, 'wait_for_control', side_effect=advance),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertEqual(sample_starts, [100, 103, 106])
        self.assertEqual(self.pwm_writes, [74, 54, 45])
        self.assertTrue(self.automatic)

    def test_interval_changes_reschedule_without_extra_downward_steps(self):
        now = [100]
        actions = iter([
            (100.1, 'profile', 'full-speed'), (100.2, 'profile', 'silent'),
            (100.25, 'interval', 0.5), (100.5, None, None),
            (100.6, 'interval', 10), (100.7, 'interval', 0.5), (101, None, None),
        ])

        def advance(listener, timeout, status):
            try:
                now[0], key, value = next(actions)
            except StopIteration:
                raise KeyboardInterrupt
            if key is None:
                return None
            connection, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.resources.enter_context(peer)
            return connection, key, value

        with (
            mock.patch.object(controller.time, 'monotonic', side_effect=lambda: now[0]),
            mock.patch.object(controller, 'wait_for_control', side_effect=advance),
        ):
            self.assertEqual(controller.main(), 0)
        self.assertEqual(self.pwm_writes, [45, 100, 80, 60])
        self.assertEqual(controller.read_setting(str(self.interval_state), controller.interval_value), 0.5)
        self.assertEqual(controller.read_setting(str(self.state), controller.profile_value), 'silent')

    def test_saved_interval_and_override_survive_restarts(self):
        controller.save_setting(str(self.interval_state), 10)
        waits = []

        def stop_after_cycle(listener, timeout, status):
            waits.append(timeout)
            raise KeyboardInterrupt

        with (
            mock.patch.object(controller.time, 'monotonic', return_value=100),
            mock.patch.object(controller, 'wait_for_control', side_effect=stop_after_cycle),
        ):
            self.assertEqual(controller.main(), 0)
            with mock.patch.object(sys, 'argv', sys.argv + ['--interval', '0.5']):
                self.assertEqual(controller.main(), 0)
            self.assertEqual(controller.main(), 0)
        self.assertEqual(waits, [10, 0.5, 0.5])
        self.assertEqual(controller.read_setting(str(self.interval_state), controller.interval_value), 0.5)

    def test_invalid_interval_commands_leave_the_controller_available(self):
        status = {'profile': 'balanced', 'interval': 3}
        with controller.control_socket(self.endpoint) as listener:
            for value in (0.49, 10.01, float('nan'), 10 ** 400, True, None):
                with self.subTest(value=value), socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
                    peer.settimeout(1)
                    peer.connect(self.endpoint)
                    peer.sendall(json.dumps({'command': 'set-interval', 'interval': value}).encode())
                    request = controller.wait_for_control(listener, 0, status)
                    if request is not None:
                        request[0].close()
                    self.assertIsNone(request)
                    self.assertIn('error', json.loads(peer.recv(4096)))
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
                peer.settimeout(1)
                peer.connect(self.endpoint)
                peer.sendall(b'{"command":"get"}')
                self.assertIsNone(controller.wait_for_control(listener, 0, status))
                self.assertEqual(json.loads(peer.recv(4096))['interval'], 3)

    def test_corrupt_saved_interval_prevents_manual_control(self):
        self.interval_state.write_text('NaN\n')
        self.assertEqual(controller.main(), 1)
        self.assertEqual(self.pwm_writes, [])
        self.assertTrue(self.automatic)

    def test_interval_save_failure_restores_auto_and_preserves_previous_value(self):
        controller.save_setting(str(self.state), 'balanced')
        controller.save_setting(str(self.interval_state), 10)
        with (
            mock.patch.object(sys, 'argv', sys.argv + ['--interval', '3']),
            mock.patch.object(controller.os, 'replace', side_effect=OSError('disk full')),
            mock.patch.object(controller, 'wait_for_control', side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(controller.main(), 1)
        self.assertEqual(controller.read_setting(str(self.interval_state), controller.interval_value), 10)
        self.assertTrue(self.automatic)


if __name__ == '__main__':
    unittest.main()
