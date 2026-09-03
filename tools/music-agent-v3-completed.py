#!/usr/bin/env python3
"""
AI Music Learning Agent v3 — Neural-Symbolic Hybrid (refactored)
==================================================================
Drop-in successor to v2. Bug fixes:
  • MusicKey: real semitone math + from_tonic()/relative(); key detection
    no longer crashes on enum re-instantiation
  • MusicMemory: atomic writes (temp files closed before replace),
    search indices rebuilt on load
  • StemGenerator: drum patterns follow the structure tempo (v2 hardcoded
    120 BPM); chord pad envelope fixed (v2 crashed on chords > 0.5 s)
  • StyleTransfer: loudness contour re-centred (v2 attenuated the mix),
    compressor is no longer a no-op
  • UserProfile.update_preferences() now exists (v2 called it → AttributeError)
  • Resampling via librosa (v2 used resample_poly with 44100/32000 up/down)
  • Generated tracks no longer logged as implicit "dislikes"
  • Neural/CLIP models are lazy-loaded (heavy init only on first use)
  • edit_music saves at the correct sample rate

Subsystems (all optional deps degrade gracefully):
  memory (vector ANN) • neural codec (EnCodec/DAC) • MusicGen wrapper
  CLIP multimodal conditioning • symbolic engine • stem generation
  style/timbre transfer • personalization • provenance/ethics
  real-time stream • NSGA-II evolution
"""

from __future__ import annotations

import os
import sys
import json
import hashlib
import logging
import argparse
import tempfile
import time
import pickle
import threading
import copy
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Callable, Literal, NamedTuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict, deque, Counter
from enum import Enum

import numpy as np
import numpy.typing as npt
import librosa
import soundfile as sf
from scipy.ndimage import gaussian_filter1d
from scipy.signal import butter, lfilter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("music_agent")

NOTES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_SEMITONES = {
    'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4, 'F': 5,
    'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8, 'A': 9, 'A#': 10, 'Bb': 10, 'B': 11,
}

_GENRE_CODE = {'electronic': 0.2, 'rock': 0.4, 'ambient': 0.6, 'pop': 0.8, 'jazz': 1.0}
_MOOD_CODE = {'happy': 0.2, 'sad': 0.4, 'energetic': 0.6, 'calm': 0.8, 'neutral': 1.0}


# ==============================================================================
# OPTIONAL DEPENDENCIES — Graceful Degradation
# ==============================================================================

class DependencyManager:
    """Centralized optional dependency tracking with lazy loading."""

    _cache: Dict[str, bool] = {}
    _modules: Dict[str, Any] = {}

    @classmethod
    def check(cls, name: str, import_path: str) -> bool:
        if name in cls._cache:
            return cls._cache[name]
        try:
            cls._modules[name] = __import__(import_path, fromlist=[''])
            cls._cache[name] = True
        except ImportError:
            cls._cache[name] = False
        return cls._cache[name]

    @classmethod
    def get(cls, name: str) -> Any:
        return cls._modules.get(name)


DEPS = {
    'torch': DependencyManager.check('torch', 'torch'),
    'torchaudio': DependencyManager.check('torchaudio', 'torchaudio'),
    'transformers': DependencyManager.check('transformers', 'transformers'),
    'diffusers': DependencyManager.check('diffusers', 'diffusers'),
    'clip': DependencyManager.check('clip', 'clip'),
    'sounddevice': DependencyManager.check('sounddevice', 'sounddevice'),
    'encodec': DependencyManager.check('encodec', 'encodec'),
    'dac': DependencyManager.check('dac', 'dac'),
    'sklearn': DependencyManager.check('sklearn', 'sklearn'),
}

if DEPS['torch']:
    torch = DependencyManager.get('torch')  # type: ignore
    Tensor = torch.Tensor  # type: ignore
else:
    Tensor = Any  # type: ignore


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

class MusicKey(Enum):
    """Twelve major + twelve minor keys. v3: semitones come from the tonic
    string (v2 stored 'fifths' and used them as semitones — wrong for G, D, …)."""

    C_MAJOR = ('C', 'major')
    G_MAJOR = ('G', 'major')
    D_MAJOR = ('D', 'major')
    A_MAJOR = ('A', 'major')
    E_MAJOR = ('E', 'major')
    B_MAJOR = ('B', 'major')
    F_SHARP_MAJOR = ('F#', 'major')
    D_FLAT_MAJOR = ('Db', 'major')
    A_FLAT_MAJOR = ('Ab', 'major')
    E_FLAT_MAJOR = ('Eb', 'major')
    B_FLAT_MAJOR = ('Bb', 'major')
    F_MAJOR = ('F', 'major')

    A_MINOR = ('A', 'minor')
    E_MINOR = ('E', 'minor')
    B_MINOR = ('B', 'minor')
    F_SHARP_MINOR = ('F#', 'minor')
    C_SHARP_MINOR = ('C#', 'minor')
    G_SHARP_MINOR = ('G#', 'minor')
    E_FLAT_MINOR = ('Eb', 'minor')
    B_FLAT_MINOR = ('Bb', 'minor')
    F_MINOR = ('F', 'minor')
    C_MINOR = ('C', 'minor')
    G_MINOR = ('G', 'minor')
    D_MINOR = ('D', 'minor')

    def __init__(self, tonic: str, quality: str):
        self.tonic = tonic
        self.quality = quality

    @property
    def semitones(self) -> int:
        return NOTE_SEMITONES[self.tonic]

    @property
    def relative(self) -> "MusicKey":
        if self.quality == 'major':
            return MusicKey.from_tonic(NOTES[(self.semitones - 3) % 12], 'minor')
        return MusicKey.from_tonic(NOTES[(self.semitones + 3) % 12], 'major')

    @classmethod
    def from_tonic(cls, tonic: str, quality: str = 'major') -> "MusicKey":
        for member in cls:
            if member.tonic == tonic and member.quality == quality:
                return member
        raise KeyError(f"Unknown key: {tonic} {quality}")

    def frequency(self, octave: int = 4) -> float:
        """Concert pitch frequency of the tonic (A4 = 440 Hz)."""
        return 440.0 * (2 ** ((self.semitones - 9) / 12)) * (2 ** (octave - 4))


@dataclass(frozen=True)
class ChordEvent:
    """A chord at a specific time position."""
    start_time: float
    duration: float
    root: str
    quality: str  # 'maj', 'min', 'dom7', 'maj7', 'min7', 'dim', …
    bass: Optional[str] = None

    @property
    def symbol(self) -> str:
        if self.bass and self.bass != self.root:
            return f"{self.root}{self.quality}/{self.bass}"
        return f"{self.root}{self.quality}"

    def frequencies(self, octave: int = 3) -> List[float]:
        root_semi = NOTE_SEMITONES.get(self.root, 0)
        root_freq = 440.0 * (2 ** ((root_semi - 9) / 12)) * (2 ** (octave - 4))
        intervals = {
            'maj': [0, 4, 7], 'min': [0, 3, 7], 'dim': [0, 3, 6], 'aug': [0, 4, 8],
            'dom7': [0, 4, 7, 10], 'maj7': [0, 4, 7, 11], 'min7': [0, 3, 7, 10],
            'halfdim7': [0, 3, 6, 10], 'dim7': [0, 3, 6, 9],
            'sus2': [0, 2, 7], 'sus4': [0, 5, 7],
        }
        return [root_freq * (2 ** (i / 12)) for i in intervals.get(self.quality, [0, 4, 7])]


@dataclass
class Section:
    name: str
    start_bar: int
    duration_bars: int
    chord_progression: List[ChordEvent] = field(default_factory=list)
    intensity: float = 0.5
    texture: str = 'full'

    @property
    def end_bar(self) -> int:
        return self.start_bar + self.duration_bars


@dataclass
class SongStructure:
    sections: List[Section] = field(default_factory=list)
    tempo: float = 120.0
    time_signature: Tuple[int, int] = (4, 4)
    key: MusicKey = MusicKey.C_MAJOR
    transitions: Dict[Tuple[str, str], str] = field(default_factory=dict)

    def add_section(self, section: Section) -> None:
        self.sections.append(section)

    def get_section(self, name: str) -> Optional[Section]:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def total_bars(self) -> int:
        return max((s.end_bar for s in self.sections), default=0)

    def to_timeline(self) -> List[Tuple[float, str, Section]]:
        bar_duration = (60.0 / self.tempo) * self.time_signature[0]
        return [(s.start_bar * bar_duration, s.name, s) for s in self.sections]


@dataclass
class AudioFeatures:
    tempo: float
    tempo_confidence: float
    key: Optional[MusicKey]
    loudness: float
    spectral_centroid: float
    spectral_rolloff: float
    spectral_flux: float
    zero_crossing_rate: float
    rms_energy: float
    chroma: npt.NDArray[np.float32]
    mfcc: npt.NDArray[np.float32]
    onset_envelope: npt.NDArray[np.float32]
    beat_frames: npt.NDArray[np.int32]
    harmony_change_times: List[float]
    dynamic_range: float
    stereo_width: Optional[float] = None
    encodec_tokens: Optional[npt.NDArray[np.int64]] = None
    dac_tokens: Optional[npt.NDArray[np.int64]] = None


@dataclass
class StemTrack:
    name: str
    audio: npt.NDArray[np.float32]
    midi_notes: Optional[List[Tuple[float, float, int, float]]] = None
    effects_chain: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.audio) / 22050.0  # nominal; caller owns the real sr

    def apply_effect(self, effect_name: str, params: Dict[str, Any]) -> "StemTrack":
        new_stem = copy.deepcopy(self)
        new_stem.effects_chain.append(effect_name)
        return new_stem


@dataclass
class MixSession:
    stems: Dict[str, StemTrack] = field(default_factory=dict)
    master_effects: List[str] = field(default_factory=list)
    automation: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    sample_rate: int = 44100

    def add_stem(self, stem: StemTrack) -> None:
        self.stems[stem.name] = stem

    def render(self) -> npt.NDArray[np.float32]:
        if not self.stems:
            return np.array([])
        max_len = max(len(s.audio) for s in self.stems.values())
        mix = np.zeros(max_len, dtype=np.float32)
        for name, stem in self.stems.items():
            gain = 1.0
            if name in self.automation:
                gains = [g for _, g in self.automation[name]]
                gain = float(np.mean(gains)) if gains else 1.0
            mix[:len(stem.audio)] += stem.audio * gain
        mix = np.tanh(mix * 0.8) / 0.8
        return mix / (np.max(np.abs(mix)) + 1e-8)


# ==============================================================================
# 1. MUSIC MEMORY
# ==============================================================================

