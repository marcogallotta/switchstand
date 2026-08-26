# Product boundary

## Operator problem

An operator needs to work directly with two distinct logical roles without losing which role
received which instruction, whether a message is still queued, which exact attempt is live,
or whether a late result is still eligible to update durable context. A generic chat list
does not make those execution truths obvious, especially after a stop, correction, process
restart, or lost acknowledgement.

## Intended experience

Switchstand presents one Work with two durable role cards. Each role has its own ordered
message stream, accepted checkpoint, selected attempt, and attempt history. The operator can:

- send a direct message to either role;
- see later messages remain queued behind the active turn;
- stop the exact selected attempt;
- redirect it by durably recording a correction, stopping it, and replacing it from the
  role checkpoint;
- replace a stopped, failed, stale, or unknown selected attempt;
- distinguish accepted output from visible but fenced stale output; and
- restart the local service without treating an interrupted mutation as known success.

## Boundaries

The prototype has one local Work and exactly two roles. It stores a JSON snapshot and JSONL
event log on one machine. It serves one unauthenticated loopback HTTP UI and uses one explicit
Codex app-server adapter over a Unix socket.

Switchstand does not decide what the roles should build, grant remote-system authority,
coordinate releases, schedule background projects, manage multiple users, or claim durable
distributed execution. It does not hide transport uncertainty: `unknown` means the available
evidence does not prove the outcome, and `stale` means output was observed but was not eligible
to update the selected generation's checkpoint.
