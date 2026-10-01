# 2026-09-30 shared Funnel ingress outage

## Impact and status

Dish and Switchstand ChatGPT clients simultaneously returned `Connection failed`. Reported client
requests did not reach either application. Rewriting the Tailscale Funnel configuration restored
Dish. The currently configured public listener maps HTTPS port 443 to `127.0.0.1:8786`, the shared
reverse-proxy origin in front of both applications.

This is a technical RCA with **high confidence in the failed layer** and **unknown confidence in the
exact Tailscale implementation defect**. No evidence here establishes corrupted persistent state or
an application/OAuth fault.

## Evidence and timeline

All times are local CEST on 2026-09-30.

- `tailscaled` remained active as PID 1609; it did not crash or restart.
- At 19:46:13, its journal recorded a link change and closed multiple live system connections to
  `127.0.0.1:8786`.
- From 19:46:13 through 19:46:47, Tailscale control-plane and DERP attempts repeatedly failed with
  `network is unreachable`. At 19:46:49 the default route returned and the control connection
  recovered; DERP reconnected by 19:46:52.
- At 19:48:18 and 19:48:57, `tailscaled` still recorded proxy requests ending with
  `context canceled`.
- At 20:10:18, `tailscale funnel reset` produced a Serve-config POST, closed the handlers for
  `127.0.0.1:8775`, `:8786`, `:8798`, `:8765`, and `:8001`, closed listeners on 443 and
  8443–8446, and changed `Hostinfo.IngressEnabled` to false. The immediately following
  `tailscale funnel --bg 8786` produced a second Serve-config POST, created a new handler for
  `127.0.0.1:8786`, and changed ingress back to true. Dish then recovered.
- After mitigation, a read-only probe forced the public hostname to each IPv4 address returned by
  public DNS, preserving the hostname for TLS and HTTP. All three Funnel addresses returned an HTTP
  response, and the exact Switchstand public resource returned its expected 401 challenge and
  protected-resource metadata.
- A normal same-host lookup of the same public hostname resolved to the node's Tailscale address.
  Therefore a conventional same-host `curl https://…` bypasses Funnel and cannot prove public
  ingress health.

The reset also removed every other pre-existing Serve listener, so restoring 443 alone recovered
the shared user path but left those independent routes down. A subsequent bounded reconstruction
and exact status readback restored this complete route set:

- 443 to `127.0.0.1:8786`, public Funnel;
- 8443 to `127.0.0.1:8001`, public Funnel;
- 8444 to `127.0.0.1:8765`, tailnet-only Serve;
- 8445 to `127.0.0.1:8775`, tailnet-only Serve; and
- 8446 to `127.0.0.1:8798`, tailnet-only Serve.

## Causal conclusion

The proximate failure was the shared Tailscale Funnel/Serve ingress state after a host-network
flap. The applications and their OAuth/provider paths are downstream of the shared `:8786` origin:
a simultaneous cross-application client failure, absence of application receipt, and recovery from
rewriting only Funnel configuration jointly localize the incident above those services.

The most precise supported root cause is: **Tailscale public ingress did not return to a working
state after network reconvergence, despite the daemon and local origin remaining available; a
Serve-config rewrite reinitialized ingress.** The retained journal does not reveal whether the
defect was a stale listener, DERP ingress registration, connection state, or another internal
Tailscale condition. Calling it configuration corruption would exceed the evidence.

## Prevention and monitoring

Use independent, layered claims:

1. Local process and loopback challenge/metadata checks establish application transport.
2. A public-DNS-pinned HTTPS challenge/metadata check establishes the external Funnel route and
   must reject Tailscale CGNAT or other non-public answers.
3. The fixed read-only authenticated `work_get` canary separately establishes OAuth, application,
   and provider function.
4. A distinct `shared_ingress_failure` transition enters the durable Wakeful outbox when step 1 is
   green and step 2 is not. Existing transition suppression prevents an alert storm; recovery still
   requires two healthy cycles.

Run the one-shot monitor on a short systemd timer only after review and host qualification. The
candidate in this incident is inert: it neither installs that timer nor dispatches an event. The
smallest later wake path is one consumer of the existing Wakeful outbox that durably delivers each
event ID to the registered-agent message surface and marks it delivered only after attributable
acceptance. Until scheduling and that consumer are implemented and activated, no inactive agent is
woken.

Do not automate `tailscale funnel reset` as recovery. It is a whole Serve-config mutation and this
incident demonstrates collateral removal of unrelated listeners. Any self-heal must first compare
the exact expected shared-ingress configuration, apply the narrowest idempotent repair, read it
back, and preserve an explicit rollback snapshot; ambiguous state must alert rather than retry.
