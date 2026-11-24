"""Read-only semantic annotations for exported Flex command sequences.

Annotations never generate commands or authorize execution. Unknown sequences retain
ordinary execution and raw script labels without inventing a semantic outcome.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from pylabrobot.thermo_fisher.kingfisher.bdz_container import parse_xml

from .protocol import FlexCommand, FlexProtocol


@dataclass(frozen=True)
class ProgressSpan:
  """An inclusive command interval with an independently correlated lifecycle."""

  operation: str
  first_line: int
  last_line: int
  plate_id: Optional[str] = None
  details: dict[str, Any] = field(default_factory=dict)


def _duration(text: str) -> Optional[int]:
  """Read BindIt time durations as hundredths of a second without rounding."""
  if len(text) > 64:
    return None
  match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text)
  if match is None:
    return None
  hours, minutes, seconds = match.groups()
  ticks = (int(hours or 0) * 3600 + int(minutes or 0) * 60) * 100 + Decimal(seconds or "0") * 100
  return int(ticks) if ticks == int(ticks) else None


def _mix_phases(step: ET.Element, limit: int) -> list[tuple[str, int, Optional[int]]]:
  """Describe timed motion phases; pause presets are waits rather than mixing."""
  phases: list[tuple[str, int, Optional[int]]] = []

  def add(element: Optional[ET.Element], phase: str) -> None:
    """Include a positive, explicitly enabled release or postmix duration."""
    if element is not None and element.get("enabled", "true") == "true":
      ticks = _duration(element.findtext("Duration", ""))
      if ticks:
        phases.append((phase, ticks, None))

  if step.tag == "ReleaseBeads":
    add(step, "release")
  elif step.tag == "Mix":
    add(step.find("ReleaseBeads"), "release")
    count = step.findtext("Mixing/LoopCount", "1")
    # Bound annotation work independently of untrusted XML metadata.
    if len(count) > 5 or not count.isdecimal() or not 1 <= int(count) <= 10000:
      return []
    shakes = []
    for shake in step.findall("Mixing/Shakes/Shake"):
      ticks = _duration(shake.get("duration", ""))
      if ticks and shake.get("speed") != "ffffffff-ffff-ffff-ffff-ffffffffffff":
        shakes.append(ticks)
    if len(phases) + len(shakes) * int(count) > limit:
      return []
    for iteration in range(1, int(count) + 1):
      phases.extend(("mixing", ticks, iteration) for ticks in shakes)
    add(step.find("Postmix"), "postmix")
  return phases


def _substeps(
  step: ET.Element, commands: tuple[FlexCommand, ...], plate_id: Optional[str], details: dict
) -> list[ProgressSpan]:
  """Recognize timed mixing and the observed 96DW collection-cycle boundaries."""
  spans: list[ProgressSpan] = []
  timed = [c for c in commands if c.opcode == "MOH" and len(c.text.split()) == 6]
  phases = _mix_phases(step, len(timed))
  matched = [int(c.text.split()[4]) for c in timed] == [p[1] for p in phases]
  unmapped = [] if matched else ["mix_phases"]
  for index, command in enumerate(timed):
    phase, _, iteration = phases[index] if matched else ("unspecified", 0, None)
    spans.append(
      ProgressSpan(
        "mix",
        command.line_number,
        command.line_number,
        plate_id,
        {
          **details,
          "phase": phase,
          "iteration": iteration,
          "segment_index": index + 1,
          "duration": int(command.text.split()[4]) / 100,
        },
      )
    )
  collection = step if step.tag == "CollectBeads" else step.find("CollectBeads")
  cycles: list[tuple[FlexCommand, FlexCommand, int]] = []
  for index in range(len(commands) - 6):
    block = commands[index : index + 7]
    words = [c.text for c in block]
    if (
      re.fullmatch(r"MOH \d+ 100", words[0])
      and words[1:5] == ["MOM 460", "WAI 1 150", "WAI 1", "MOH 650 8"]
      and re.fullmatch(r"WAI 1 \d+", words[5])
      and words[6] == "WAI 1"
    ):
      cycles.append((block[0], block[-1], int(words[5].split()[2])))
  if collection is not None and collection.get("enabled", "true") == "true":
    count = collection.findtext("CollectCount" if step.tag == "CollectBeads" else "Count")
    ticks = _duration(collection.findtext("CollectTime", ""))
    if count == str(len(cycles)) and all(c[2] == ticks for c in cycles):
      for index, (first, last, duration) in enumerate(cycles, 1):
        spans.append(
          ProgressSpan(
            "collect_beads",
            first.line_number,
            last.line_number,
            plate_id,
            {
              **details,
              "phase": "collection",
              "cycle_index": index,
              "cycle_count": len(cycles),
              "duration": duration / 100,
            },
          )
        )
    else:
      unmapped.append("collection_cycles")
  if step.find("Precollect[@enabled='true']") is not None:
    for index in range(len(commands) - 2):
      block = commands[index : index + 3]
      if (
        block[0].text == "MOM 460"
        and block[1].text == "MOH 650 8"
        and re.fullmatch(r"MOH \d+ 100", block[2].text)
      ):
        spans.append(
          ProgressSpan(
            "collect_beads",
            block[0].line_number,
            block[-1].line_number,
            plate_id,
            {**details, "phase": "precollection", "cycle_index": 1, "cycle_count": 1},
          )
        )
        break
    else:
      unmapped.append("precollection")
  details["unmapped_features"] = unmapped
  return spans


def progress_spans(protocol: FlexProtocol, commands: tuple[FlexCommand, ...]) -> list[ProgressSpan]:
  """Link arbitrary BDZ step names to script markers and supported motion patterns.

  Step indices identify metadata order, including duplicate names. Marker matches
  must agree in both type and name. Tip patterns require matching Tip metadata and
  its pickup/return tray; an isolated magnet command is never called a tip transfer.
  """
  root = parse_xml(protocol.exported_xml, "ExportedData")
  steps: list[ET.Element] = []

  metadata = root.find("./Protocol/Run/Protocol")
  if metadata is None:
    return []
  pending = list(reversed(metadata.findall("Steps/*")))
  while pending:
    step = pending.pop()
    if step.get("enabled", "true") != "true":
      continue
    if step.tag != "Tip":
      steps.append(step)
    pending.extend(reversed(step.findall("Steps/*")))
  codes = {
    "Mix": {"6", "7", "8"},
    "Dry": {"4"},
    "Pause": {"10", "11"},
    "CollectBeads": {"2"},
    "ReleaseBeads": {"2"},
  }
  typed_markers = {
    i
    for i, c in enumerate(commands)
    if re.fullmatch(r'TXT (?:2|4|6|7|8|10|11) "[^"\r\n]*"', c.text)
  }
  markers = []
  for i, command in enumerate(commands):
    if i in typed_markers:
      markers.append(i)
    elif command.text == 'TXT "Unloading"':
      # A standalone pause may itself be named Unloading. Its plain resume label
      # precedes the next typed step, not an unload action or tip return.
      end = next((j for j in range(i + 1, len(commands)) if j in typed_markers), len(commands))
      if any(c.text.startswith("PAU 2 ") for c in commands[i + 1 : end]):
        markers.append(i)
  spans: list[ProgressSpan] = []
  cursor = 0
  for position, start in enumerate(markers):
    marker = commands[start].text
    if marker == 'TXT "Unloading"':
      continue
    name = marker.split('"')[1]
    code = marker.split()[1]
    matches = [
      i
      for i in range(cursor, len(steps))
      if steps[i].get("name", "").replace(" ", "_") == name.replace(" ", "_")
      and code in codes.get(steps[i].tag, set())
    ]
    if not matches:
      continue
    step_index = matches[0]
    cursor = step_index + 1
    step = steps[step_index]
    end = markers[position + 1] if position + 1 < len(markers) else len(commands)
    while end > start + 1 and commands[end - 1].opcode == "TIM":
      end -= 1
    plate = step.find("Plates/Plate")
    plate_id = plate.get("id") if plate is not None else None
    details = {
      "step_name": step.get("name", ""),
      "script_label": name,
      "step_type": step.tag,
      "step_index": step_index + 1,
    }
    spans.append(
      ProgressSpan(
        "step", commands[start].line_number, commands[end - 1].line_number, plate_id, details
      )
    )
    spans.extend(_substeps(step, commands[start:end], plate_id, details))

  # These are the complete observed 96DW comb pickup/return sequences, not a
  # heuristic based on a negative magnet position that other operations also use.
  patterns = {
    "pick_up_tip_comb": [
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
    ],
    "return_tip_comb": [
      "MOS 0",
      "MOH 230 1800",
      "MOH 650 80",
      "MOM -130",
      "MOH 230 80",
      "MOM 0",
      "MOH 0 1800",
    ],
  }
  slots = {p.label: p.loading_slot for p in protocol.prompts() if p.action == "load"}
  plate_slots = {plate.id: slots.get(plate.name) for plate in protocol.plates}
  tip_index = 0
  orientation: Optional[int] = None
  picked_up = False
  for index, command in enumerate(commands):
    if command.opcode == "MOR":
      orientation = int(command.text.split()[1])
    if tip_index >= len(protocol.tips):
      break
    tip = protocol.tips[tip_index]
    operation = "return_tip_comb" if picked_up else "pick_up_tip_comb"
    pattern = patterns[operation]
    block = commands[index : index + len(pattern)]
    plate_id = tip.plate_ids[-1 if picked_up else 0]
    if orientation == plate_slots.get(plate_id) and [c.text for c in block] == pattern:
      spans.append(
        ProgressSpan(
          operation,
          command.line_number,
          block[-1].line_number,
          plate_id,
          {"tip_name": tip.name, "tip_index": tip_index + 1},
        )
      )
      if picked_up:
        tip_index += 1
      picked_up = not picked_up
  return sorted(spans, key=lambda s: (s.first_line, -s.last_line))
