"""WAV files the model's reader must accept (voice clips come from many different apps)."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from pocket_tts.data.audio import audio_read

RATE = 24000
TONE = (0.3 * np.sin(2 * np.pi * 220 * np.arange(RATE) / RATE)).astype(np.float32)


@pytest.mark.parametrize("subtype", ["PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"])
def test_wav_files_of_every_common_sample_type_are_read(tmp_path: Path, subtype: str):
    path = tmp_path / "clip.wav"
    sf.write(path, TONE, RATE, subtype=subtype)
    audio, rate = audio_read(path)
    assert rate == RATE
    assert audio.shape == (1, RATE)
    assert np.allclose(audio[0].numpy(), TONE, atol=1e-3)


def test_a_stereo_float_wav_is_mixed_down_to_one_channel(tmp_path: Path):
    path = tmp_path / "stereo.wav"
    sf.write(path, np.stack([TONE, TONE], axis=1), RATE, subtype="FLOAT")
    audio, _ = audio_read(path)
    assert audio.shape == (1, RATE)
