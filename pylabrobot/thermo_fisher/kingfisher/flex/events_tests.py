"""Semantic event boundaries against controlled HID responses, without USB access."""

import asyncio
import json
from collections import Counter

from pylabrobot.events import EventBus, PLREvent, event_context, use_event_bus

from .actions import FlexAction
from .connection import FlexError
from .flex import KingFisherFlex
from .protocol import FlexCommand, read_flex_bdz
from .test_helpers import HIDTestCase, bdz_bytes

PICKUP = "\n".join(
  [
    "MOS 0",
    "MOH 230 1800",
    "MOM -130",
    "MOH 650 80",
    "MOM 0",
    "MOH 230 100",
    "MOM 460",
    "MOM 32",
    "MOH 0 1800",
    "MOS 1",
  ]
)
RETURN = "\n".join(
  [
    "MOS 0",
    "MOH 230 1800",
    "MOH 650 80",
    "MOM -130",
    "MOH 230 80",
    "MOM 0",
    "MOH 0 1800",
  ]
)
MIX = "MOH 640 200 0 100 0"
COLLECT = "MOH 627 100\nMOM 460\nWAI 1 150\nWAI 1\nMOH 650 8\nWAI 1 500\nWAI 1"
STEPS = """<Mix name="Arbitrary step 42" enabled="true">
  <Mixing><Shakes><Shake duration="PT1S" speed="medium" /></Shakes>
  <LoopCount>2</LoopCount></Mixing>
  <CollectBeads enabled="true"><Count>2</Count><CollectTime>PT5S</CollectTime></CollectBeads>
  <Plates><Plate id="plate" /></Plates></Mix>"""
SCRIPT = (
  f'MOR 6\nPAU 1 "plate"\nMOR 1\n{PICKUP}\nTXT 8 "Arbitrary_step_42"\n'
  f'{MIX}\n{MIX}\n{COLLECT}\n{COLLECT}\nTXT "Unloading"\n{RETURN}\n'
  'MOR 6\nPAU 2 "plate"\n'
)