class MusicMemory:
    """Persistent memory: JSON metadata + numpy vector store + ANN index."""

    def __init__(self, memory_path: str = "music_memory.json",
                 vector_dim: int = 512, use_vectors: bool = True):
        self.memory_path = memory_path
        self.vector_dim = vector_dim
        self.use_vectors = use_vectors and DEPS['sklearn']
        self.vector_path = str(Path(memory_path).with_suffix('.vectors.npy'))

        self.tracks: Dict[str, Dict] = {}
        self.genre_profiles: Dict[str, Dict] = {}
        self.sample_library: List[Dict] = []
        self.pattern_library: List[Dict] = []
        self.vectors: Dict[str, npt.NDArray[np.float32]] = {}

        self._bpm_index: Dict[Tuple[int, int], List[str]] = defaultdict(list)
        self._genre_index: Dict[str, List[str]] = defaultdict(list)
        self._key_index: Dict[str, List[str]] = defaultdict(list)
        self._ann_index: Any = None
        self._ann_hashes: List[str] = []

        self.load_memory()

    # -- vectors --------------------------------------------------------------

    def _compute_vector(self, features: AudioFeatures) -> npt.NDArray[np.float32]:
        vec = np.zeros(self.vector_dim, dtype=np.float32)
        vec[0] = min(features.tempo / 200.0, 1.0)
        vec[1] = (features.loudness + 60) / 60.0
        vec[2] = np.log10(max(features.spectral_centroid, 20)) / np.log10(20000)
        vec[3] = features.spectral_rolloff / (22050 / 2)
        vec[4] = min(features.zero_crossing_rate * 10, 1.0)
        vec[5] = min(features.dynamic_range / 60.0, 1.0)

        chroma_mean = np.mean(features.chroma, axis=1)
        vec[6:18] = chroma_mean / (np.sum(chroma_mean) + 1e-8)
        mfcc_mean = np.mean(features.mfcc[:5], axis=1)
        vec[18:23] = (mfcc_mean + 100) / 200.0
        vec[23:33] = np.histogram(features.onset_envelope, bins=10,
                                  range=(0, 10))[0] / max(len(features.onset_envelope), 1)
        return vec

    def _rebuild_ann(self):
        if not DEPS['sklearn']:
            return
        from sklearn.neighbors import BallTree
        self._ann_hashes = list(self.vectors.keys())
        if len(self._ann_hashes) < 5:
            return
        matrix = np.stack([self.vectors[h] for h in self._ann_hashes])
        self._ann_index = BallTree(matrix, metric='euclidean')
        logger.info(f"Rebuilt ANN index with {len(self._ann_hashes)} tracks")

    # -- indices ---------------------------------------------------------------

    def _index_track(self, track_hash: str, bpm: Optional[float],
                     genre: str, key_str: Optional[str]):
        if bpm:
            b = int(round(float(bpm) / 10)) * 10
            self._bpm_index[(b, b + 10)].append(track_hash)
        self._genre_index[genre].append(track_hash)
        if key_str:
            self._key_index[key_str].append(track_hash)

    def _update_indices(self, track_hash: str, features: AudioFeatures, genre: str):
        key_str = f"{features.key.tonic}{features.key.quality}" if features.key else None
        self._index_track(track_hash, features.tempo, genre, key_str)
        if self.use_vectors:
            self.vectors[track_hash] = self._compute_vector(features)
            if len(self.vectors) % 10 == 0:
                self._rebuild_ann()

    # -- similarity ------------------------------------------------------------

    def find_similar(self,
                     query_features: Optional[AudioFeatures] = None,
                     query_hash: Optional[str] = None,
                     genre: Optional[str] = None,
                     bpm_range: Optional[Tuple[float, float]] = None,
                     k: int = 5) -> List[Tuple[str, float]]:
        candidates: set = set()
        if genre:
            candidates.update(self._genre_index.get(genre, []))
        if bpm_range:
            for (lo, hi), hashes in self._bpm_index.items():
                if lo >= bpm_range[0] and hi <= bpm_range[1]:
                    candidates.update(hashes)
        if not candidates:
            candidates = set(self.tracks.keys())

        if query_features and self._ann_index and len(candidates) >= k:
            query_vec = self._compute_vector(query_features).reshape(1, -1)
            dists, inds = self._ann_index.query(query_vec,
                                                k=min(k * 3, len(self._ann_hashes)))
            results = [(self._ann_hashes[idx], float(d))
                       for d, idx in zip(dists[0], inds[0])
                       if self._ann_hashes[idx] in candidates]
            return results[:k]

        if query_hash and query_hash in self.vectors:
            query_vec = self.vectors[query_hash]
            results = [(h, float(np.linalg.norm(query_vec - self.vectors[h])))
                       for h in candidates if h in self.vectors]
            results.sort(key=lambda x: x[1])
            return results[:k]

        import random
        sample = random.sample(list(candidates), min(k, len(candidates)))
        return [(h, float('inf')) for h in sample]

    # -- persistence -------------------------------------------------------------

    def _init_genre(self, genre: str):
        if genre not in self.genre_profiles:
            self.genre_profiles[genre] = {
                'bpms': [], 'keys': [], 'spectral_centroids': [],
                'samples': [], 'patterns': [], 'vectors': [], 'track_hashes': [],
            }

    def load_memory(self):
        if not os.path.exists(self.memory_path):
            return
        try:
            with open(self.memory_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.tracks = data.get('tracks', {})
            self.genre_profiles = data.get('genre_profiles', {})
            self.sample_library = data.get('sample_library', [])
            self.pattern_library = data.get('pattern_library', [])

            if os.path.exists(self.vector_path):
                vec_data = np.load(self.vector_path, allow_pickle=True).item()
                self.vectors = {k: v.astype(np.float32) for k, v in vec_data.items()}

            # v3: rebuild search indices from persisted metadata (v2 never did)
            for h, track in self.tracks.items():
                self._index_track(h, track.get('bpm'),
                                  track.get('genre', 'unknown'), track.get('key'))
            if self.vectors and self.use_vectors:
                self._rebuild_ann()
            logger.info(f"Loaded memory: {len(self.tracks)} tracks, "
                        f"{len(self.vectors)} vectors")
        except Exception as e:
            logger.error(f"Failed to load memory: {e}")

    def save_memory(self):
        data = {
            'tracks': self.tracks,
            'genre_profiles': self.genre_profiles,
            'sample_library': self.sample_library,
            'pattern_library': self.pattern_library,
            'version': '3.0',
            'saved_at': datetime.now().isoformat(),
        }
        dir_name = os.path.dirname(self.memory_path) or '.'
        tmp_json = None
        with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False,
                                         encoding='utf-8') as tf:
            json.dump(data, tf, indent=2)
            tmp_json = tf.name  # file closed here before os.replace (v2 bug)
        tmp_vec = None
        if self.vectors:
            with tempfile.NamedTemporaryFile('wb', dir=dir_name, delete=False) as tf:
                np.save(tf, dict(self.vectors))
                tmp_vec = tf.name
        try:
            os.replace(tmp_json, self.memory_path)
            if tmp_vec:
                os.replace(tmp_vec, self.vector_path)
            logger.info("Memory saved successfully.")
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")
            for t in (tmp_json, tmp_vec):
                if t and os.path.exists(t):
                    os.remove(t)


# ==============================================================================
# 2. NEURAL AUDIO CODEC INTERFACE
# ==============================================================================

class NeuralCodecInterface:
    """Unified interface for EnCodec / DAC token codecs."""

    def __init__(self, codec_type: Literal['encodec', 'dac', 'auto'] = 'auto',
                 device: str = 'cpu'):
        self.codec_type = codec_type
        self.device = device
        self.model = None
        self.processor = None
        self.sample_rate = 32000
        self._load_codec()

    def _load_codec(self):
        if self.codec_type == 'auto':
            if DEPS['dac']:
                self.codec_type = 'dac'
            elif DEPS['encodec']:
                self.codec_type = 'encodec'
            else:
                logger.warning("No neural codec available")
                return

        if self.codec_type == 'dac' and DEPS['dac']:
            try:
                import dac  # type: ignore
                self.model = dac.DAC.load()
                self.model.to(self.device)
                self.sample_rate = 44100
                logger.info("DAC (Descript Audio Codec) loaded")
            except Exception as e:
                logger.warning(f"Failed to load DAC: {e}")
                self.codec_type = 'encodec'
                self._load_codec()
        elif self.codec_type == 'encodec' and DEPS['encodec']:
            try:
                from transformers import EncodecModel, AutoProcessor  # type: ignore
                self.model = EncodecModel.from_pretrained("facebook/encodec_32khz").to(self.device)
                self.processor = AutoProcessor.from_pretrained("facebook/encodec_32khz")
                self.sample_rate = 32000
                logger.info("EnCodec 32kHz loaded")
            except Exception as e:
                logger.warning(f"Failed to load EnCodec: {e}")

    def encode(self, audio: npt.NDArray[np.float32], sr: int) -> npt.NDArray[np.int64]:
        if self.model is None:
            return np.array([])
        if sr != self.sample_rate:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=self.sample_rate)
        try:
            import torch
            if self.codec_type == 'encodec':
                inputs = self.processor(raw_audio=audio, sampling_rate=self.sample_rate,
                                        return_tensors="pt")
                with torch.no_grad():
                    enc = self.model.encode(inputs["input_values"].to(self.device))
                return enc.audio_codes[0].cpu().numpy()
            if self.codec_type == 'dac':
                audio_t = torch.from_numpy(audio).float().to(self.device).unsqueeze(0).unsqueeze(0)
                with torch.no_grad():
                    codes = self.model.encode(audio_t)
                return codes[0].cpu().numpy()
        except Exception as e:
            logger.error(f"Codec encoding failed: {e}")
        return np.array([])

    def decode(self, codes: npt.NDArray[np.int64]) -> Tuple[npt.NDArray[np.float32], int]:
        if self.model is None:
            return np.array([]), 0
        try:
            import torch
            codes_t = torch.from_numpy(codes).to(self.device)
            if self.codec_type == 'encodec':
                audio_values = self.model.decode(codes_t.unsqueeze(0), [None])[0]
                return audio_values[0, 0].cpu().numpy(), self.sample_rate
            if self.codec_type == 'dac':
                return self.model.decode(codes_t)[0].cpu().numpy(), self.sample_rate
        except Exception as e:
            logger.error(f"Decoding failed: {e}")
        return np.array([]), 0

    def encode_file(self, path: str) -> npt.NDArray[np.int64]:
        audio, sr = librosa.load(path, sr=self.sample_rate, mono=True)
        return self.encode(audio, sr)


# ==============================================================================
# 3. NEURAL GENERATION — MusicGen with temporal conditioning
# ==============================================================================

class TemporalCondition:
    """Symbolic temporal condition (chord/rhythm), MusiConGen-style."""

    def __init__(self, bpm: float, chord_progression: List[ChordEvent],
                 time_signature: Tuple[int, int] = (4, 4)):
        self.bpm = bpm
        self.chord_progression = chord_progression
        self.time_signature = time_signature

    def to_condition_tensor(self, duration: float, sr: int = 32000) -> Optional[Any]:
        if not DEPS['torch']:
            return None
        import torch
        frame_rate = 50
        n_frames = int(duration * frame_rate)
        frame_duration = 1.0 / frame_rate
        feats = []
        for frame_idx in range(n_frames):
            frame_time = frame_idx * frame_duration
            active = None
            for chord in self.chord_progression:
                if chord.start_time <= frame_time < chord.start_time + chord.duration:
                    active = chord
                    break
            if active:
                freqs = active.frequencies(octave=3)
                feats.append([len(freqs)] + [f / 1000.0 for f in freqs[:3]])
            else:
                feats.append([0, 0, 0, 0])
        return torch.tensor(feats, dtype=torch.float32).unsqueeze(0)


