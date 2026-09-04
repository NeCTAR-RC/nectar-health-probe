#!/usr/bin/env python
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

"""Generic health probe for OpenStack worker processes.

Run as a container exec probe inside the pod being checked, so the
service's own config supplies transport_url. Pass ``--project <svc>``
so oslo.config discovers ``/etc/<svc>/<svc>.conf`` and
``/etc/<svc>/<svc>.conf.d/`` (where secrets such as transport_url are
often injected), and so control_exchange defaults to the project name,
matching the ``oslo_messaging.set_transport_defaults()`` call most
services make in code.

``rpc-ping`` calls oslo.messaging's built-in ping endpoint
(``oslo_rpc_server_ping``) on the RPC server named by
``--topic``/``--server``, proving the worker is consuming and
dispatching messages from its own queue (``<topic>.<server>``), not
merely that the process exists. Requires ``[DEFAULT] rpc_ping_enabled
= true`` in the server's config. The topic defaults to
``<project>-worker`` and the server to this hostname, which matches
workers that set ``server=CONF.host`` on their RPC target.

``amqp`` passes if this network namespace holds an ESTABLISHED TCP
connection to a messaging port from ``transport_url``. This is for
processes that consume from the bus without running an RPC server
(notification listeners) and so have no ping endpoint. It is a weaker
check: it relies on AMQP heartbeat expiry to drop the connection of a
wedged process.

Exits 0 when healthy, 1 otherwise.
"""

import argparse
import socket
import sys

from oslo_config import cfg
import oslo_messaging as messaging
from pbr import version


PROC_NET_TCP = ('/proc/net/tcp', '/proc/net/tcp6')
# TCP_ESTABLISHED, hex, as found in the `st` column of /proc/net/tcp
TCP_ESTABLISHED = '01'
# Standard AMQP plain/TLS ports, assumed when a transport_url host has
# no explicit port.
AMQP_PORTS = (5672, 5671)

cli_opts = [
    cfg.StrOpt(
        'check',
        positional=True,
        required=True,
        choices=['rpc-ping', 'amqp'],
        help='Health check to run. rpc-ping calls an RPC server\'s '
        'oslo_rpc_server_ping endpoint; amqp checks for an established '
        'TCP connection to the messaging server.',
    ),
    cfg.StrOpt(
        'topic',
        help='Topic of the RPC server to ping. Defaults to '
        '<project>-worker when --project is given.',
    ),
    cfg.StrOpt(
        'server',
        help='Server name of the RPC server to ping (the queue suffix '
        'in <topic>.<server>). Defaults to the host option, which '
        'defaults to this hostname. Essential for probing: a bare '
        'topic call would round-robin the shared queue and any healthy '
        'replica could answer for a dead one.',
    ),
    cfg.StrOpt(
        'exchange',
        help='Exchange the RPC server\'s queues live on. Defaults to '
        'control_exchange, whose default is the --project name.',
    ),
    cfg.IntOpt(
        'timeout',
        default=8,
        min=1,
        help='Seconds to wait for the RPC ping reply.',
    ),
]

opts = [
    cfg.StrOpt(
        'host',
        default=socket.gethostname(),
        help='Name of this node, matching the host option of the '
        'service being probed.',
    ),
]


def check_rpc_ping(conf, topic, server, exchange=None, timeout=8):
    transport = messaging.get_rpc_transport(conf)
    target = messaging.Target(topic=topic, server=server, exchange=exchange)
    client = messaging.get_rpc_client(transport, target, timeout=timeout)
    try:
        reply = client.call({}, 'oslo_rpc_server_ping')
    except Exception as e:
        print(f'RPC ping to {topic}.{server} failed: {e}', file=sys.stderr)
        return 1
    if reply != 'pong':
        print(f'RPC ping returned unexpected reply: {reply}', file=sys.stderr)
        return 1
    return 0


def _amqp_ports(url):
    ports = set()
    for host in url.hosts:
        if host.port:
            ports.add(host.port)
        else:
            ports.update(AMQP_PORTS)
    return ports


def _established_remote_ports(proc_files=PROC_NET_TCP):
    ports = set()
    for path in proc_files:
        try:
            with open(path) as f:
                entries = f.readlines()[1:]
        except OSError:
            continue
        for entry in entries:
            fields = entry.split()
            if len(fields) < 4 or fields[3] != TCP_ESTABLISHED:
                continue
            # remote address is hex ip:port
            ports.add(int(fields[2].rsplit(':', 1)[1], 16))
    return ports


def check_amqp(conf):
    expected = _amqp_ports(messaging.TransportURL.parse(conf))
    if not expected:
        print('transport_url has no hosts to check', file=sys.stderr)
        return 1
    if expected & _established_remote_ports():
        return 0
    print(
        'no established TCP connection to messaging '
        f'port(s) {sorted(expected)}',
        file=sys.stderr,
    )
    return 1


def _parse_project(argv):
    """Extract --project before oslo.config parses the command line.

    The project name is needed before cfg.ConfigOpts is called (it
    drives config file discovery) and before the transport is created
    (it sets the control_exchange default), so it cannot itself be an
    oslo option.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--project')
    known, remaining = pre.parse_known_args(argv)
    return known.project, remaining


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    project, argv = _parse_project(argv)
    if project:
        # Reproduce the set_transport_defaults(control_exchange=...)
        # call OpenStack services make in code; without it an external
        # probe would default to the 'openstack' exchange and ping a
        # queue that does not exist. An explicit control_exchange in
        # the conf file still takes precedence, as it does for the
        # service itself.
        messaging.set_transport_defaults(control_exchange=project)

    # Use the global CONF object: oslo.messaging registers some option
    # groups (e.g. oslo_messaging_metrics) on the global object at
    # import time but reads them back from the conf the transport was
    # created with, so a private ConfigOpts raises NoSuchOptError
    # inside client.call().
    conf = cfg.CONF
    conf.register_cli_opts(cli_opts)
    conf.register_opts(opts)
    release = version.VersionInfo('nectar-health-probe').release_string()
    conf(
        argv,
        project=project,
        prog='nectar-health-probe',
        version=f'%prog {release}',
        usage='%(prog)s [--project PROJECT] [options] {rpc-ping,amqp}',
    )

    if conf.check == 'rpc-ping':
        topic = conf.topic or (f'{project}-worker' if project else None)
        if not topic:
            print('rpc-ping requires --topic or --project', file=sys.stderr)
            return 2
        server = conf.server or conf.host
        return check_rpc_ping(
            conf,
            topic,
            server,
            exchange=conf.exchange,
            timeout=conf.timeout,
        )
    return check_amqp(conf)


if __name__ == '__main__':
    sys.exit(main())
