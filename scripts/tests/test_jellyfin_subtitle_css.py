import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "jellyfin_subtitle_css.py"
  spec = importlib.util.spec_from_file_location("jellyfin_subtitle_css", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


jsc = _load_module()


# ---- render_block --------------------------------------------------------


def test_render_block_contains_geometry_and_sentinels():
  block = jsc.render_block(92, 1.25)
  assert block.startswith(jsc.BLOCK_START)
  assert block.endswith(jsc.BLOCK_END)
  assert "max-width: 92%;" in block
  assert "line-height: 1.25;" in block
  assert ".videoSubtitlesInner {" in block


# ---- merge_block ---------------------------------------------------------


def test_merge_into_empty_css_yields_block_only():
  block = jsc.render_block(92, 1.25)
  assert jsc.merge_block("", block) == block


def test_merge_preserves_foreign_css():
  foreign = ".skinHeader { display: none; }"
  block = jsc.render_block(92, 1.25)
  merged = jsc.merge_block(foreign, block)
  assert merged.startswith(foreign)
  assert block in merged


def test_merge_is_idempotent():
  block = jsc.render_block(92, 1.25)
  once = jsc.merge_block(".foo { color: red; }", block)
  twice = jsc.merge_block(once, block)
  assert once == twice
  assert once.count(jsc.BLOCK_START) == 1


def test_merge_replaces_existing_block_with_new_geometry():
  old = jsc.merge_block(".foo { color: red; }", jsc.render_block(92, 1.25))
  new = jsc.merge_block(old, jsc.render_block(96, 1.1))
  assert "max-width: 96%;" in new
  assert "max-width: 92%;" not in new
  assert new.count(jsc.BLOCK_START) == 1
  assert ".foo { color: red; }" in new


def test_merge_keeps_foreign_css_written_after_the_block():
  block = jsc.render_block(92, 1.25)
  css = f"{block}\n\n.trailing {{ color: blue; }}"
  merged = jsc.merge_block(css, jsc.render_block(96, 1.1))
  assert ".trailing { color: blue; }" in merged
  assert merged.count(jsc.BLOCK_START) == 1


# ---- strip_block ---------------------------------------------------------


def test_strip_block_on_untouched_css_is_a_noop():
  css = ".skinHeader { display: none; }"
  assert jsc.strip_block(css) == css


def test_strip_block_removes_block_and_keeps_neighbours():
  block = jsc.render_block(92, 1.25)
  css = f".before {{ a: 1; }}\n\n{block}\n\n.after {{ b: 2; }}"
  stripped = jsc.strip_block(css)
  assert jsc.BLOCK_START not in stripped
  assert ".videoSubtitlesInner" not in stripped
  assert ".before { a: 1; }" in stripped
  assert ".after { b: 2; }" in stripped


def test_strip_block_leaves_empty_string_when_block_was_all_there_was():
  assert jsc.strip_block(jsc.render_block(92, 1.25)) == ""


def test_strip_block_handles_missing_end_sentinel():
  css = f".before {{ a: 1; }}\n{jsc.BLOCK_START}\n.videoSubtitlesInner {{ max-width: 92%; }}"
  stripped = jsc.strip_block(css)
  assert stripped == ".before { a: 1; }"