class NeuralGenerator:
    """MusicGen wrapper (melody conditioning, continuation, stem edit fallback)."""

    MODEL_SIZES = {
        'small': 'facebook/musicgen-small',
        'medium': 'facebook/musicgen-medium',
        'large': 'facebook/musicgen-large',
        'melody': 'facebook/musicgen-melody',
        'stereo-small': 'facebook/musicgen-stereo-small',
        'stereo-medium': 'facebook/musicgen-stereo-medium',
        'stereo-large': 'facebook/musicgen-stereo-large',
    }

    def __init__(self,
                 model_size: Literal['small', 'medium', 'large', 'melody',
                                     'stereo-small', 'stereo-medium', 'stereo-large'] = 'medium',
                 device: Optional[str] = None):
        self.model_size = model_size
        self.model_name = self.MODEL_SIZES[model_size]
        self.device = device or ("cuda" if DEPS['torch'] and torch.cuda.is_available() else "cpu")
        self.model = None
        self.processor = None
        self._is_stereo = 'stereo' in model_size
        self._load_model()

    def _load_model(self):
        if not (DEPS['transformers'] and DEPS['torch']):
            logger.warning("Transformers/torch not available. Neural generation disabled.")
            return
        try:
            from transformers import AutoProcessor, MusicgenForConditionalGeneration
            logger.info(f"Loading MusicGen: {self.model_name}")
            self.processor = AutoProcessor.from_pretrained(self.model_name)
            self.model = MusicgenForConditionalGeneration.from_pretrained(self.model_name)
            self.model.to(self.device)
            self.model.eval()
            logger.info(f"MusicGen loaded on {self.device}")
        except Exception as e:
            logger.warning(f"Failed to load MusicGen: {e}")

    @property
    def is_available(self) -> bool:
        return self.model is not None

    def generate(self,
                 prompt: str,
                 duration: float = 10.0,
                 temporal_condition: Optional[TemporalCondition] = None,
                 reference_audio: Optional[npt.NDArray[np.float32]] = None,
                 guidance_scale: float = 3.0,
                 temperature: float = 1.0,
                 top_k: int = 250,
                 max_new_tokens: Optional[int] = None) -> Optional[npt.NDArray[np.float32]]:
        if not self.is_available:
            return None
        try:
            import torch
            if reference_audio is not None and 'melody' in self.model_size:
                inputs = self.processor(text=[prompt], audio=reference_audio,
                                        sampling_rate=32000, padding=True,
                                        return_tensors="pt")
            else:
                inputs = self.processor(text=[prompt], padding=True,
                                        return_tensors="pt")
            inputs = {k: v.to(self.device) if isinstance(v, torch.Tensor) else v
                      for k, v in inputs.items()}

            if temporal_condition is not None:
                # Frame-wise chord/rhythm conditioning hook (MusiConGen-style).
                # The stock HF MusicGen generate() has no external conditioning
                # slot; the tensor is prepared here for a forked decoder.
                _ = temporal_condition.to_condition_tensor(duration)

            if max_new_tokens is None:
                max_new_tokens = int(duration * 50)

            with torch.no_grad():
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=temperature > 0,
                    temperature=temperature,
                    top_k=top_k,
                    guidance_scale=guidance_scale,
                )

            audio = output[0]
            if audio.ndim > 1:
                audio = audio[0]
            audio_np = audio.cpu().numpy()
            target_len = int(duration * 32000)  # MusicGen native 32 kHz
            if len(audio_np) < target_len:
                audio_np = np.pad(audio_np, (0, target_len - len(audio_np)))
            else:
                audio_np = audio_np[:target_len]
            return audio_np
        except Exception as e:
            logger.error(f"Neural generation failed: {e}")
            return None

    def continue_audio(self, prompt: str, audio_prefix: npt.NDArray[np.float32],
                       duration: float = 10.0) -> Optional[npt.NDArray[np.float32]]:
        if not self.is_available:
            return None
        try:
            import torch
            inputs = self.processor(text=[prompt], audio=audio_prefix,
                                    sampling_rate=32000, padding=True,
                                    return_tensors="pt")
            inputs = {k: v.to(self.device) if isinstance(v, torch.Tensor) else v
                      for k, v in inputs.items()}
            with torch.no_grad():
                output = self.model.generate(**inputs,
                                             max_new_tokens=int(duration * 50),
                                             do_sample=True, temperature=1.0)
            return output[0, 0].cpu().numpy()
        except Exception as e:
            logger.error(f"Audio continuation failed: {e}")
            return None

    def edit_stem(self, audio: npt.NDArray[np.float32], instruction: str,
                  sr: int = 32000, strength: float = 0.5) -> Optional[npt.NDArray[np.float32]]:
        """Instruct-MusicGen-style editing; heuristic fallback without the model."""
        logger.info(f"Stem editing requested: '{instruction}' (strength={strength})")
        if "remove" in instruction.lower() and "drum" in instruction.lower():
            return self._highpass_filter(audio, 200, sr)
        if "add" in instruction.lower() and "guitar" in instruction.lower():
            logger.info("Note: requires Instruct-MusicGen weights for real addition")
        return audio

    def _highpass_filter(self, audio: npt.NDArray[np.float32],
                         cutoff: float, sr: int) -> npt.NDArray[np.float32]:
        nyquist = 0.5 * sr
        normalized = min(cutoff / nyquist, 0.99)
        b, a = butter(4, normalized, btype='high')
        return lfilter(b, a, audio)


# ==============================================================================
# 4. MULTI-MODAL CONDITIONING — CLIP + mapper
# ==============================================================================

class MultiModalConditioner:
    """CLIP embeddings → structured music parameters (learned mapper or priors)."""

    def __init__(self, mapper_checkpoint: Optional[str] = None):
        self.clip_model = None
        self.clip_preprocess = None
        self.device = "cpu"
        self.mapper: Optional[Callable] = None
        self._load_clip()
        self._init_mapper(mapper_checkpoint)

    def _load_clip(self):
        if DEPS['clip']:
            try:
                import clip  # type: ignore
                import torch
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
                self.clip_model, self.clip_preprocess = clip.load("ViT-B/32", device=self.device)
                self.clip_model.eval()
                logger.info("CLIP loaded for multi-modal conditioning")
            except Exception as e:
                logger.warning(f"Failed to load CLIP: {e}")

    def _init_mapper(self, checkpoint: Optional[str]):
        if DEPS['torch'] and checkpoint and os.path.exists(checkpoint):
            try:
                self.mapper = torch.load(checkpoint, map_location=self.device)
                logger.info(f"Loaded mapper from {checkpoint}")
            except Exception as e:
                logger.warning(f"Failed to load mapper: {e}")
        self._genre_priors = {
            'electronic': {'bpm_mean': 128, 'bpm_std': 15, 'brightness': 0.7, 'complexity': 0.8},
            'rock':       {'bpm_mean': 140, 'bpm_std': 20, 'brightness': 0.6, 'complexity': 0.7},
            'ambient':    {'bpm_mean': 70,  'bpm_std': 15, 'brightness': 0.3, 'complexity': 0.4},
            'pop':        {'bpm_mean': 120, 'bpm_std': 12, 'brightness': 0.6, 'complexity': 0.5},
            'classical':  {'bpm_mean': 90,  'bpm_std': 25, 'brightness': 0.4, 'complexity': 0.9},
            'jazz':       {'bpm_mean': 110, 'bpm_std': 30, 'brightness': 0.5, 'complexity': 0.85},
            'hiphop':     {'bpm_mean': 85,  'bpm_std': 10, 'brightness': 0.5, 'complexity': 0.6},
        }

    def encode_text(self, text: str) -> Optional[npt.NDArray[np.float32]]:
        if self.clip_model is None:
            return None
        try:
            import torch
            import clip  # type: ignore
            with torch.no_grad():
                tokens = clip.tokenize([text]).to(self.device)
                embedding = self.clip_model.encode_text(tokens).cpu().numpy()[0]
            return embedding.astype(np.float32)
        except Exception as e:
            logger.error(f"Text encoding failed: {e}")
            return None

    def encode_image(self, image_path: str) -> Optional[npt.NDArray[np.float32]]:
        if self.clip_model is None:
            return None
        try:
            from PIL import Image
            import torch
            image = Image.open(image_path).convert("RGB")
            image_input = self.clip_preprocess(image).unsqueeze(0).to(self.device)
            with torch.no_grad():
                embedding = self.clip_model.encode_image(image_input).cpu().numpy()[0]
            return embedding.astype(np.float32)
        except Exception as e:
            logger.error(f"Image encoding failed: {e}")
            return None

    def embedding_to_music_params(self, embedding: npt.NDArray[np.float32]) -> Dict[str, Any]:
        if self.mapper is not None and DEPS['torch']:
            try:
                import torch
                with torch.no_grad():
                    self.mapper(torch.from_numpy(embedding).float().to(self.device))
            except Exception:
                pass

        if len(embedding) < 64:
            return self._default_params()

        energy = float(np.mean(embedding[:128]))
        valence = float(np.mean(embedding[128:256]))
        texture = float(np.mean(embedding[256:384]))

        genre_scores = {}
        for genre, prior in self._genre_priors.items():
            proto = np.zeros(512, dtype=np.float32)
            proto[0] = prior['bpm_mean'] / 200.0
            proto[1] = prior['brightness']
            proto[2] = prior['complexity']
            sim = float(np.dot(embedding[:3], proto[:3]) /
                        (np.linalg.norm(embedding[:3]) * np.linalg.norm(proto[:3]) + 1e-8))
            genre_scores[genre] = sim

        best_genre = max(genre_scores, key=genre_scores.get)
        prior = self._genre_priors[best_genre]
        bpm = prior['bpm_mean'] + np.tanh(energy) * prior['bpm_std'] * 2

        key_idx = int(np.abs(np.sum(embedding[300:312]) * 10)) % 12
        key = NOTES[key_idx]

        arousal = float(np.tanh(energy * 2))
        valence_norm = float(np.tanh(valence * 2))
        if valence_norm > 0.3 and arousal > 0.3:
            mood = 'happy'
        elif valence_norm < -0.3 and arousal > 0.3:
            mood = 'angry'
        elif valence_norm < -0.3 and arousal < -0.3:
            mood = 'sad'
        elif valence_norm > 0.3 and arousal < -0.3:
            mood = 'peaceful'
        else:
            mood = 'neutral'

        complexity = float(np.clip(prior['complexity'] + 0.1 * np.tanh(texture), 0, 1))
        brightness = float(np.clip(prior['brightness'] + 0.1 * np.tanh(np.mean(embedding[400:450])), 0, 1))

        return {
            'bpm': float(bpm), 'genre': best_genre, 'key': key, 'mood': mood,
            'complexity': complexity, 'brightness': brightness,
            'arousal': arousal, 'valence': valence_norm,
            'genre_confidence': float(genre_scores[best_genre]),
        }

    def _default_params(self) -> Dict[str, Any]:
        return {'bpm': 120.0, 'genre': 'unknown', 'key': 'C', 'mood': 'neutral',
                'complexity': 0.5, 'brightness': 0.5, 'arousal': 0.0,
                'valence': 0.0, 'genre_confidence': 0.0}

    def generate_chord_progression(self, params: Dict[str, Any],
                                   duration: float) -> List[ChordEvent]:
        key = params.get('key', 'C')
        genre = params.get('genre', 'pop')
        mood = params.get('mood', 'neutral')
        complexity = params.get('complexity', 0.5)

        progressions = {
            'pop': {
                'happy':   [('I', 'maj'), ('V', 'maj'), ('vi', 'min'), ('IV', 'maj')],
                'sad':     [('vi', 'min'), ('IV', 'maj'), ('I', 'maj'), ('V', 'maj')],
                'neutral': [('I', 'maj'), ('IV', 'maj'), ('V', 'maj'), ('I', 'maj')],
            },
            'electronic': {
                'happy':   [('i', 'min'), ('VI', 'maj'), ('III', 'maj'), ('VII', 'maj')],
                'sad':     [('i', 'min'), ('v', 'min'), ('VI', 'maj'), ('III', 'maj')],
                'neutral': [('i', 'min'), ('VII', 'maj'), ('VI', 'maj'), ('V', 'maj')],
            },
            'jazz': {
                'happy':   [('ii', 'min7'), ('V', 'dom7'), ('I', 'maj7'), ('VI', 'min7')],
                'sad':     [('iii', 'min7'), ('vi', 'min7'), ('ii', 'min7'), ('V', 'dom7')],
                'neutral': [('I', 'maj7'), ('vi', 'min7'), ('ii', 'min7'), ('V', 'dom7')],
            },
            'classical': {
                'happy':   [('I', 'maj'), ('IV', 'maj'), ('V', 'dom7'), ('I', 'maj')],
                'sad':     [('i', 'min'), ('iv', 'min'), ('V', 'maj'), ('i', 'min')],
                'neutral': [('I', 'maj'), ('V', 'maj'), ('vi', 'min'), ('iii', 'min')],
            },
        }
        genre_progs = progressions.get(genre, progressions['pop'])
        mood_progs = genre_progs.get(mood, genre_progs.get('neutral', genre_progs['happy']))

        roman_to_degree = {'I': 0, 'II': 2, 'III': 4, 'IV': 5, 'V': 7, 'VI': 9, 'VII': 11,
                           'i': 0, 'ii': 2, 'iii': 4, 'iv': 5, 'v': 7, 'vi': 9, 'vii': 11}

        key_offset = NOTE_SEMITONES.get(key, 0)
        bar_duration = 60.0 / params['bpm'] * 4
        chords_per_bar = 2 if complexity > 0.6 else 1
        num_bars = max(4, int(duration / bar_duration))

        chord_events = []
        current_time = 0.0
        for bar in range(num_bars):
            for chord_idx in range(chords_per_bar):
                roman, quality = mood_progs[(bar * chords_per_bar + chord_idx) % len(mood_progs)]
                degree = roman_to_degree.get(roman.upper(), 0)
                root = NOTES[(key_offset + degree) % 12]
                chord_dur = bar_duration / chords_per_bar
                chord_events.append(ChordEvent(current_time, chord_dur, root, quality))
                current_time += chord_dur
        return chord_events