class FlexEventTests(HIDTestCase):
  """Exercise structured events using public execution and explicit resource transfers."""

  async def asyncSetUp(self) -> None:
    """Install deterministic HID and independently named PLR resources."""
    self.mock_hid()
    self.flex = KingFisherFlex(name="flex_left", timeout=5, operation_timeout=5)
    self.flex.io = self.io
    self.physical_flex = self.flex
    self.bus = EventBus()
    self.events: list[PLREvent] = []
    self.bus.subscribe(self.events.append)
    self.protocol = read_flex_bdz(bdz_bytes(SCRIPT, steps=STEPS)).bind_plates(self.plate_bindings)

  async def asyncTearDown(self) -> None:
    """Drain the reader even after assertion failures."""
    await self.flex.stop()

  async def ready(self) -> None:
    """Prepare simulation independently of any observation scope."""
    await self.flex.setup()
    await self.flex.initialize()
    self.flex.reconcile_inventory()

  async def transfer(self, action: FlexAction) -> bool:
    """Perform only the fixture's modeled load/unload and explicitly confirm."""
    self.complete_transfer(action)
    return True

  def named(self, name: str) -> list[PLREvent]:
    """Select one fully qualified event name without depending on unrelated producers."""
    return [e for e in self.events if e.name == f"kingfisher.{name}"]

  async def test_lifecycle_resources_and_arbitrary_step_names(self) -> None:
    """Publish correlated operations, original BDZ names, loops and real model locations."""
    with use_event_bus(self.bus), event_context(run_id="run-42"):
      await self.ready()
      await self.flex.run_protocol(self.protocol, handle_action=self.transfer)
      await self.flex.stop()
    counts = Counter(e.name for e in self.events)
    for operation, count in [
      ("setup", 1),
      ("initialize", 1),
      ("stop", 1),
      ("run_protocol", 1),
      ("load", 1),
      ("unload", 1),
      ("step", 1),
      ("mix", 2),
      ("collect_beads", 2),
      ("pick_up_tip_comb", 1),
      ("return_tip_comb", 1),
      ("rotate", 4),
    ]:
      self.assertEqual(counts[f"kingfisher.{operation}.started"], count)
      self.assertEqual(counts[f"kingfisher.{operation}.completed"], count)
      self.assertEqual(counts[f"kingfisher.{operation}.failed"], 0)
    starts = {e.context["operation_id"]: e for e in self.events if e.name.endswith(".started")}
    for event in self.events:
      json.dumps(event.as_dict())
      self.assertEqual(event.context["run_id"], "run-42")
      if event.name.endswith(".completed"):
        start = starts[event.context["operation_id"]]
        self.assertEqual(start.name.removesuffix("started"), event.name.removesuffix("completed"))
    self.assertEqual(self.named("setup.completed")[0].data["serial_number"], "fixture")
    mixes = self.named("mix.started")
    self.assertEqual([e.data["iteration"] for e in mixes], [1, 2])
    self.assertEqual(mixes[0].data["step_name"], "Arbitrary step 42")
    self.assertEqual(mixes[0].data["resources"][0]["name"], "fixture_plate")
    self.assertEqual([e.data["cycle_index"] for e in self.named("collect_beads.completed")], [1, 2])
    self.assertIsNone(self.named("load.started")[0].data["plate_location"])
    loaded = self.named("load.completed")[0]
    self.assertEqual(loaded.data["plate_location"]["name"], "flex_left_tray_1")
    self.assertTrue(loaded.data["inventory_verified"])
    self.assertIsNone(self.named("unload.completed")[0].data["plate_location"])
    rotation = self.named("rotate.started")[2]
    self.assertEqual(rotation.data["target_processing_slot"], 1)
    self.assertEqual(rotation.data["processing"]["expected_plate"]["name"], "fixture_plate")
    self.assertEqual(rotation.data["processing"]["actual_plate"]["name"], "fixture_plate")
    before_load = self.named("rotate.started")[1].data
    self.assertIsNone(before_load["loading"]["expected_plate"])
    self.assertIsNone(before_load["loading"]["actual_plate"])
    self.assertEqual(before_load["requested_plate"]["name"], "fixture_plate")

  async def test_mix_ack_and_temperature_are_not_completion(self) -> None:
    """Reader-task observations retain the run scope, and ACK cannot finish mixing."""
    await self.ready()
    self.responses[MIX] = "MOH\r\nTEM 24 22\r\n"
    with use_event_bus(self.bus), event_context(run_id="second-scope"):
      task = asyncio.create_task(self.flex.run_protocol(self.protocol, handle_action=self.transfer))
      try:
        await self.wait_sent(MIX)

        # Yield to the reader; queue receipt and callback dispatch are separate operations.
        async def observed_temperature() -> None:
          """Wait for the deterministic reader callback, bounded by the caller."""
          while not self.named("temperature_report"):
            await asyncio.sleep(0)

        await asyncio.wait_for(observed_temperature(), 2)
        self.assertFalse(task.done())
        self.assertFalse(self.named("mix.completed"))
        telemetry = self.named("temperature_report")[0]
        self.assertEqual(telemetry.context["run_id"], "second-scope")
        self.assertEqual(
          telemetry.context["operation_id"], self.named("mix.started")[0].context["operation_id"]
        )
        self.responses.pop(MIX)
        self.emit("MOH END 0\r\n")
        await task
      finally:
        if not task.done():
          task.cancel()
          await asyncio.gather(task, return_exceptions=True)

  async def test_wrong_inventory_emits_failed_action(self) -> None:
    """Handler confirmation without the requested transfer is never a completed load."""
    await self.ready()

    async def incorrect_handler(action: FlexAction) -> bool:
      """Confirm without performing the modeled transfer."""
      return True

    with use_event_bus(self.bus), self.assertRaises(FlexError):
      await self.flex.run_protocol(self.protocol, handle_action=incorrect_handler)
    self.assertFalse(self.named("load.completed"))
    self.assertEqual(len(self.named("load.failed")), 1)
    self.assertEqual(len(self.named("run_protocol.failed")), 1)
    self.assertEqual(self.named("load.failed")[0].data["error_type"], "FlexError")

  async def test_tip_failure_unwinds_without_success(self) -> None:
    """A pickup terminal error produces a failed pickup and failed enclosing run."""
    await self.ready()
    self.responses["MOM -130"] = "MOM\r\nMOM END 13\r\n"
    with use_event_bus(self.bus), self.assertRaises(FlexError):
      await self.flex.run_protocol(self.protocol, handle_action=self.transfer)
    self.assertEqual(len(self.named("pick_up_tip_comb.failed")), 1)
    self.assertFalse(self.named("pick_up_tip_comb.completed"))
    self.assertFalse(self.named("step.started"))
    self.assertEqual(len(self.named("run_protocol.failed")), 1)

  async def test_cancellation_fails_open_spans(self) -> None:
    """Cancellation closes the mix, step and run as failed without swallowing it."""
    await self.ready()
    self.responses[MIX] = "MOH\r\n"
    with use_event_bus(self.bus):
      task = asyncio.create_task(self.flex.run_protocol(self.protocol, handle_action=self.transfer))
      await self.wait_sent(MIX)
      task.cancel()
      with self.assertRaises(asyncio.CancelledError):
        await task
    for operation in ("mix", "step", "run_protocol"):
      self.assertEqual(len(self.named(f"{operation}.failed")), 1)
      self.assertFalse(self.named(f"{operation}.completed"))

  async def test_observer_failure_does_not_change_commands(self) -> None:
    """A broken subscriber cannot authorize, interrupt, or modify execution."""
    await self.ready()
    await self.flex.run_protocol(self.protocol, handle_action=self.transfer)
    first = list(self.commands)
    self.commands.clear()
    self.flex.reconcile_inventory()

    def broken(event: PLREvent) -> None:
      """Simulate an unavailable dashboard subscriber."""
      raise RuntimeError("dashboard unavailable")

    self.bus.subscribe(broken)
    with use_event_bus(self.bus), self.assertLogs("pylabrobot.events.bus", level="ERROR"):
      await self.flex.run_protocol(self.protocol, handle_action=self.transfer)
    self.assertEqual(first[-len(self.commands) :], self.commands)
    self.assertEqual(len(self.named("run_protocol.completed")), 1)

  async def test_rotation_reports_distinct_expected_and_actual(self) -> None:
    """Unknown initialization inventory remains visible; events do not relocate plates."""
    await self.ready()
    plate = self.plate_bindings["plate"]
    self.flex.carousel[8].assign_child_resource(plate)
    expected = (None,) * 8
    with use_event_bus(self.bus):
      await self.flex._rotate(FlexCommand(1, "MOR 8"), expected)
    event = self.named("rotate.started")[0]
    self.assertIsNone(event.data["processing"]["expected_plate"])
    self.assertEqual(event.data["processing"]["actual_plate"]["name"], plate.name)
    self.assertEqual(event.data["target_loading_slot"], 3)
    self.assertIs(plate.parent, self.flex.carousel[8])

  async def test_rotation_ack_does_not_publish_completion(self) -> None:
    """Expose intended orientation at dispatch and confirmed orientation only at END."""
    await self.ready()
    self.responses["MOR 8"] = "MOR\r\n"
    with use_event_bus(self.bus):
      task = asyncio.create_task(self.flex._rotate(FlexCommand(1, "MOR 8"), (None,) * 8))
      try:
        await self.wait_sent("MOR 8")
        self.assertEqual(len(self.named("rotate.started")), 1)
        self.assertFalse(self.named("rotate.completed"))
        self.assertIsNone(self.flex.processing_slot)
        self.emit("MOR END 0\r\n")
        await task
        self.assertEqual(len(self.named("rotate.completed")), 1)
        self.assertEqual(self.flex.processing_slot, 8)
      finally:
        if not task.done():
          task.cancel()
          await asyncio.gather(task, return_exceptions=True)

  async def test_intervention_host_and_physical_modes(self) -> None:
    """The same action lifecycle covers host fulfillment and physical-Start fulfillment."""
    script = 'MOR 6\nPAU 1 "plate"\nTXT 10 "inspect"\nPAU 0 "check"\nPAU 2 "plate"\n'
    steps = '<Pause name="inspect"><Plates><Plate id="plate" /></Plates></Pause>'
    protocol = read_flex_bdz(bdz_bytes(script, steps=steps)).bind_plates(self.plate_bindings)
    for mode in ("host", "instrument"):
      with self.subTest(mode=mode):
        await self.ready()
        self.events.clear()

        async def handle(action: FlexAction) -> bool:
          """Model host actions or await the simulator's physical confirmation."""
          if mode == "instrument":
            await self.flex.wait_for_physical_start(action)
          else:
            self.complete_transfer(action)
          return True

        with use_event_bus(self.bus):
          await self.flex.run_protocol(protocol, handle_action=handle, acknowledgement=mode)
        self.assertEqual(len(self.named("pause.started")), 1)
        self.assertEqual(len(self.named("pause.completed")), 1)
        self.assertEqual(self.named("pause.completed")[0].data["acknowledgement"], mode)

  async def test_preflight_failure_has_no_hardware_effect(self) -> None:
    """Invalid execution requests still have a failed run lifecycle before I/O."""
    await self.ready()
    before = list(self.commands)
    protocol = read_flex_bdz(bdz_bytes("UNKNOWN 1\n")).bind_plates(self.plate_bindings)
    with use_event_bus(self.bus), self.assertRaises(ValueError):
      await self.flex.run_protocol(protocol, handle_action=self.transfer)
    self.assertEqual(self.commands, before)
    self.assertEqual(len(self.named("run_protocol.started")), 1)
    self.assertEqual(len(self.named("run_protocol.failed")), 1)
