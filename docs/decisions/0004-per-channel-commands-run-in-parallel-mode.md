# 0004 Per-channel commands run in PARALLEL mode

- **Status:** Accepted, 2026-11-05
- **Rule waived:** `op-execution-mode-by-repeatability` (the guide's table gives `SINGLE` for every non-idempotent command)

## Context

An execution mode applies to the whole command, not to one channel. With `SINGLE`, a `StartProtocol` on channel 2 would be rejected while a `StartProtocol` on channel 1 is still running. Starting can take seconds while the vendor software converts and loads the protocol, so an orchestrator starting many channels would get spurious `CommandExecutionNotAccepted` rejections.

## Decision

- `StartProtocol`, `StopProtocol`, `PauseProtocol`, `ResumeProtocol` and `ClearChannel` use `PARALLEL`.
- The backend enforces exclusion per channel. A second `StartProtocol` or a `ClearChannel` on a channel that is running or paused raises `ChannelBusyError`, a defined execution error. That keeps the rule's intent: no double start on the same channel.
- `StopProtocol`, `PauseProtocol` and `ResumeProtocol` are idempotent and succeed without effect when already in the target state.
- If a vendor API cannot take concurrent calls, the backend queues them internally. That limitation belongs to the backend, not to the SiLA interface.

## Consequences

The conformance kit checks that:

- concurrent starts on two different channels both succeed
- concurrent starts on the same channel result in exactly one success and one `ChannelBusyError`

## Scope

This covers the per-channel commands listed above. Any command acting on the whole cycler follows the rule as written.