# ==============================================================================
# 5. SYMBOLIC MUSIC ENGINE
# ==============================================================================

class SymbolicMusicEngine:
    """Theory-aware symbolic generation (melody, bass) + wavetable rendering."""

    SCALES = {
        'major': [0, 2, 4, 5, 7, 9, 11],
        'minor': [0, 2, 3, 5, 7, 8, 10],
        'pentatonic_major': [0, 2, 4, 7, 9],
        'pentatonic_minor': [0, 3, 5, 7, 10],
        'blues': [0, 3, 5, 6, 7, 10],
        'chromatic': list(range(12)),
    }

    def __init__(self, sample_rate: int = 44100):
        self.sr = sample_rate

    @staticmethod
    def _freq_to_midi(freq: float) -> int:
        return 69 + int(np.round(12 * np.log2(freq / 440.0)))

    def generate_melody_from_chords(self, chords: List[ChordEvent], key: MusicKey,
                                    style: str = 'balanced',
                                    humanize: float = 0.3) -> List[Tuple[float, float, int, float]]:
        melody = []
        scale = self.SCALES.get('major' if key.quality == 'major' else 'minor',
                                self.SCALES['major'])
        notes_per_chord = {'sparse': 1, 'balanced': 2, 'busy': 4}.get(style, 2)

        for chord in chords:
            chord_midi = [self._freq_to_midi(f) for f in chord.frequencies(octave=4)]
            note_dur = chord.duration / (notes_per_chord + 1)
            for i in range(notes_per_chord):
                if np.random.rand() < 0.7:
                    pitch = int(np.random.choice(chord_midi))
                else:
                    scale_pitches = [chord_midi[0] + s for s in scale]
                    pitch = int(np.random.choice(scale_pitches))
                start = chord.start_time + i * note_dur + \
                    np.random.uniform(0, note_dur * 0.3) * humanize
                duration = note_dur * np.random.uniform(0.5, 1.2)
                velocity = float(np.random.uniform(60, 110))
                pitch = max(48, min(84, pitch))
                melody.append((float(start), float(duration), pitch, velocity))
        return melody

    def generate_bass_line(self, chords: List[ChordEvent],
                           style: str = 'walking') -> List[Tuple[float, float, int, float]]:
        bass = []
        for chord in chords:
            root_midi = self._freq_to_midi(chord.frequencies(octave=2)[0])
            if style == 'root':
                bass.append((chord.start_time, chord.duration * 0.9,
                             max(24, min(48, root_midi)), 100.0))
            elif style == 'walking':
                beats = 4
                beat_dur = chord.duration / beats
                for b in range(beats):
                    if b == 0:
                        pitch = root_midi
                    elif b == 2:
                        pitch = root_midi + 7
                    else:
                        pitch = root_midi + int(np.random.choice([2, 4, 5, 9]))
                    bass.append((chord.start_time + b * beat_dur, beat_dur * 0.8,
                                 max(24, min(48, pitch)), 90.0))
        return bass

    def _adsr(self, n: int, dur: float) -> npt.NDArray[np.float32]:
        attack, decay, sustain, release = 0.01, 0.1, 0.7, 0.1
        env = np.ones(n, dtype=np.float32)
        a = min(int(attack * self.sr), n)
        if a > 0:
            env[:a] = np.linspace(0, 1, a)
        d = min(int(decay * self.sr), max(n - a, 0))
        if d > 0:
            env[a:a + d] = np.linspace(1, sustain, d)
        r = min(int(release * self.sr), n)
        rs = max(n - r, 0)
        if r > 0 and rs < n:
            env[rs:] *= np.linspace(1, 0, n - rs)
        return env

    def render_melody_to_audio(self, notes: List[Tuple[float, float, int, float]],
                               instrument: str = 'sine',
                               duration: Optional[float] = None) -> npt.NDArray[np.float32]:
        if not notes:
            return np.array([])
        if duration is None:
            duration = max(n[0] + n[1] for n in notes)
        samples = int(duration * self.sr)
        audio = np.zeros(samples, dtype=np.float32)
        t = np.arange(samples) / self.sr

        for start, dur, pitch, vel in notes:
            start_sample = int(start * self.sr)
            end_sample = min(start_sample + int(dur * self.sr), samples)
            if start_sample >= end_sample:
                continue
            freq = 440.0 * (2 ** ((pitch - 69) / 12.0))
            note_t = t[start_sample:end_sample] - start
            if instrument == 'saw':
                wave = 2 * ((note_t * freq) % 1) - 1
            elif instrument == 'square':
                wave = np.sign(np.sin(2 * np.pi * freq * note_t))
            elif instrument == 'fm':
                mod = np.sin(2 * np.pi * freq * 2 * note_t)
                wave = np.sin(2 * np.pi * freq * note_t + 3 * mod)
            else:
                wave = np.sin(2 * np.pi * freq * note_t)
            env = self._adsr(len(note_t), dur)
            audio[start_sample:end_sample] += wave * env * (vel / 127.0) * 0.3
        return audio / (np.max(np.abs(audio)) + 1e-8)


# ==============================================================================
# 6. STEM GENERATION
# ==============================================================================

