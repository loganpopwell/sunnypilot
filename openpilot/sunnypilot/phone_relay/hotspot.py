"""Automatic access point so the paired iPhone can reach the comma away from known Wi-Fi.

Client first: whenever the comma is on a saved Wi-Fi network (e.g. home) nothing changes.
After NO_WIFI_SECONDS (20 s) without a Wi-Fi client connection, a dedicated AP ("phone-relay-ap") is
started with the pairing SSID/passphrase. After RESCAN_SECONDS parked with the AP up, it is
dropped once so the comma can rejoin home Wi-Fi; if none appears it comes back. It is never
dropped while the phone has used the relay in the last two minutes. A user-enabled
Tethering ("Hotspot") connection always wins: this never touches it.
"""
import subprocess
import time

CONNECTION = 'phone-relay-ap'
ADDRESS = '192.168.44.1/24'
NO_WIFI_SECONDS = 20  # home Wi-Fi never reaches the car; NetworkManager joins known networks well within this
RESCAN_SECONDS = 300
RESCAN_WINDOW = 45


def nmcli(*args, timeout=20):
    r = subprocess.run(['sudo', 'nmcli', *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip()


class Nmcli:
    def wlan_connection(self):
        """Name of the active connection on wlan0, or None."""
        code, out = nmcli('-t', '-f', 'GENERAL.STATE,GENERAL.CONNECTION', 'device', 'show', 'wlan0')
        if code != 0:
            return None
        fields = dict(line.split(':', 1) for line in out.splitlines() if ':' in line)
        if not fields.get('GENERAL.STATE', '').startswith('100'):
            return None
        return fields.get('GENERAL.CONNECTION') or None

    def ensure(self, ssid, psk):
        code, out = nmcli('-t', '-f', '802-11-wireless.ssid,802-11-wireless-security.psk', '--show-secrets',
                          'connection', 'show', CONNECTION)
        wanted = {'802-11-wireless.ssid': ssid, '802-11-wireless-security.psk': psk}
        if code == 0 and dict(line.split(':', 1) for line in out.splitlines() if ':' in line) == wanted:
            return
        if code == 0:
            nmcli('connection', 'delete', CONNECTION)
        nmcli('connection', 'add', 'type', 'wifi', 'ifname', 'wlan0', 'con-name', CONNECTION,
              'autoconnect', 'no', 'ssid', ssid,
              '802-11-wireless.mode', 'ap', '802-11-wireless.band', 'a', '802-11-wireless.channel', '36',
              'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.proto', 'rsn', 'wifi-sec.pairwise', 'ccmp',
              'wifi-sec.psk', psk, 'ipv4.method', 'shared', 'ipv4.addresses', ADDRESS, 'ipv6.method', 'disabled')

    def up(self):
        code, _ = nmcli('connection', 'up', CONNECTION, timeout=45)
        if code != 0:  # 5 GHz AP not permitted here; fall back to 2.4 GHz
            nmcli('connection', 'modify', CONNECTION, '802-11-wireless.band', 'bg', '802-11-wireless.channel', '6')
            code, _ = nmcli('connection', 'up', CONNECTION, timeout=45)
        return code == 0

    def down(self):
        nmcli('connection', 'down', CONNECTION)


class HotspotManager:
    def __init__(self, ssid, psk, onroad, nm=None, clock=time.monotonic, busy=lambda: False):
        self.ssid, self.psk, self.onroad, self.busy = ssid, psk, onroad, busy
        self.nm = nm or Nmcli()
        self.clock = clock
        self.no_wifi_since = clock()
        self.parked_since = None
        self.rescan_until = None
        self.ready = False
        self.last_connection = None

    def step(self):
        if not self.ready:
            self.nm.ensure(self.ssid, self.psk); self.ready = True
        now = self.clock()
        active = self.last_connection = self.nm.wlan_connection()
        if active == 'Hotspot':  # user turned on Tethering; leave it alone
            self.parked_since = None; self.no_wifi_since = now
            return 'tethering'
        if active == CONNECTION:
            if self.onroad() or self.busy():  # driving, or the phone is still pulling footage
                self.parked_since = None
                return 'ap'
            if self.parked_since is None:
                self.parked_since = now
            if now - self.parked_since >= RESCAN_SECONDS:
                self.nm.down(); self.parked_since = None
                self.rescan_until = now + RESCAN_WINDOW; self.no_wifi_since = now
                return 'rescan'
            return 'ap'
        self.parked_since = None
        if active is not None:  # on a saved network: stay a client
            self.no_wifi_since = now; self.rescan_until = None
            return 'client'
        if self.rescan_until is not None and now < self.rescan_until:
            return 'waiting'
        if now - self.no_wifi_since >= NO_WIFI_SECONDS:
            self.rescan_until = None
            return 'ap' if self.nm.up() else 'failed'
        return 'waiting'


def run(ssid, psk, onroad, stop, busy=lambda: False, state=None):
    manager = HotspotManager(ssid, psk, onroad, busy=busy)
    while not stop.is_set():
        try:
            mode = manager.step()
            if state is not None:
                state['wifi'] = {'mode': mode, 'connection': manager.last_connection}
        except Exception:
            pass  # never take the relay down over Wi-Fi management
        stop.wait(5)
