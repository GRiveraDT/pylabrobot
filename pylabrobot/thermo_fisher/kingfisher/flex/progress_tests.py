"""Conservative BDZ-to-command annotations, independent of instrument execution."""

import unittest

from .events_tests import COLLECT, MIX, PICKUP, RETURN, SCRIPT, STEPS
from .progress import progress_spans
from .protocol import read_flex_bdz
from .test_helpers import bdz_bytes


class ProgressTests(unittest.TestCase):
  """Keep protocol labels and motion evidence separate from inferred presentation."""

  def test_duplicate_names_keep_metadata_order(self) -> None:
    """Distinct steps may use identical names; their indices and loop phases remain distinct."""
    script = f'TXT 8 "Arbitrary_step_42"\n{MIX}\n{MIX}\n{COLLECT}\n{COLLECT}\n'
    protocol = read_flex_bdz(bdz_bytes(script * 2, steps=STEPS * 2))
    spans = progress_spans(protocol, protocol.preflight())
    steps = [s for s in spans if s.operation == "step"]
    self.assertEqual([s.details["step_index"] for s in steps], [1, 2])
    self.assertEqual([s.details["step_name"] for s in steps], ["Arbitrary step 42"] * 2)
    self.assertTrue(all(not s.details["unmapped_features"] for s in steps))

  def test_unknown_collection_pattern_is_not_claimed(self) -> None:
    """Changed collection timing leaves execution intact and explicitly marks partial annotation."""
    protocol = read_flex_bdz(bdz_bytes(SCRIPT.replace("MOH 650 8", "MOH 650 9"), steps=STEPS))
    spans = progress_spans(protocol, protocol.preflight())
    self.assertFalse(any(s.operation == "collect_beads" for s in spans))
    step = next(s for s in spans if s.operation == "step")
    self.assertIn("collection_cycles", step.details["unmapped_features"])

  def test_unmatched_metadata_cannot_absorb_next_step(self) -> None:
    """Unknown script labels remain boundaries instead of borrowing another step's metadata."""
    script = f'TXT 8 "Arbitrary_step_42"\n{MIX}\nTXT 6 "Other"\n{MIX}\n'
    protocol = read_flex_bdz(bdz_bytes(script, steps=STEPS))
    spans = progress_spans(protocol, protocol.preflight())
    self.assertEqual(
      [(s.first_line, s.last_line) for s in spans if s.operation == "step"], [(1, 2)]
    )
    mix = next(s for s in spans if s.operation == "mix")
    self.assertEqual(mix.details["phase"], "unspecified")

  def test_precollection_release_loops_and_postmix(self) -> None:
    """Actual motion segments identify release, repeated mixing and postmix independently."""
    steps = """<Mix name="Elution with spaces">
      <Precollect enabled="true" />
      <ReleaseBeads enabled="true"><Duration>PT2S</Duration></ReleaseBeads>
      <Mixing><Shakes><Shake duration="PT1S" speed="medium" />
      <Shake duration="PT3S" speed="ffffffff-ffff-ffff-ffff-ffffffffffff" />
      </Shakes><LoopCount>2</LoopCount></Mixing>
      <Postmix enabled="true"><Duration>PT4S</Duration></Postmix>
      <Plates><Plate id="plate" /></Plates></Mix>"""
    script = (
      'TXT 6 "Elution_with_spaces"\nMOM 460\nMOH 650 8\nMOH 627 100\n'
      f"MOH 640 200 0 200 0\n{MIX}\nWAI 1 300\nWAI 1\n"
      f"{MIX}\nWAI 1 300\nWAI 1\nMOH 640 20 0 400 0\n"
    )
    protocol = read_flex_bdz(bdz_bytes(script, steps=steps))
    spans = progress_spans(protocol, protocol.preflight())
    self.assertEqual(
      [s.details["phase"] for s in spans if s.operation == "mix"],
      ["release", "mixing", "mixing", "postmix"],
    )
    collection = next(s for s in spans if s.operation == "collect_beads")
    self.assertEqual(collection.details["phase"], "precollection")
    self.assertFalse(spans[0].details["unmapped_features"])

  def test_standalone_collection_and_release(self) -> None:
    """Both standalone step types share a firmware icon but retain distinct metadata."""
    steps = """<CollectBeads name="a"><CollectCount>1</CollectCount>
      <CollectTime>PT5S</CollectTime><Plates><Plate id="plate" /></Plates></CollectBeads>
      <ReleaseBeads name="b"><Duration>PT1S</Duration>
      <Plates><Plate id="plate" /></Plates></ReleaseBeads>"""
    protocol = read_flex_bdz(bdz_bytes(f'TXT 2 "a"\n{COLLECT}\nTXT 2 "b"\n{MIX}\n', steps=steps))
    spans = progress_spans(protocol, protocol.preflight())
    self.assertEqual(
      [s.details["step_type"] for s in spans if s.operation == "step"],
      ["CollectBeads", "ReleaseBeads"],
    )
    self.assertEqual(next(s for s in spans if s.operation == "mix").details["phase"], "release")

  def test_tip_pattern_requires_the_declared_tray(self) -> None:
    """Do not label an isolated magnet command or a sequence on an unrelated tray as pickup."""
    script = f'MOR 6\nPAU 1 "plate"\nMOR 2\n{PICKUP}\n{RETURN}\nMOM -130\n'
    protocol = read_flex_bdz(bdz_bytes(script))
    self.assertFalse(progress_spans(protocol, protocol.preflight()))

  def test_large_loop_metadata_is_bounded_by_exported_commands(self) -> None:
    """Annotation does not expand a large loop into an unbounded synthetic protocol."""
    protocol = read_flex_bdz(
      bdz_bytes(SCRIPT, steps=STEPS.replace("<LoopCount>2", "<LoopCount>10000"))
    )
    spans = progress_spans(protocol, protocol.preflight())
    self.assertEqual(sum(s.operation == "mix" for s in spans), 2)
    self.assertTrue(all(s.details["phase"] == "unspecified" for s in spans if s.operation == "mix"))

  def test_unloading_is_allowed_as_a_step_name(self) -> None:
    """A pause's resume label is not mistaken for the run's unload section."""
    script = (
      'TXT 10 "Unloading"\nMOR 6\nPAU 0 "check"\nTXT "Unloading"\nMOR 1\n'
      f'TXT 8 "Arbitrary_step_42"\n{MIX}\n{MIX}\n'
      'TXT "Unloading"\nMOR 6\nPAU 2 "plate"\n'
    )
    steps = '<Pause name="Unloading"><Plates><Plate id="plate" /></Plates></Pause>' + STEPS
    protocol = read_flex_bdz(bdz_bytes(script, steps=steps))
    spans = progress_spans(protocol, protocol.preflight())
    pause = next(s for s in spans if s.operation == "step")
    self.assertEqual(pause.details["step_name"], "Unloading")
    self.assertEqual(pause.last_line, 5)