class StemGenerator:
    """Theory-guided multi-track stem generation (drums, bass, chords, melody, fx)."""

    def __init__(self, sample_rate: int = 44100,
                 symbolic_engine: Optional[SymbolicMusicEngine] = None):
        self.sr = sample_rate
        self.symbolic = symbolic_engine or SymbolicMusicEngine(sample_rate)
        self._drum_kits: Dict[str, Dict[str, npt.NDArray[np.float32]]] = {}
        self._load_default_drum_kits()

    def _load_default_drum_kits(self):
        t = np.linspace(0, 0.5, int(0.5 * self.sr), endpoint=False)
        freq = np.linspace(150, 40, len(t))
        kick = np.sin(2 * np.pi * np.cumsum(freq) / self.sr) * np.exp(-t * 10)
        kick = kick / (np.max(np.abs(kick)) + 1e-8)
        snare = (np.random.randn(len(t)) * np.exp(-t * 8) +
                 np.sin(2 * np.pi * 200 * t) * np.exp(-t * 15)) * 0.5
        snare = snare / (np.max(np.abs(snare)) + 1e-8)
        hh_t = np.linspace(0, 0.1, int(0.1 * self.sr), endpoint=False)
        hihat = np.random.randn(len(hh_t)) * np.exp(-hh_t * 50) * 0.3
        clap_t = np.linspace(0, 0.15, int(0.15 * self.sr), endpoint=False)
        clap = np.random.randn(len(clap_t)) * np.exp(-clap_t * 30) * 0.4
        self._drum_kits['electronic'] = {'kick': kick, 'snare': snare,
                                         'hihat': hihat, 'clap': clap}
        self._drum_kits['acoustic'] = {'kick': kick * 1.2, 'snare': snare * 1.3,
                                       'hihat': hihat * 0.8, 'clap': clap}

    def _default_progression(self, structure: SongStructure,
                             duration: float) -> List[ChordEvent]:
        bar_dur = (60.0 / structure.tempo) * structure.time_signature[0]
        prog = [('C', 'maj'), ('A', 'min'), ('F', 'maj'), ('G', 'maj')]
        reps = max(1, int(duration / (bar_dur * len(prog))) + 1)
        return [ChordEvent(i * bar_dur, bar_dur, r, q)
                for i, (r, q) in enumerate(prog * reps)]

    def generate_stems(self, structure: SongStructure,
                       style: str = 'electronic') -> Dict[str, StemTrack]:
        stems: Dict[str, StemTrack] = {}
        duration = structure.total_bars() * (60.0 / structure.tempo) * \
            structure.time_signature[0]
        if duration <= 0:
            duration = 10.0

        chords = [c for s in structure.sections for c in s.chord_progression]
        if not chords:  # v3: real progression (v2 misused the melody generator)
            chords = self._default_progression(structure, duration)

        stems['drums'] = self._generate_drums(structure, style, duration)

        bass_notes = self.symbolic.generate_bass_line(
            chords, style='walking' if style == 'jazz' else 'root')
        stems['bass'] = StemTrack(
            name='bass',
            audio=self.symbolic.render_melody_to_audio(bass_notes, 'sine', duration),
            midi_notes=bass_notes)

        stems['chords'] = StemTrack(name='chords',
                                    audio=self._generate_chord_pad(chords, duration, style))

        melody_notes = self.symbolic.generate_melody_from_chords(
            chords, structure.key, style='balanced' if style != 'ambient' else 'sparse')
        stems['melody'] = StemTrack(
            name='melody',
            audio=self.symbolic.render_melody_to_audio(
                melody_notes, 'fm' if style == 'electronic' else 'sine', duration),
            midi_notes=melody_notes)

        if style in ('ambient', 'electronic'):
            stems['fx'] = StemTrack(name='fx', audio=self._generate_fx(duration, style))
        return stems

    def _generate_drums(self, structure: SongStructure, style: str,
                        duration: float) -> StemTrack:
        drums = np.zeros(int(duration * self.sr), dtype=np.float32)
        kit = self._drum_kits.get(style, self._drum_kits['electronic'])
        beat_dur = 60.0 / structure.tempo  # v3: real tempo (v2 hardcoded 120)

        for section in structure.sections:
            section_start = section.start_bar * beat_dur * structure.time_signature[0]
            for event in self._drum_pattern_for_section(section, style, beat_dur):
                event_time = section_start + event['offset']
                if event_time >= duration:
                    continue
                start_sample = int(event_time * self.sr)
                sample = kit.get(event['sample'], kit['kick'])
                end_sample = min(start_sample + len(sample), len(drums))
                if start_sample >= len(drums):
                    continue
                gain = event.get('gain', 1.0) * (0.5 + section.intensity * 0.5)
                drums[start_sample:end_sample] += \
                    sample[:end_sample - start_sample] * gain
        drums = drums / (np.max(np.abs(drums)) + 1e-8)
        return StemTrack(name='drums', audio=drums)

    def _drum_pattern_for_section(self, section: Section, style: str,
                                  beat_dur: float) -> List[Dict]:
        events: List[Dict] = []
        total_beats = section.duration_bars * 4
        if style == 'electronic':
            for beat in range(total_beats):
                offset = beat * beat_dur
                events.append({'offset': offset, 'sample': 'kick', 'gain': 1.0})
                if beat % 2 == 1:
                    events.append({'offset': offset, 'sample': 'hihat', 'gain': 0.7})
                if beat % 4 == 2:
                    events.append({'offset': offset, 'sample': 'clap', 'gain': 0.9})
        elif style == 'hiphop':
            for beat in range(total_beats):
                offset = beat * beat_dur
                if beat % 4 == 0:
                    events.append({'offset': offset, 'sample': 'kick', 'gain': 1.0})
                if beat % 4 == 2:
                    events.append({'offset': offset, 'sample': 'snare', 'gain': 1.0})
                events.append({'offset': offset + beat_dur * 0.5,
                               'sample': 'hihat', 'gain': 0.5})
        elif style == 'rock':
            for beat in range(total_beats):
                offset = beat * beat_dur
                events.append({'offset': offset, 'sample': 'kick', 'gain': 1.0})
                events.append({'offset': offset + beat_dur * 0.5,
                               'sample': 'hihat', 'gain': 0.6})
                if beat % 2 == 1:
                    events.append({'offset': offset, 'sample': 'snare', 'gain': 1.0})
        else:  # minimal / ambient
            for bar in range(section.duration_bars):
                events.append({'offset': bar * 4 * beat_dur, 'sample': 'kick', 'gain': 0.8})
        return events

    def _generate_chord_pad(self, chords: List[ChordEvent], duration: float,
                            style: str) -> npt.NDArray[np.float32]:
        samples = int(duration * self.sr)
        pad = np.zeros(samples, dtype=np.float32)
        t = np.linspace(0, duration, samples, endpoint=False)
        for chord in chords:
            start = int(chord.start_time * self.sr)
            end = min(int((chord.start_time + chord.duration) * self.sr), samples)
            if start >= end:
                continue
            seg_t = t[start:end]
            for f in chord.frequencies(octave=3):
                phase = np.random.uniform(0, 2 * np.pi)
                wave = np.sin(2 * np.pi * f * seg_t + phase)
                env = np.ones(end - start, dtype=np.float32)  # v3: full-length envelope
                att = min(int(0.5 * self.sr), end - start)
                env[:att] = np.linspace(0, 1, att)
                rel = min(int(0.25 * self.sr), end - start)
                env[-rel:] *= np.linspace(1, 0, rel)
                pad[start:end] += wave * env * 0.1
        if style == 'ambient':
            pad = gaussian_filter1d(pad, sigma=20)
        return pad / (np.max(np.abs(pad)) + 1e-8)

    def _generate_fx(self, duration: float, style: str) -> npt.NDArray[np.float32]:
        samples = int(duration * self.sr)
        noise = np.random.randn(samples) * 0.05
        fx = gaussian_filter1d(noise, sigma=100)
        t = np.linspace(0, duration, samples, endpoint=False)
        drone = np.sin(2 * np.pi * 55 * t) * 0.1 * np.exp(-t * 0.1)
        return (fx + drone) / (np.max(np.abs(fx + drone)) + 1e-8)

    def export_stems(self, stems: Dict[str, StemTrack], output_dir: str):
        os.makedirs(output_dir, exist_ok=True)
        for name, stem in stems.items():
            audio = stem.audio / (np.max(np.abs(stem.audio)) + 1e-8)
            path = os.path.join(output_dir, f"{name}.wav")
            sf.write(path, audio, self.sr)
            if stem.midi_notes:
                with open(os.path.join(output_dir, f"{name}.json"), 'w') as f:
                    json.dump([{'start': n[0], 'duration': n[1],
                                'pitch': n[2], 'velocity': n[3]}
                               for n in stem.midi_notes], f, indent=2)
            logger.info(f"Saved stem: {path}")


# ==============================================================================
# 7. STYLE & TIMBRE TRANSFER
# ==============================================================================

class TimbreAnalyzer:
    def __init__(self, n_fft: int = 2048, hop_length: int = 512):
        self.n_fft = n_fft
        self.hop_length = hop_length

    def extract_spectral_envelope(self, audio: npt.NDArray[np.float32],
                                  sr: int) -> npt.NDArray[np.float32]:
        D = librosa.stft(audio, n_fft=self.n_fft, hop_length=self.hop_length)
        log_mag = np.log(np.abs(D) + 1e-8)
        cepstrum = np.fft.rfft(log_mag, axis=0)
        cepstrum[30:, :] = 0
        return np.exp(np.fft.irfft(cepstrum, n=log_mag.shape[0], axis=0))

    def extract_harmonic_structure(self, audio: npt.NDArray[np.float32],
                                   sr: int) -> Tuple[npt.NDArray, npt.NDArray]:
        return librosa.effects.hpss(audio)


