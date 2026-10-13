from collections.abc import Callable
from pathlib import Path

import pytest
import torch

from pocket_tts.models.model_state import _import_model_state, export_model_state
from reader import voices as voices_module
from reader.tests.conftest import FakeModel
from reader.voices import VoiceError, VoiceStore, encode_voice


def write_bytes(content: bytes) -> Callable[[Path], None]:
    def make_state(dest: Path) -> None:
        dest.write_bytes(content)

    return make_state


@pytest.fixture
def store(tmp_path: Path) -> VoiceStore:
    return VoiceStore(tmp_path / "voices")


def test_a_new_store_has_no_voices_even_before_its_folder_exists(store: VoiceStore):
    assert store.list_voices() == []


def test_an_added_voice_is_listed_and_its_state_is_on_disk(store: VoiceStore):
    voice = store.add("Grandma", write_bytes(b"state"))
    assert [(v.id, v.name) for v in store.list_voices()] == [(voice.id, "Grandma")]
    path = store.state_path(voice.id)
    assert path is not None
    assert path.read_bytes() == b"state"
    assert path.suffix == ".safetensors"


def test_voices_survive_a_new_store_on_the_same_folder(store: VoiceStore, tmp_path: Path):
    voice = store.add("Kept", write_bytes(b"x"))
    assert [v.id for v in VoiceStore(tmp_path / "voices").list_voices()] == [voice.id]


def test_the_same_name_twice_gives_two_voices_with_different_ids(store: VoiceStore):
    first = store.add("Echo", write_bytes(b"1"))
    second = store.add("Echo", write_bytes(b"2"))
    assert first.id != second.id
    assert len(store.list_voices()) == 2


def test_voices_are_listed_by_name_ignoring_case(store: VoiceStore):
    for name in ("banana", "Apple", "cherry"):
        store.add(name, write_bytes(b"x"))
    assert [v.name for v in store.list_voices()] == ["Apple", "banana", "cherry"]


def test_a_name_is_trimmed_and_inner_whitespace_collapsed(store: VoiceStore):
    assert store.add("  My \n  voice  ", write_bytes(b"x")).name == "My voice"


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_a_blank_name_is_refused(store: VoiceStore, name: str):
    with pytest.raises(VoiceError, match="name"):
        store.add(name, write_bytes(b"x"))
    assert store.list_voices() == []


def test_a_name_over_the_limit_is_refused_and_one_at_the_limit_is_kept(store: VoiceStore):
    with pytest.raises(VoiceError, match="40"):
        store.add("x" * 41, write_bytes(b"x"))
    assert store.add("x" * 40, write_bytes(b"x")).name == "x" * 40


