"""The real event_lockd message loop, run against a fake messaging module.

Regression for the Nov 2026 crash: Poller.poll() returns fresh SubSocket wrappers, not the objects passed to
sub_sock, so looking sockets up by identity raised KeyError on the first message of every drive.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from openpilot.sunnypilot.phone_relay import event_lock as el


class _Stop(BaseException):
  """Ends the loop from inside the test without being swallowed by `except Exception`."""


class _Msg:
  def __init__(self, name, t, **fields):
    self.logMonoTime = int(t * 1e9)
    self._name = name
    for k, v in fields.items():
      setattr(self, k, v)

  def which(self):
    return self._name


def _accel(t, v):
  inner = types.SimpleNamespace(which=lambda: 'acceleration', acceleration=types.SimpleNamespace(v=v))
  return _Msg('accelerometer', t, accelerometer=inner)


def _car(t, v_ego, a_ego=0.0, brake=False, gas=False):
  return _Msg('carState', t, carState=types.SimpleNamespace(vEgo=v_ego, aEgo=a_ego, brakePressed=brake, gasPressed=gas))


class FakeMessaging:
  """Hands out new wrapper objects from poll(), like cereal does."""
  def __init__(self, script, polls_before_stop):
    self.queues = {}
    self.script = list(script)
    self.polls = 0
    self.polls_before_stop = polls_before_stop
    self.drained = 0

  def Poller(self):
    fake = self

    class P:
      def poll(self, timeout):
        fake.polls += 1
        if fake.polls > fake.polls_before_stop:
          raise _Stop()
        if fake.script:
          name, msg = fake.script.pop(0)
          fake.queues[name].append(msg)
        return [object()]  # never one of the sockets we created
    return P()

  def sub_sock(self, name, poller=None, conflate=False):
    self.queues[name] = []
    return types.SimpleNamespace(name=name)

  def drain_sock(self, sock):
    msgs, self.queues[sock.name] = self.queues[sock.name], []
    self.drained += len(msgs)
    return msgs


class EventLockLoop(unittest.TestCase):
  def run_loop(self, script, polls):
    fake = FakeMessaging(script, polls)
    params = mock.Mock()
    params.get.return_value = None   # not recording: events are found but have no segment to lock
    params.get_bool.return_value = False
    cereal = types.ModuleType('openpilot.cereal')
    cereal.messaging = fake
    params_mod = types.ModuleType('openpilot.common.params')
    params_mod.Params = lambda: params
    gps_mod = types.ModuleType('openpilot.common.gps')
    gps_mod.get_gps_location_service = lambda p: 'gpsLocation'
    swaglog_mod = types.ModuleType('openpilot.common.swaglog')
    swaglog_mod.cloudlog = mock.Mock()
    fakes = {'openpilot.cereal': cereal, 'openpilot.cereal.messaging': fake, 'openpilot.common.params': params_mod,
             'openpilot.common.gps': gps_mod, 'openpilot.common.swaglog': swaglog_mod}
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.dict(sys.modules, fakes), \
         mock.patch.object(el, 'LOG_ROOT', tmp), \
         mock.patch.object(el, 'EVENTS_PATH', str(Path(tmp) / 'events.jsonl')):
      with self.assertRaises(_Stop):
        el._run()
    return fake

  def test_messages_are_processed_without_crashing(self):
    script = []
    t = 100.0
    for i in range(200):
      t += 0.01
      script.append(('accelerometer', _accel(t, [9.8, 0.1, 0.2])))
      script.append(('carState', _car(t, 10.0)))
    fake = self.run_loop(script, polls=len(script) + 5)
    self.assertEqual(fake.drained, len(script))

  def test_an_impact_is_handled_inside_the_loop(self):
    script = []
    t = 100.0
    for i in range(80):
      t += 0.01
      script.append(('accelerometer', _accel(t, [9.8, 0.0, 0.3] if i != 60 else [9.8, 25.0, 0.3])))
    fake = self.run_loop(script, polls=len(script) + 120)
    self.assertEqual(fake.drained, len(script))

  def test_main_restarts_after_an_error(self):
    calls = []

    def boom():
      calls.append(1)
      if len(calls) < 3:
        raise RuntimeError('simulated')
      raise _Stop()
    with mock.patch.object(el, '_run', boom), mock.patch.object(el.time, 'sleep'):
      with self.assertRaises(_Stop):
        el.main()
    self.assertEqual(len(calls), 3)


if __name__ == '__main__':
  unittest.main()