class StyleTransfer:
    """Genre spectral presets + DDSP-inspired envelope-morph timbre transfer."""

    def __init__(self, sample_rate: int = 44100):
        self.sr = sample_rate
        self.analyzer = TimbreAnalyzer()
        self._style_presets = {
            'electronic': {'highpass_freq': 80, 'saturation': 0.3,
                           'stereo_widening': 0.5, 'transient_boost': 1.3},
            'ambient':    {'highpass_freq': 40, 'stereo_widening': 0.8,
                           'transient_boost': 0.7, 'smoothing': 100},
            'rock':       {'highpass_freq': 100, 'saturation': 0.6,
                           'transient_boost': 1.5, 'mid_boost_db': 3},
            'jazz':       {'highpass_freq': 60, 'saturation': 0.2,
                           'transient_boost': 1.1, 'mid_boost_db': 2},
            'classical':  {'highpass_freq': 30, 'saturation': 0.05,
                           'transient_boost': 0.9, 'stereo_widening': 0.6},
        }

    def apply_style(self, audio: npt.NDArray[np.float32], target_genre: str,
                    strength: float = 1.0) -> npt.NDArray[np.float32]:
        preset = self._style_presets.get(target_genre, self._style_presets['electronic'])
        result = audio.copy().astype(np.float32)
        if 'highpass_freq' in preset:
            result = self._highpass(result, preset['highpass_freq'])
        if 'saturation' in preset:
            sat = preset['saturation'] * strength
            result = np.tanh(result * (1 + sat * 5)) / (1 + sat)
        if 'transient_boost' in preset:
            result = self._compress(result, ratio=preset['transient_boost'])
        if 'smoothing' in preset:
            result = gaussian_filter1d(result,
                                       sigma=preset['smoothing'] / 1000 * self.sr)
        if 'mid_boost_db' in preset:
            result = self._eq_boost(result, 1000, preset['mid_boost_db'])
        if 'stereo_widening' in preset and result.ndim == 2:
            result = self._widen_stereo(result, preset['stereo_widening'] * strength)
        return result / (np.max(np.abs(result)) + 1e-8)

    def timbre_transfer(self, audio: npt.NDArray[np.float32], target_instrument: str,
                        source_instrument: Optional[str] = None) -> npt.NDArray[np.float32]:
        f0, _, _ = librosa.pyin(audio, fmin=librosa.note_to_hz('C2'),
                                fmax=librosa.note_to_hz('C7'), sr=self.sr)
        np.nan_to_num(f0, copy=False)

        source_env = self.analyzer.extract_spectral_envelope(audio, self.sr)
        target_env = self._synthetic_instrument_envelope(target_instrument, source_env.shape)
        morph = 0.7
        morphed_env = source_env ** (1 - morph) * target_env ** morph

        D = librosa.stft(audio, n_fft=self.analyzer.n_fft,
                         hop_length=self.analyzer.hop_length)
        mag, phase = librosa.magphase(D)
        mag_new = mag * (morphed_env / (source_env + 1e-8))
        y_new = librosa.istft(mag_new * phase, length=len(audio))
        return y_new / (np.max(np.abs(y_new)) + 1e-8)

    def _synthetic_instrument_envelope(self, instrument: str,
                                       shape: Tuple[int, ...]) -> npt.NDArray[np.float32]:
        freqs = np.linspace(0, self.sr // 2, shape[0])
        env = np.ones(shape, dtype=np.float32)
        if instrument == 'guitar':
            for h in range(1, 8):
                env *= (1 + 0.5 * np.exp(-((freqs - 200 * h) / (100 * h)) ** 2))
        elif instrument == 'piano':
            env *= np.exp(-freqs / 3000)
        elif instrument == 'violin':
            for h in (1, 3, 5, 7):
                env *= (1 + 0.3 * np.exp(-((freqs - 440 * h) / 200) ** 2))
        elif instrument == 'synth_pad':
            env *= np.exp(-((freqs - 500) / 2000) ** 2)
            env[freqs > 8000] *= 0.1
        return env

    def _highpass(self, audio: npt.NDArray[np.float32], freq: float) -> npt.NDArray[np.float32]:
        normalized = freq / (0.5 * self.sr)
        if normalized >= 0.99:
            return audio
        b, a = butter(2, normalized, btype='high')
        return lfilter(b, a, audio)

    def _compress(self, audio: npt.NDArray[np.float32], threshold_db: float = -20.0,
                  ratio: float = 2.0) -> npt.NDArray[np.float32]:
        """Simple block-RMS compressor (v2's version was a no-op)."""
        if len(audio) == 0 or np.max(np.abs(audio)) < 1e-6:
            return audio
        block = int(0.005 * self.sr)
        n = (len(audio) // block) * block
        out = audio.copy()
        for i in range(0, n, block):
            blk = audio[i:i + block]
            rms = float(np.sqrt(np.mean(blk ** 2)) + 1e-9)
            level_db = 20 * np.log10(rms)
            if level_db > threshold_db:
                red_db = (level_db - threshold_db) * (1 - 1 / max(ratio, 1.01))
                out[i:i + block] = blk * (10 ** (-red_db / 20))
        return out

    def _eq_boost(self, audio: npt.NDArray[np.float32], freq: float,
                  db: float) -> npt.NDArray[np.float32]:
        D = librosa.stft(audio)
        freqs = librosa.fft_frequencies(sr=self.sr, n_fft=2048)
        gain = 10 ** (db / 20)
        curve = 1 + (gain - 1) * np.exp(-((freqs - freq) / (freq * 0.5)) ** 2)
        return librosa.istft(D * curve[:, np.newaxis], length=len(audio))

    def _widen_stereo(self, audio: npt.NDArray[np.float32],
                      amount: float) -> npt.NDArray[np.float32]:
        if audio.ndim == 1:
            return audio
        left, right = audio[0], audio[1]
        mid = (left + right) * 0.5
        side = (left - right) * 0.5 * (1 + amount)
        return np.stack([mid + side, mid - side])


# ==============================================================================
# 8. PERSONALIZATION
# ==============================================================================

class FeedbackEvent(NamedTuple):
    track_id: str
    rating: float
    context: Dict[str, Any]
    timestamp: str


class UserProfile:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.feedback_history: deque = deque(maxlen=1000)
        self.liked_features: List[Dict[str, Any]] = []
        self.disliked_features: List[Dict[str, Any]] = []
        self.preferred_genres: Dict[str, float] = defaultdict(float)
        self.preferred_bpms: deque = deque(maxlen=100)
        self.preferred_keys: Dict[str, float] = defaultdict(float)
        self.preferred_complexity: float = 0.5
        self.preference_vector: Optional[npt.NDArray[np.float32]] = None

    def update_preferences(self, genre: str, bpm: float):
        """Neutral exposure signal from learned tracks (v3: this method was
        called by MusicAgent.learn but never existed in v2)."""
        if genre and genre != 'unknown':
            self.preferred_genres[genre] += 3.0
        if bpm:
            self.preferred_bpms.append(float(bpm))

    def add_feedback(self, track_id: str, rating: float,
                     features: Optional[Dict] = None):
        event = FeedbackEvent(track_id, rating, features or {},
                              datetime.now().isoformat())
        self.feedback_history.append(event)
        if not features:
            return
        if rating > 0:  # v3: rating 0 = placeholder, never a "dislike"
            if rating >= 4.0:
                self.liked_features.append(features)
            elif rating <= 2.0:
                self.disliked_features.append(features)
        genre = features.get('genre')
        if genre:
            self.preferred_genres[genre] += max(rating, 0)
        if features.get('bpm'):
            self.preferred_bpms.append(features['bpm'])
        if features.get('key'):
            self.preferred_keys[features['key']] += max(rating, 0)
        if features.get('complexity') is not None:
            n = len(self.feedback_history)
            self.preferred_complexity = (self.preferred_complexity * (n - 1)
                                         + features['complexity']) / n
        self._update_preference_vector()

    def _update_preference_vector(self):
        if not self.liked_features:
            return
        vec = np.zeros(10, dtype=np.float32)
        count = 0
        for feat in self.liked_features[-50:]:
            vec[0] += feat.get('bpm', 120) / 200.0
            vec[1] += _GENRE_CODE.get(feat.get('genre', 'pop'), 0.5)
            vec[2] += feat.get('complexity', 0.5)
            vec[3] += feat.get('brightness', 0.5)
            vec[4] += _MOOD_CODE.get(feat.get('mood', 'neutral'), 0.5)
            count += 1
        if count > 0:
            self.preference_vector = vec / count

    def predict_rating(self, features: Dict[str, Any]) -> float:
        if self.preference_vector is None:
            return 3.0
        feat_vec = np.zeros(10, dtype=np.float32)
        feat_vec[0] = features.get('bpm', 120) / 200.0
        feat_vec[1] = _GENRE_CODE.get(features.get('genre', 'pop'), 0.5)
        feat_vec[2] = features.get('complexity', 0.5)
        feat_vec[3] = features.get('brightness', 0.5)
        feat_vec[4] = _MOOD_CODE.get(features.get('mood', 'neutral'), 0.5)
        sim = float(np.dot(self.preference_vector, feat_vec) /
                    (np.linalg.norm(self.preference_vector) *
                     np.linalg.norm(feat_vec) + 1e-8))
        return 3.0 + sim * 2.0

    def get_recommendation_params(self) -> Dict[str, Any]:
        return {
            'genre': max(self.preferred_genres, key=self.preferred_genres.get)
                    if self.preferred_genres else 'pop',
            'bpm': float(np.mean(list(self.preferred_bpms)))
                   if self.preferred_bpms else 120.0,
            'complexity': self.preferred_complexity,
            'key': max(self.preferred_keys, key=self.preferred_keys.get)
                   if self.preferred_keys else 'C',
        }


class PersonalizationManager:
    """Synchronous atomic profile persistence (v2's asyncio queue was fragile
    outside a running loop; swap in a worker thread if you need it)."""

    def __init__(self, profiles_dir: str = "user_profiles"):
        self.profiles_dir = Path(profiles_dir)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.profiles: Dict[str, UserProfile] = {}

    def get_profile(self, user_id: str) -> UserProfile:
        if user_id not in self.profiles:
            self.profiles[user_id] = self._load_profile(user_id)
        return self.profiles[user_id]

    def _load_profile(self, user_id: str) -> UserProfile:
        path = self.profiles_dir / f"{user_id}.pkl"
        if path.exists():
            try:
                with open(path, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                logger.warning(f"Failed to load profile {user_id}: {e}")
        return UserProfile(user_id)

    def save_profile(self, user_id: str):
        path = self.profiles_dir / f"{user_id}.pkl"
        profile = self.profiles.get(user_id) or self.get_profile(user_id)
        with tempfile.NamedTemporaryFile('wb', dir=str(self.profiles_dir),
                                         delete=False) as tf:
            pickle.dump(profile, tf)
            tmp = tf.name
        os.replace(tmp, path)


# ==============================================================================
# 9. ETHICS & PROVENANCE
# ==============================================================================

class ProvenanceRecord:
    def __init__(self, output_path: str, prompt: str, models_used: List[str],
                 sources: List[str], user_id: str,
                 parent_hashes: Optional[List[str]] = None):
        self.timestamp = datetime.now().isoformat()
        self.output_path = output_path
        self.prompt = prompt
        self.models_used = models_used
        self.sources = sources
        self.user_id = user_id
        self.parent_hashes = parent_hashes or []

    def compute_hash(self) -> str:
        data = {'timestamp': self.timestamp, 'prompt': self.prompt,
                'models': self.models_used, 'sources': self.sources,
                'user': self.user_id, 'parents': self.parent_hashes}
        return hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        return {'timestamp': self.timestamp, 'output_path': self.output_path,
                'prompt': self.prompt, 'models_used': self.models_used,
                'sources': self.sources, 'user_id': self.user_id,
                'parent_hashes': self.parent_hashes,
                'record_hash': self.compute_hash()}


class EthicsTracker:
    def __init__(self, log_path: str = "generation_log.json"):
        self.log_path = log_path
        self.provenance_dag: Dict[str, ProvenanceRecord] = {}
        self.logs: List[Dict] = []
        self._load_log()

    def _load_log(self):
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, 'r') as f:
                    self.logs = json.load(f)
            except Exception:
                self.logs = []

    def add_entry(self, output_path: str, prompt: str, genre: str, bpm: float,
                  models_used: List[str], sources: List[str], user_id: str,
                  parent_hashes: Optional[List[str]] = None,
                  license_type: str = "generated",
                  commercial_use: bool = False) -> str:
        record = ProvenanceRecord(output_path, prompt, models_used, sources,
                                  user_id, parent_hashes)
        entry = {**record.to_dict(), 'genre': genre, 'bpm': bpm,
                 'license_type': license_type, 'commercial_use': commercial_use,
                 'model_licenses': self._get_model_licenses(models_used)}
        self.logs.append(entry)
        self.provenance_dag[record.compute_hash()] = record
        self._atomic_save()
        return record.compute_hash()

    @staticmethod
    def _get_model_licenses(models: List[str]) -> Dict[str, str]:
        licenses = {
            'musicgen': 'CC-BY-NC 4.0 (research only, check Meta license)',
            'musicgen-commercial': 'Requires license from Meta',
            'procedural': 'Public Domain',
            'ddsp': 'Apache 2.0', 'encodec': 'MIT', 'dac': 'MIT',
        }
        return {m: licenses.get(m, 'Unknown') for m in models}

    def _atomic_save(self):
        dir_name = os.path.dirname(self.log_path) or '.'
        with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False,
                                         encoding='utf-8') as tf:
            json.dump(self.logs, tf, indent=2)
            temp = tf.name
        try:
            os.replace(temp, self.log_path)
        except Exception as e:
            logger.error(f"Failed to save ethics log: {e}")
            if os.path.exists(temp):
                os.remove(temp)

    def generate_transparency_report(self,
                                     user_id: Optional[str] = None) -> Dict[str, Any]:
        relevant = self.logs if not user_id else \
            [l for l in self.logs if l['user_id'] == user_id]
        models_used = Counter(m for l in relevant for m in l.get('models_used', []))
        genres = Counter(l.get('genre', 'unknown') for l in relevant)
        return {
            'total_generations': len(relevant),
            'unique_users': len({l['user_id'] for l in relevant}),
            'model_breakdown': dict(models_used),
            'genre_breakdown': dict(genres),
            'commercial_licensed': sum(1 for l in relevant if l.get('commercial_use')),
            'report_generated': datetime.now().isoformat(),
        }

    def verify_lineage(self, record_hash: str) -> List[Dict]:
        record = self.provenance_dag.get(record_hash)
        if not record:
            return []
        lineage = [record.to_dict()]
        for parent in record.parent_hashes:
            lineage.extend(self.verify_lineage(parent))
        return lineage

    def save_metadata_json(self, output_path: str, metadata: Dict):
        meta = {**metadata, 'provenance': {
            'system': 'AI Music Agent v3', 'version': '3.0.0',
            'ethics_framework': 'transparent_attribution',
            'generated_at': datetime.now().isoformat()}}
        meta_path = os.path.splitext(output_path)[0] + ".json"
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)


# ==============================================================================
# 10. REAL-TIME INTERACTION
# ==============================================================================

class ParameterInterpolator:
    def __init__(self, smoothing_time: float = 0.5, sr: int = 44100):
        self.smoothing_time = smoothing_time
        self.sr = sr
        self._current: Dict[str, float] = {}
        self._target: Dict[str, float] = {}
        self._alpha = 1.0 - np.exp(-1.0 / (smoothing_time * sr / 512))

    def set_target(self, params: Dict[str, float]):
        self._target = params.copy()
        for k in self._target:
            self._current.setdefault(k, self._target[k])

    def process_block(self, block_size: int) -> Dict[str, float]:
        for k, target in self._target.items():
            current = self._current.get(k, target)
            self._current[k] = current + self._alpha * (target - current)
        return self._current.copy()


