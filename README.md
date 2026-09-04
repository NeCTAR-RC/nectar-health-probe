# nectar-health-probe

A container exec health probe for OpenStack worker processes. It answers
one question: *is this pod's worker actually processing messages from
its own queue?* — not merely whether a PID exists.

Two checks are provided:

- **rpc-ping** — calls oslo.messaging's built-in ping endpoint
  (`oslo_rpc_server_ping`, available since oslo.messaging 12.4.0) on a
  specific RPC server, i.e. the queue `<topic>.<server>`. Targeting a
  specific server is essential: a bare topic call would round-robin the
  shared queue and any healthy replica could answer for a dead one.
- **amqp** — passes if the pod's network namespace holds an ESTABLISHED
  TCP connection to a `transport_url` port (read from
  `/proc/net/tcp{,6}`). For processes that consume from the bus without
  running an RPC server (notification listeners), which have no ping
  endpoint. This is a weaker check: it relies on AMQP heartbeat expiry
  to eventually drop the connection of a wedged process.

## Usage

```
nectar-health-probe --project <svc> [options] {rpc-ping,amqp}
```

Typical invocations, run as Kubernetes exec probes inside the pod being
checked:

```
nectar-health-probe --project varroa rpc-ping
nectar-health-probe --project warre amqp
nectar-health-probe --project heat --topic engine rpc-ping
```

`--project` does three things:

1. oslo.config discovers `/etc/<project>/<project>.conf` **and**
   `/etc/<project>/<project>.conf.d/` — important where secrets such as
   `transport_url` are injected as a conf.d snippet.
2. `control_exchange` defaults to the project name, reproducing the
   `oslo_messaging.set_transport_defaults()` call most services make in
   code. An explicit `control_exchange` in the conf file still wins,
   exactly as it does for the service itself.
3. The rpc-ping topic defaults to `<project>-worker`.

Options: `--topic` (RPC server topic), `--server` (queue suffix,
defaults to this hostname — matching workers that set
`server=CONF.host`), `--exchange` (hard override of the exchange),
`--timeout` (RPC reply wait, default 8s), plus the standard oslo.config
`--config-file`/`--config-dir`.

Exit codes: 0 healthy, 1 unhealthy, 2 usage error.

## Server-side requirements

`rpc-ping` requires the *service* to expose the ping endpoint:

```ini
[DEFAULT]
rpc_ping_enabled = true
```

Recommended belt-and-braces: also render `control_exchange = <svc>` in
the service's conf so the config is self-describing for external tools,
even though the service sets the same value in code.

## Probe configuration guidance

Use it as a **startupProbe**:

```yaml
startupProbe:
  exec:
    command: ["nectar-health-probe", "--project", "myservice", "rpc-ping"]
  periodSeconds: 10
  timeoutSeconds: 10
  failureThreshold: 30
```

Think twice before also wiring it as a livenessProbe: the check reaches
through the message bus, so a RabbitMQ outage fails it on every worker
at once and Kubernetes mass-restarts pods that oslo.messaging would
have ridden out by auto-reconnecting. The failure class a probe
reliably catches (a service manager that starts but never spawns a
consumer) shows up at startup. If you do add liveness, use a long
period and a high failureThreshold so it rides out broker blips — and
remember each rpc-ping run spawns a Python interpreter plus an AMQP
connection and reply queue, which is real RabbitMQ churn at fleet
scale.

Keep database or Keystone checks out of probes entirely. Fleet-level
correctness (service lists, agent heartbeats, queue depth, wedged
consumers after startup) belongs to metrics alerting; pod probes
should only assert "this process is processing its own messages".

Before trusting a new rpc-ping probe, verify the queue name
`<topic>.<server>` exists in the RabbitMQ management UI — a mismatched
`server` (some services hardcode one rather than using the host) or a
wrong exchange makes the probe fail closed and restart-loop the pod.
