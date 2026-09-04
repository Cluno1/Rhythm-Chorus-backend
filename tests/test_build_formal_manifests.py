import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "build_formal_manifests.py"
SPEC = importlib.util.spec_from_file_location("build_formal_manifests", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
lyrics_from_document = MODULE.lyrics_from_document
names_from_document = MODULE.names_from_document


def test_gmusic_lyrics_text_is_preferred_with_legacy_fallback() -> None:
    assert lyrics_from_document({"lyrics_text": "正式歌词", "lyrics": "旧字段"}) == "正式歌词"
    assert lyrics_from_document({"lyrics": ["第一行", "第二行"]}) == "第一行\n第二行"
    assert lyrics_from_document({}) is None
    assert names_from_document(" Composer ") == ["Composer"]