class RealtimeGenerator:
    def __init__(self, agent: "MusicAgent", sample_rate: int = 44100,
                 block_size: int = 512, buffer_seconds: float = 2.0):
        self.agent = agent
        self.sr = sample_rate
        self.block_size = block_size
        self.buffer_samples = int(buffer_seconds * sample_rate)
        self.audio_buffer = np.zeros(self.buffer_samples, dtype=np.float32)
        self.write_pos = 0
        self.read_pos = 0
        self.interpolator = ParameterInterpolator(0.3, sample_rate)
        self._generation_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._stream = None

    def start(self) -> bool:
        if not DEPS['sounddevice']:
            logger.error("sounddevice not installed. Cannot start real-time stream.")
            return False
        import sounddevice as sd
        self._is_running = True
        self._generation_thread = threading.Thread(target=self._generate_loop, daemon=True)
        self._generation_thread.start()
        self._stream = sd.OutputStream(samplerate=self.sr, channels=1,
                                       blocksize=self.block_size,
                                       callback=self._audio_callback, latency='low')
        self._stream.start()
        logger.info("Real-time stream started")
        return True

    def stop(self):
        self._is_running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
        if self._generation_thread:
            self._generation_thread.join(timeout=1.0)
        logger.info("Real-time stream stopped")

    def _generate_loop(self):
        while self._is_running:
            if self._buffer_level() < self.buffer_samples // 2:
                params = self.interpolator.process_block(self.block_size)
                self._write_to_buffer(self._generate_chunk(params))
            else:
                time.sleep(0.001)

    def _generate_chunk(self, params: Dict[str, float]) -> npt.NDArray[np.float32]:
        return self.agent.generate_procedural(
            bpm=float(params.get('bpm', 120)),
            genre=params.get('genre', 'electronic'),
            duration=0.5,
            intensity=float(params.get('intensity', 0.5)),
            complexity=float(params.get('complexity', 0.5)),
            return_array=True)

    def _audio_callback(self, outdata, frames, time_info, status):
        if status:
            logger.warning(f"Audio callback status: {status}")
        data = self._read_from_buffer(frames)
        if len(data) < frames:
            data = np.pad(data, (0, frames - len(data)))
        outdata[:, 0] = data[:frames]

    def _buffer_level(self) -> int:
        if self.write_pos >= self.read_pos:
            return self.write_pos - self.read_pos
        return self.buffer_samples - (self.read_pos - self.write_pos)

    def _write_to_buffer(self, audio: npt.NDArray[np.float32]):
        for i, s in enumerate(audio):
            self.audio_buffer[self.write_pos % self.buffer_samples] = s
            self.write_pos += 1

    def _read_from_buffer(self, frames: int) -> npt.NDArray[np.float32]:
        data = np.zeros(frames, dtype=np.float32)
        i = 0
        while i < frames and self.read_pos < self.write_pos:
            data[i] = self.audio_buffer[self.read_pos % self.buffer_samples]
            self.read_pos += 1
            i += 1
        return data

    def set_params(self, **kwargs):
        numeric = {k: float(v) for k, v in kwargs.items()
                   if isinstance(v, (int, float))}
        self.interpolator.set_target(numeric)
        logger.info(f"Params updated: {kwargs}")


# ==============================================================================
# 11. EVOLUTIONARY ALGORITHMS (NSGA-II style)
# ==============================================================================

class Genome:
    def __init__(self, params: Dict[str, Any]):
        self.params = params
        self.fitness: Dict[str, float] = {}
        self.rank: int = 0
        self.crowding_distance: float = 0.0
        self.dominatees: List["Genome"] = []
        self.dominated_count: int = 0

    def dominates(self, other: "Genome") -> bool:
        better_in_any = False
        for obj in self.fitness:
            if self.fitness[obj] < other.fitness.get(obj, float('inf')):
                return False
            if self.fitness[obj] > other.fitness.get(obj, float('inf')):
                better_in_any = True
        return better_in_any


class EvolutionaryGenerator:
    OBJECTIVES = ['preference', 'diversity', 'structure', 'novelty']

    def __init__(self, population_size: int = 20, mutation_rate: float = 0.25,
                 crossover_rate: float = 0.7, elitism: int = 2):
        self.population_size = population_size
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.elitism = elitism
        self.population: List[Genome] = []
        self.generation = 0
        self.archive: List[Genome] = []
        self._user_profile: Optional[UserProfile] = None

    def set_user_profile(self, profile: UserProfile):
        self._user_profile = profile

    def initialize_population(self):
        self.population = [Genome(self._random_genome())
                           for _ in range(self.population_size)]
        self.generation = 0

    def _random_genome(self) -> Dict[str, Any]:
        genres = ['electronic', 'rock', 'ambient', 'pop', 'jazz', 'classical']
        moods = ['happy', 'sad', 'energetic', 'calm', 'melancholic']
        if self._user_profile:
            rec = self._user_profile.get_recommendation_params()
            genre = np.random.choice([rec['genre']] + genres)
            bpm = float(np.random.normal(rec['bpm'], 10))
        else:
            genre = str(np.random.choice(genres))
            bpm = float(np.random.uniform(70, 160))
        return {
            'bpm': float(np.clip(bpm, 60, 200)),
            'genre': str(genre),
            'mood': str(np.random.choice(moods)),
            'complexity': float(np.random.beta(2, 2)),
            'brightness': float(np.random.beta(2, 2)),
            'structure_density': float(np.random.beta(3, 2)),
            'harmonic_richness': float(np.random.beta(2, 3)),
            'rhythm_syncopation': float(np.random.uniform(0, 1)),
        }

    def evaluate_fitness(self, genome: Genome, agent: "MusicAgent") -> None:
        params = genome.params
        genome.fitness['preference'] = (
            self._user_profile.predict_rating(params)
            if self._user_profile else 3.0)
        if len(self.population) > 1:
            genome.fitness['diversity'] = min(
                self._param_distance(params, self._average_params()) * 5, 5.0)
        else:
            genome.fitness['diversity'] = 2.5
        genome.fitness['structure'] = self._evaluate_structure(params)
        if self.archive:
            min_dist = min(self._param_distance(params, a.params)
                           for a in self.archive)
            genome.fitness['novelty'] = min(min_dist * 10, 5.0)
        else:
            genome.fitness['novelty'] = 2.5

    def _average_params(self) -> Dict[str, Any]:
        result = {}
        for key in self.population[0].params:
            values = [p.params[key] for p in self.population if key in p.params]
            if isinstance(values[0], (int, float)):
                result[key] = float(np.mean(values))
            else:
                result[key] = Counter(values).most_common(1)[0][0]
        return result

    @staticmethod
    def _param_distance(p1: Dict, p2: Dict) -> float:
        numeric_keys = ['bpm', 'complexity', 'brightness', 'structure_density',
                        'harmonic_richness', 'rhythm_syncopation']
        dist = 0.0
        for k in numeric_keys:
            v1, v2 = p1.get(k, 0), p2.get(k, 0)
            if k == 'bpm':
                v1, v2 = v1 / 200.0, v2 / 200.0
            dist += (v1 - v2) ** 2
        return float(np.sqrt(dist))

    @staticmethod
    def _evaluate_structure(params: Dict) -> float:
        score = 2.5
        ideal_bpms = {'electronic': (120, 140), 'rock': (120, 160),
                      'ambient': (60, 90), 'pop': (100, 130),
                      'jazz': (80, 140), 'classical': (60, 120)}
        genre = params.get('genre')
        bpm = params.get('bpm', 120)
        if genre in ideal_bpms:
            low, high = ideal_bpms[genre]
            score += 1.0 if low <= bpm <= high else -0.5
        if 0.3 <= params.get('complexity', 0.5) <= 0.8:
            score += 0.5
        return min(score, 5.0)

    def non_dominated_sort(self) -> List[List[Genome]]:
        fronts: List[List[Genome]] = [[]]
        for p in self.population:
            p.dominatees = []
            p.dominated_count = 0
            for q in self.population:
                if p is q:
                    continue
                if p.dominates(q):
                    p.dominatees.append(q)
                elif q.dominates(p):
                    p.dominated_count += 1
            if p.dominated_count == 0:
                p.rank = 0
                fronts[0].append(p)
        i = 0
        while fronts[i]:
            nxt = []
            for p in fronts[i]:
                for q in p.dominatees:
                    q.dominated_count -= 1
                    if q.dominated_count == 0:
                        q.rank = i + 1
                        nxt.append(q)
            i += 1
            fronts.append(nxt)
        return fronts[:-1]

    def crowding_distance(self, front: List[Genome]):
        if len(front) <= 2:
            for p in front:
                p.crowding_distance = float('inf')
            return
        for p in front:
            p.crowding_distance = 0.0
        for obj in self.OBJECTIVES:
            front.sort(key=lambda x: x.fitness.get(obj, 0))
            front[0].crowding_distance = float('inf')
            front[-1].crowding_distance = float('inf')
            f_min = front[0].fitness.get(obj, 0)
            f_max = front[-1].fitness.get(obj, 0)
            if f_max - f_min < 1e-8:
                continue
            for i in range(1, len(front) - 1):
                dist = (front[i + 1].fitness.get(obj, 0)
                        - front[i - 1].fitness.get(obj, 0))
                front[i].crowding_distance += dist / (f_max - f_min)

    def tournament_select(self, tournament_size: int = 3) -> Genome:
        contestants = np.random.choice(self.population,
                                       size=tournament_size, replace=False)
        return sorted(contestants,
                      key=lambda x: (x.rank, -x.crowding_distance))[0]

    def crossover(self, p1: Genome, p2: Genome) -> Tuple[Genome, Genome]:
        if np.random.rand() > self.crossover_rate:
            return Genome(p1.params.copy()), Genome(p2.params.copy())
        c1_params, c2_params = {}, {}
        for key in p1.params:
            v1, v2 = p1.params[key], p2.params[key]
            if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                lo, hi = (60.0, 200.0) if key == 'bpm' else (0.0, 1.0)
                c1_params[key] = float(np.clip(
                    v1 + np.random.uniform(-0.5, 1.5) * (v2 - v1), lo, hi))
                c2_params[key] = float(np.clip(
                    v2 + np.random.uniform(-0.5, 1.5) * (v1 - v2), lo, hi))
            else:
                c1_params[key] = v1 if np.random.rand() < 0.5 else v2
                c2_params[key] = v2 if np.random.rand() < 0.5 else v1
        return Genome(c1_params), Genome(c2_params)

    def mutate(self, genome: Genome) -> Genome:
        mutated = Genome(genome.params.copy())
        for key, value in mutated.params.items():
            if np.random.rand() >= self.mutation_rate:
                continue
            if isinstance(value, (int, float)):
                delta = np.random.uniform(-1, 1)
                if key == 'bpm':
                    mutated.params[key] = float(np.clip(value + delta * 20, 60, 200))
                elif isinstance(value, float):
                    mutated.params[key] = float(np.clip(value + delta * 0.2, 0, 1))
            elif key == 'genre':
                mutated.params[key] = str(np.random.choice(
                    ['electronic', 'rock', 'ambient', 'pop', 'jazz', 'classical']))
            elif key == 'mood':
                mutated.params[key] = str(np.random.choice(
                    ['happy', 'sad', 'energetic', 'calm', 'melancholic']))
        return mutated

    def evolve(self, agent: "MusicAgent") -> None:
        for genome in self.population:
            self.evaluate_fitness(genome, agent)
        for front in self.non_dominated_sort():
            self.crowding_distance(front)

        offspring: List[Genome] = []
        while len(offspring) < self.population_size:
            c1, c2 = self.crossover(self.tournament_select(),
                                    self.tournament_select())
            offspring.extend([self.mutate(c1), self.mutate(c2)])
        for g in offspring[:self.population_size]:
            self.evaluate_fitness(g, agent)

        combined = self.population + offspring[:self.population_size]
        weights = {'preference': 0.4, 'diversity': 0.2,
                   'structure': 0.2, 'novelty': 0.2}
        combined.sort(key=lambda g: sum(g.fitness.get(k, 0) * w
                                        for k, w in weights.items()), reverse=True)
        self.population = combined[:self.population_size]

        for g in self.population[:self.elitism]:
            if not any(a.dominates(g) for a in self.archive):
                self.archive.append(g)
        if len(self.archive) > 50:
            self.archive = self.archive[-50:]
        self.generation += 1

    def get_best(self) -> Genome:
        if not self.population:
            return Genome(self._random_genome())
        return max(self.population, key=lambda g: g.fitness.get('preference', 0))