def test_the_voice_cap_is_enforced(store: VoiceStore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(voices_module, "MAX_VOICES", 2)
    store.add("one", write_bytes(b"x"))
    store.add("two", write_bytes(b"x"))
    with pytest.raises(VoiceError, match="Delete one"):
        store.add("three", write_bytes(b"x"))


def test_a_failed_conversion_leaves_nothing_behind(store: VoiceStore, tmp_path: Path):
    def broken(dest: Path) -> None:
        dest.write_bytes(b"half")
        raise VoiceError("could not convert")

    with pytest.raises(VoiceError, match="could not convert"):
        store.add("Broken", broken)
    assert store.list_voices() == []
    assert list((tmp_path / "voices").iterdir()) == []


def test_delete_removes_both_files_and_reports_whether_it_existed(
    store: VoiceStore, tmp_path: Path
):
    voice = store.add("Gone", write_bytes(b"x"))
    assert store.delete(voice.id) is True
    assert store.state_path(voice.id) is None
    assert store.list_voices() == []
    assert list((tmp_path / "voices").iterdir()) == []
    assert store.delete(voice.id) is False


@pytest.mark.parametrize(
    "voice_id", ["", "..", "../secret", "a/b", "/etc/passwd", "secret.safetensors", "a b"]
)
def test_an_id_that_could_escape_the_folder_never_resolves(
    store: VoiceStore, tmp_path: Path, voice_id: str
):
    # A real voice-shaped pair sits next to the store folder; traversal must not reach it.
    (tmp_path / "secret.safetensors").write_bytes(b"x")
    (tmp_path / "secret.json").write_text('{"name": "Secret"}')
    store.add("Real", write_bytes(b"x"))
    assert store.state_path(voice_id) is None
    assert store.delete(voice_id) is False
    assert (tmp_path / "secret.safetensors").exists()


def test_a_state_file_without_its_name_file_is_not_a_voice(store: VoiceStore, tmp_path: Path):
    voice = store.add("Half", write_bytes(b"x"))
    (tmp_path / "voices" / f"{voice.id}.json").unlink()
    assert store.list_voices() == []
    assert store.state_path(voice.id) is None


def test_a_corrupt_name_file_is_skipped_not_fatal(store: VoiceStore, tmp_path: Path):
    good = store.add("Good", write_bytes(b"x"))
    bad = store.add("Bad", write_bytes(b"x"))
    (tmp_path / "voices" / f"{bad.id}.json").write_text("{not json")
    assert [v.id for v in store.list_voices()] == [good.id]


class BrokenAudioModel(FakeModel):
    def get_state_for_audio_prompt(self, audio_conditioning: Path, truncate: bool = False):
        raise RuntimeError("cannot decode")


class NoCloningModel(FakeModel):
    has_voice_cloning = False


def a_voice_state_file(path: Path) -> Path:
    export_model_state({"fake": {"voice": torch.ones(3)}}, path)
    return path


def test_an_audio_clip_becomes_a_voice_state_made_from_its_first_30_seconds(tmp_path: Path):
    model = FakeModel()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"audio")
    dest = tmp_path / "out.part"
    encode_voice(model, clip, dest)
    assert model.prompts == [("clip.wav", True)]
    assert "fake" in _import_model_state(dest, torch.device("cpu"))


def test_a_voice_state_is_validated_by_loading_then_copied_unchanged(tmp_path: Path):
    model = FakeModel()
    source = a_voice_state_file(tmp_path / "mine.safetensors")
    dest = tmp_path / "out.part"
    encode_voice(model, source, dest)
    assert model.prompts == [("mine.safetensors", False)]
    assert dest.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("content", [b"", b"this is not a voice"])
def test_a_damaged_voice_state_is_refused_with_the_reason(tmp_path: Path, content: bytes):
    source = tmp_path / "bad.safetensors"
    source.write_bytes(content)
    with pytest.raises(VoiceError, match="not a voice this reader can use"):
        encode_voice(FakeModel(), source, tmp_path / "out.part")
    assert not (tmp_path / "out.part").exists()


def test_audio_that_cannot_be_decoded_is_a_plain_error(tmp_path: Path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"")
    with pytest.raises(VoiceError, match="Could not read that audio"):
        encode_voice(BrokenAudioModel(), clip, tmp_path / "out.part")


def test_a_model_without_voice_cloning_still_accepts_voice_states(tmp_path: Path):
    model = NoCloningModel()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"audio")
    with pytest.raises(VoiceError, match="cannot make a voice from audio"):
        encode_voice(model, clip, tmp_path / "out.part")
    source = a_voice_state_file(tmp_path / "mine.safetensors")
    encode_voice(model, source, tmp_path / "out2.part")
    assert (tmp_path / "out2.part").exists()


def test_an_empty_voice_state_is_refused(tmp_path: Path):
    from safetensors.torch import save_file

    source = tmp_path / "empty.safetensors"
    save_file({}, str(source))
    with pytest.raises(VoiceError, match="not a voice this reader can use"):
        encode_voice(FakeModel(), source, tmp_path / "out.part")
    assert not (tmp_path / "out.part").exists()
