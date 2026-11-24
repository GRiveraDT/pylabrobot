# Flex execution events

The Flex publishes structured events through PLR's optional
[EventBus](../../../machine-agnostic-features/event-bus.md). Subscribers can display
progress or queue records for an external service. They do not acknowledge actions,
update resources, or control execution. Continue to use the awaited `handle_action`
callback for loading, unloading and interventions.

Subscribe before starting the operation. In an existing asynchronous application,
with a prepared Flex, a bound protocol and a configured handler:

```python
from queue import SimpleQueue

from pylabrobot.events import EventBus, event_context, use_event_bus

records = SimpleQueue()
bus = EventBus()
unsubscribe = bus.subscribe(lambda event: records.put(event.as_dict()))

try:
  with use_event_bus(bus), event_context(run_id="extraction-42"):
    result = await flex.run_protocol(bound_protocol, handle_action=handler)
finally:
  unsubscribe()
```

An application consumer can drain `records` into its log or dashboard. Configure a
retention policy for long-running applications; this illustrative queue is unbounded.
Subscribers run synchronously, so keep them fast and perform network I/O elsewhere.
An ordinary subscriber exception is logged and does not fail instrument execution.

## Names and completion

Operation names are stable across protocols. Each emits `.started`, followed by
`.completed` or `.failed`, sharing `context.operation_id`:

| Operation | Meaning |
| --- | --- |
| `kingfisher.setup` | Identify the instrument; completion includes serial number and firmware. |
| `kingfisher.initialize` | Await axis initialization; already-initialized calls complete without repeated motion. |
| `kingfisher.stop` | Disconnect; this does **not** mean motion has stopped. |
| `kingfisher.abort` | Await the controller's abort completion. |
| `kingfisher.run_protocol` | Validate and execute the bound BDZ, including finalization commands. |
| `kingfisher.load`, `kingfisher.unload`, `kingfisher.pause` | Await the action handler or physical Start and verify expected carousel occupancy. |
| `kingfisher.rotate` | Dispatch a carousel rotation and await terminal firmware completion. |
| `kingfisher.step` | Execute a named BDZ step, including positioning and sub-operations. |
| `kingfisher.mix` | Execute one timed mixing segment; includes release mixing and postmix. |
| `kingfisher.collect_beads` | Execute one recognized collection cycle or precollection. |
| `kingfisher.pick_up_tip_comb`, `kingfisher.return_tip_comb` | Execute the recognized 96DW comb pickup/return sequence. |

For example, `kingfisher.mix.started` carries `step_name="Initial Bead Mix"` and
`phase="mixing"`. The event name is not constructed from the arbitrary BDZ name.
Use `step_name` as the display label and `step_index` to distinguish repeated names.
`script_label` preserves the exported firmware label, which may replace spaces with
underscores. Mixing `iteration` identifies the loop iteration where known;
`segment_index` distinguishes timed segments within a step. Collection events include
`cycle_index` and `cycle_count`. Indices are one-based; durations are in seconds.

`.started` means PLR is entering the operation before issuing its commands, not that
physical motion has been independently detected. `kingfisher.step.started` precedes
the step's label command and any positioning. `kingfisher.mix.started` precedes the
actual timed mixing command. Completion waits for terminal responses, not merely
command acknowledgments. Comb completion does not verify mechanical attachment,
and collection completion does not measure bead capture.

## Expected and actual plates

A rotation event contains `target_processing_slot` and `target_loading_slot`. Its
`processing` and `loading` objects each contain:

- `tray`: the printed tray number, 1–8.
- `holder`: the persistent PLR holder reference.
- `expected_plate`: the run's expected occupant before this rotation.
- `actual_plate`: the occupant assigned in the PLR resource tree at dispatch.

`actual_plate` is a **model observation**, not a plate-detection sensor reading.
Both references are `None` for an expected empty tray. Before a loading action,
`requested_plate` separately identifies the plate to be loaded; its absence from the
tray is normal. Initialization uses the current model as its expectation and reports
`inventory_verified`; it does not confirm physical inventory.

Rotation completion publishes the new orientation. Plate objects never move between
holders merely because the carousel rotates. Action events identify the bound plate,
BDZ plate ID/name, presented holder, acknowledgment mode and required additions.
`plate_location` describes the model at action entry and again at completion.
The completed action also reports `inventory_verified`. Physical resource-transfer
events from `ManualOperator` or another instrument remain separate from Flex action
fulfillment events.

## Observations and coverage

Three observations have no lifecycle suffix:

- `kingfisher.script_label`: a `TXT` label acknowledged by the controller.
- `kingfisher.temperature_report`: an unsolicited `TEM` report, preserved in `report`.
  This is not evidence that a setpoint was reached; the raw report fields are not
  assigned undocumented sensor meanings.
- `kingfisher.session_invalidated`: the connection/session became uncertain.

Reader observations during execution retain the originating command's event context,
including the application's run ID. Distinct device names and serial numbers distinguish
multiple instruments. The existing single-consumer `flex.events()` stream remains
available alongside EventBus subscribers.

Annotations match step metadata to script labels and recognize the observed 96DW
command sequences. They do not regenerate or change the BDZ script. Unknown labels
remain visible through `script_label` observations. Within a matched step,
`unmapped_features` reports collection or mixing metadata that could not be reconciled
with its commands. Timed mixing still reports its command duration with
`phase="unspecified"` when phase attribution is uncertain; unmatched collection and
tip patterns do not produce guessed semantic events. Pause presets implemented as
waits do not produce fictitious mixing events. A new export variant may need additional
annotation support without needing changes to execution.

The complete payload contract is in the
[Event Schema Registry](../../../../contributor_guide/event-schemas.md#kingfisher-flex).