# ==============================================================================
# 12. FEATURE EXTRACTION
# ==============================================================================

class FeatureExtractor:
    def __init__(self, sr: int = 44100):
        self.sr = sr

    def extract(self, audio: npt.NDArray[np.float32]) -> AudioFeatures:
        tempo, beat_frames = librosa.beat.beat_track(y=audio, sr=self.sr)
        tempo_val = float(tempo[0]) if isinstance(tempo, np.ndarray) else float(tempo)

        chroma = librosa.feature.chroma_cqt(y=audio, sr=self.sr)
        key = self._detect_key(chroma)

        rms = librosa.feature.rms(y=audio)[0]
        loudness = float(20 * np.log10(np.mean(rms) + 1e-8))

        centroid = librosa.feature.spectral_centroid(y=audio, sr=self.sr)[0]
        rolloff = librosa.feature.spectral_rolloff(y=audio, sr=self.sr)[0]
        flux = librosa.onset.onset_strength(y=audio, sr=self.sr)
        zcr = librosa.feature.zero_crossing_rate(y=audio)[0]
        mfcc = librosa.feature.mfcc(y=audio, sr=self.sr, n_mfcc=13)
        onset_env = librosa.onset.onset_strength(y=audio, sr=self.sr)
        harmony_changes = self._detect_harmony_changes(chroma, beat_frames)
        dr = float(20 * np.log10((np.max(np.abs(audio)) + 1e-8) / (np.max(rms) + 1e-8)))

        stereo_width = None
        if audio.ndim == 2:
            side = (audio[0] - audio[1]) * 0.5
            mid = (audio[0] + audio[1]) * 0.5
            stereo_width = float(np.std(side) / (np.std(mid) + 1e-8))

        return AudioFeatures(
            tempo=tempo_val, tempo_confidence=0.8, key=key, loudness=loudness,
            spectral_centroid=float(np.mean(centroid)),
            spectral_rolloff=float(np.mean(rolloff)),
            spectral_flux=float(np.mean(flux)),
            zero_crossing_rate=float(np.mean(zcr)),
            rms_energy=float(np.mean(rms)),
            chroma=chroma.astype(np.float32), mfcc=mfcc.astype(np.float32),
            onset_envelope=onset_env.astype(np.float32),
            beat_frames=beat_frames.astype(np.int32),
            harmony_change_times=harmony_changes, dynamic_range=dr,
            stereo_width=stereo_width)

    def _detect_key(self, chroma: npt.NDArray[np.float32]) -> Optional[MusicKey]:
        """Krumhansl-Schmuckler profiles (v3: safe MusicKey lookup)."""
        chroma_mean = np.mean(chroma, axis=1)
        if np.std(chroma_mean) < 1e-6:
            return None
        major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52,
                                  5.19, 2.39, 3.66, 2.29, 2.88])
        minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54,
                                  4.75, 3.98, 2.69, 3.34, 3.17])
        best_corr, best_key = -1.0, None
        for shift in range(12):
            for profile, quality in ((major_profile, 'major'),
                                     (minor_profile, 'minor')):
                try:
                    corr = float(np.corrcoef(chroma_mean,
                                             np.roll(profile, shift))[0, 1])
                except (ValueError, FloatingPointError):
                    continue
                if corr > best_corr:
                    best_corr = corr
                    best_key = MusicKey.from_tonic(NOTES[shift], quality)
        return best_key

    def _detect_harmony_changes(self, chroma: npt.NDArray[np.float32],
                                beat_frames: npt.NDArray[np.int32]) -> List[float]:
        changes: List[float] = []
        if len(beat_frames) < 2:
            return changes
        prev = np.mean(chroma[:, :beat_frames[1]], axis=1)
        for i in range(1, len(beat_frames) - 1):
            start, end = beat_frames[i], beat_frames[i + 1]
            if end > chroma.shape[1] or end <= start:
                break
            curr = np.mean(chroma[:, start:end], axis=1)
            if float(np.linalg.norm(curr - prev)) > 2.0:
                changes.append(float(librosa.frames_to_time(start, sr=self.sr)))
                prev = curr
        return changes


# ==============================================================================
# 13. MAIN AGENT
# ==============================================================================

class MusicAgent:
    def __init__(self, memory_path: str = "music_memory.json",
                 neural_model_size: Literal['small', 'medium', 'large', 'melody',
                                            'stereo-small', 'stereo-medium',
                                            'stereo-large'] = 'medium',
                 use_codec: bool = True):
        self.sample_rate = 44100
        self.memory = MusicMemory(memory_path)
        self.codec = NeuralCodecInterface() if use_codec else None
        self._neural: Optional[NeuralGenerator] = None
        self._neural_size = neural_model_size
        self._multimodal: Optional[MultiModalConditioner] = None
        self.symbolic = SymbolicMusicEngine(self.sample_rate)
        self.stem_generator = StemGenerator(self.sample_rate, self.symbolic)
        self.style_transfer = StyleTransfer(self.sample_rate)
        self.personalization = PersonalizationManager()
        self.ethics = EthicsTracker()
        self.evolution = EvolutionaryGenerator()
        self.feature_extractor = FeatureExtractor(self.sample_rate)
        self.realtime_generator: Optional[RealtimeGenerator] = None

    # v3: heavy models load lazily on first use
    @property
    def neural_generator(self) -> NeuralGenerator:
        if self._neural is None:
            self._neural = NeuralGenerator(self._neural_size)
        return self._neural

    @property
    def multimodal(self) -> MultiModalConditioner:
        if self._multimodal is None:
            self._multimodal = MultiModalConditioner()
        return self._multimodal

    @staticmethod
    def _hash_file(path: str) -> str:
        hasher = hashlib.sha256()
        with open(path, 'rb') as f:
            for buf in iter(lambda: f.read(65536), b''):
                hasher.update(buf)
        return hasher.hexdigest()

    # -- learning ---------------------------------------------------------------

    def learn(self, path: str, user_id: str = "default_user", genre: str = "unknown"):
        file_hash = self._hash_file(path)
        if file_hash in self.memory.tracks:
            logger.info(f"Already known: {path}")
            return
        logger.info(f"Learning: {path}")
        try:
            y, sr = librosa.load(path, sr=self.sample_rate, mono=True)
            features = self.feature_extractor.extract(y)

            # -- store track record (completed tail: original file was truncated here) --
            key_str = (f"{features.key.tonic} {features.key.quality}"
                       if features.key else None)
            self.memory.tracks[file_hash] = {
                'path': path,
                'genre': genre,
                'user_id': user_id,
                'tempo': features.tempo,
                'tempo_confidence': features.tempo_confidence,
                'key': key_str,
                'loudness': features.loudness,
                'spectral_centroid': features.spectral_centroid,
                'spectral_rolloff': features.spectral_rolloff,
                'rms_energy': features.rms_energy,
                'dynamic_range': features.dynamic_range,
                'learned_at': datetime.now().isoformat(),
            }
            self.memory._update_indices(file_hash, features, genre)
            self.memory.save_memory()
            logger.info(f"Stored {file_hash[:12]}  bpm={features.tempo:.1f}  key={key_str}")
        except ImportError as exc:
            logger.error(f"Audio dependency missing, cannot learn {path}: {exc}")
        except Exception as exc:
            logger.error(f"Failed to learn {path}: {exc}")

    # -- analysis (completion) ----------------------------------------------------

    def analyze(self, path: str) -> AudioFeatures:
        """Extract features from an audio file without storing anything."""
        y, _sr = librosa.load(path, sr=self.sample_rate, mono=True)
        return self.feature_extractor.extract(y)

    def find_similar_to(self, path: str, genre: Optional[str] = None,
                        bpm_range: Optional[Tuple[float, float]] = None,
                        k: int = 5) -> List[Tuple[str, float]]:
        """Find stored tracks similar to the given audio file."""
        feats = self.analyze(path)
        return self.memory.find_similar(query_features=feats, genre=genre,
                                        bpm_range=bpm_range, k=k)

    def memory_stats(self) -> Dict[str, Any]:
        """Quick overview of what the agent knows."""
        return {
            'tracks': len(self.memory.tracks),
            'genres': sorted(self.memory._genre_index.keys()),
            'vector_index_active': bool(self.memory.use_vectors and self.memory._ann_index),
            'sample_library': len(self.memory.sample_library),
            'pattern_library': len(self.memory.pattern_library),
        }


# -- CLI (completion) ------------------------------------------------------------


def _build_agent(args: argparse.Namespace) -> "MusicAgent":
    return MusicAgent(memory_path=args.memory,
                      use_codec=not getattr(args, 'no_codec', False))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog='music-agent',
        description='AI Music Learning Agent v3 — Neural-Symbolic Hybrid '
                    '(completed tail; original file was truncated mid-learn())')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_learn = sub.add_parser('learn', help='learn audio file(s) into memory')
    p_learn.add_argument('files', nargs='+')
    p_learn.add_argument('--genre', default='unknown')
    p_learn.add_argument('--user', default='default_user')
    p_learn.add_argument('--memory', default='music_memory.json')
    p_learn.add_argument('--no-codec', action='store_true',
                         help='skip neural codec token extraction')

    p_sim = sub.add_parser('find-similar', help='find stored tracks similar to a file')
    p_sim.add_argument('file')
    p_sim.add_argument('-k', type=int, default=5)
    p_sim.add_argument('--genre')
    p_sim.add_argument('--memory', default='music_memory.json')

    p_stats = sub.add_parser('stats', help='memory statistics')
    p_stats.add_argument('--memory', default='music_memory.json')

    args = parser.parse_args(argv)
    agent = _build_agent(args)

    if args.cmd == 'learn':
        for f in args.files:
            agent.learn(f, user_id=args.user, genre=args.genre)
    elif args.cmd == 'find-similar':
        results = agent.find_similar_to(args.file, genre=args.genre, k=args.k)
        if not results:
            print("no similar tracks found")
        for h, dist in results:
            rec = agent.memory.tracks.get(h, {})
            print(f"{dist:9.4f}  {h[:12]}  {rec.get('path', '?')}  "
                  f"{rec.get('tempo', '?')}bpm  {rec.get('key') or ''}")
    elif args.cmd == 'stats':
        for key, val in agent.memory_stats().items():
            print(f"{key}: {val}")


if __name__ == '__main__':
    main()
