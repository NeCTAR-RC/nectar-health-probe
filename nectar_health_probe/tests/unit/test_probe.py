#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import tempfile
from unittest import mock

import oslo_messaging as messaging

from nectar_health_probe import probe
from nectar_health_probe.tests.unit import base


# /proc/net/tcp with one ESTABLISHED (01) connection to port 5671
# (0x1627) and one LISTEN (0A) socket on port 5672 (0x1628).
PROC_NET_TCP = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when"
    " retrnsmt   uid  timeout inode\n"
    "   0: 0100007F:B0EA 0A00000A:1627 01 00000000:00000000 00:00000000"
    " 00000000 42420        0 12345 1 0000000000000000 20 4 30 10 -1\n"
    "   1: 00000000:1628 00000000:0000 0A 00000000:00000000 00:00000000"
    " 00000000 42420        0 12346 1 0000000000000000 100 0 0 10 0\n"
)


class TestRPCPing(base.TestCase):
    @mock.patch.object(probe, 'messaging')
    def test_ping_ok(self, mock_messaging):
        conf = mock.sentinel.conf
        client = mock_messaging.get_rpc_client.return_value
        client.call.return_value = 'pong'

        self.assertEqual(
            0, probe.check_rpc_ping(conf, 'svc-worker', 'host1', timeout=8)
        )

        mock_messaging.get_rpc_transport.assert_called_once_with(conf)
        mock_messaging.Target.assert_called_once_with(
            topic='svc-worker', server='host1', exchange=None
        )
        mock_messaging.get_rpc_client.assert_called_once_with(
            mock_messaging.get_rpc_transport.return_value,
            mock_messaging.Target.return_value,
            timeout=8,
        )
        client.call.assert_called_once_with({}, 'oslo_rpc_server_ping')

    @mock.patch.object(probe, 'messaging')
    def test_ping_explicit_exchange(self, mock_messaging):
        client = mock_messaging.get_rpc_client.return_value
        client.call.return_value = 'pong'

        self.assertEqual(
            0,
            probe.check_rpc_ping(
                mock.sentinel.conf, 'engine', 'host1', exchange='heat'
            ),
        )

        mock_messaging.Target.assert_called_once_with(
            topic='engine', server='host1', exchange='heat'
        )

    @mock.patch.object(probe, 'messaging')
    def test_ping_timeout(self, mock_messaging):
        client = mock_messaging.get_rpc_client.return_value
        client.call.side_effect = messaging.MessagingTimeout('timed out')

        self.assertEqual(
            1, probe.check_rpc_ping(mock.sentinel.conf, 'svc-worker', 'host1')
        )

    @mock.patch.object(probe, 'messaging')
    def test_ping_unexpected_reply(self, mock_messaging):
        client = mock_messaging.get_rpc_client.return_value
        client.call.return_value = 'ping'

        self.assertEqual(
            1, probe.check_rpc_ping(mock.sentinel.conf, 'svc-worker', 'host1')
        )


class TestAMQP(base.TestCase):
    def _write_proc_file(self, content):
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        return path

    def test_amqp_ports_explicit(self):
        url = messaging.TransportURL.parse(
            self.make_conf(),
            'rabbit://user:pass@host1:5671,user:pass@host2:5671/vhost',
        )
        self.assertEqual({5671}, probe._amqp_ports(url))

    def test_amqp_ports_default(self):
        url = messaging.TransportURL.parse(
            self.make_conf(), 'rabbit://host1/vhost'
        )
        self.assertEqual({5671, 5672}, probe._amqp_ports(url))

    def test_established_remote_ports(self):
        path = self._write_proc_file(PROC_NET_TCP)
        self.assertEqual({5671}, probe._established_remote_ports((path,)))

    def test_established_remote_ports_missing_file(self):
        self.assertEqual(
            set(),
            probe._established_remote_ports(('/nonexistent-proc',)),
        )

    def test_check_amqp_established(self):
        path = self._write_proc_file(PROC_NET_TCP)
        conf = self.make_conf('rabbit://user:pass@host1:5671/vhost')
        orig = probe._established_remote_ports
        with mock.patch.object(
            probe,
            '_established_remote_ports',
            side_effect=lambda: orig((path,)),
        ):
            self.assertEqual(0, probe.check_amqp(conf))

    def test_check_amqp_not_established(self):
        conf = self.make_conf('rabbit://user:pass@host1:5671/vhost')
        with mock.patch.object(
            probe, '_established_remote_ports', return_value=set()
        ):
            self.assertEqual(1, probe.check_amqp(conf))

    def test_check_amqp_no_hosts(self):
        conf = self.make_conf()
        self.assertEqual(1, probe.check_amqp(conf))


class TestMain(base.TestCase):
    @mock.patch.object(probe.messaging, 'set_transport_defaults')
    @mock.patch.object(probe, 'check_rpc_ping', return_value=0)
    def test_topic_defaults_from_project(self, mock_check, mock_defaults):
        self.assertEqual(0, probe.main(['--project', 'testsvc', 'rpc-ping']))

        mock_defaults.assert_called_once_with(control_exchange='testsvc')
        conf, topic, server = mock_check.call_args.args
        self.assertEqual('testsvc-worker', topic)
        self.assertEqual(conf.host, server)
        self.assertEqual(
            {'exchange': None, 'timeout': 8}, mock_check.call_args.kwargs
        )

    @mock.patch.object(probe.messaging, 'set_transport_defaults')
    @mock.patch.object(probe, 'check_rpc_ping', return_value=1)
    def test_explicit_opts_override(self, mock_check, mock_defaults):
        self.assertEqual(
            1,
            probe.main(
                [
                    '--project',
                    'testsvc',
                    'rpc-ping',
                    '--topic',
                    'engine',
                    '--server',
                    'host9',
                    '--exchange',
                    'heat',
                    '--timeout',
                    '3',
                ]
            ),
        )

        _, topic, server = mock_check.call_args.args
        self.assertEqual('engine', topic)
        self.assertEqual('host9', server)
        self.assertEqual(
            {'exchange': 'heat', 'timeout': 3}, mock_check.call_args.kwargs
        )

    @mock.patch.object(probe, 'check_rpc_ping')
    def test_rpc_ping_requires_project_or_topic(self, mock_check):
        self.assertEqual(2, probe.main(['rpc-ping']))
        mock_check.assert_not_called()

    @mock.patch.object(probe.messaging, 'set_transport_defaults')
    @mock.patch.object(probe, 'check_amqp', return_value=0)
    def test_amqp_dispatch(self, mock_check, mock_defaults):
        self.assertEqual(0, probe.main(['--project', 'testsvc', 'amqp']))

        mock_check.assert_called_once()
        mock_defaults.assert_called_once_with(control_exchange='testsvc')

    @mock.patch.object(probe.messaging, 'set_transport_defaults')
    @mock.patch.object(probe, 'check_amqp', return_value=0)
    def test_no_project_skips_transport_defaults(
        self, mock_check, mock_defaults
    ):
        self.assertEqual(0, probe.main(['amqp']))

        mock_defaults.assert_not_called()
