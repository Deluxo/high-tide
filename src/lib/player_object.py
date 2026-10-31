# player_object.py
#
# Copyright 2023 Nokse
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
import random
import signal
import subprocess
import threading
import time
import base64
from enum import IntEnum
from gettext import gettext as _
from pathlib import Path
from typing import Any, List, Union

from gi.repository import GLib, GObject, Gst, Gio

from tidalapi.album import Album
from tidalapi.artist import Artist
from tidalapi.mix import Mix
from tidalapi.playlist import Playlist
from tidalapi.media import Track, ManifestMimeType

from . import discord_rpc, utils

logger = logging.getLogger(__name__)


class RepeatType(IntEnum):
    NONE = 0
    SONG = 1
    LIST = 2


class AudioSink(IntEnum):
    AUTO = 0
    PULSE = 1
    ALSA = 2
    JACK = 3
    OSS = 4
    PIPEWIRE = 5


class PlayerObject(GObject.GObject):
    """Handles player logic, queue, and shuffle functionality."""

    current_song_index = GObject.Property(type=int, default=-1)
    can_go_next = GObject.Property(type=bool, default=True)
    can_go_prev = GObject.Property(type=bool, default=True)

    __gsignals__ = {
        "songs-list-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "update-slider": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "song-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "song-added-to-queue": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "duration-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "volume-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "buffering": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(
        self,
        preferred_sink: AudioSink = AudioSink.AUTO,
        alsa_device: str = "default",
        normalize: bool = False,
        quadratic_volume: bool = False,
    ) -> None:
        GObject.GObject.__init__(self)

        Gst.init(None)

        version_str = Gst.version_string()
        logger.info(f"GStreamer version: {version_str}")

        self.pipeline = Gst.Pipeline.new("dash-player")

        self.playbin = Gst.ElementFactory.make("playbin3", "playbin")
        if self.playbin:
            self.playbin.connect("about-to-finish", self.play_next_gapless)
            self.gapless_enabled = True
        else:
            logger.error("Could not create playbin3 element, trying playbin...")
            self.playbin = Gst.ElementFactory.make("playbin", "playbin")
            self.gapless_enabled = False

        if preferred_sink == AudioSink.PIPEWIRE:
            self.gapless_enabled = False

        self.use_about_to_finish = True

        self.pipeline.add(self.playbin)

        # Configure the pipeline elements that playbin3 creates
        self.playbin.connect("element-setup", self._on_element_setup)

        self.normalize = normalize
        self.quadratic_volume = quadratic_volume
        self.most_recent_rg_tags = ""

        self.discord_rpc_enabled = True

        self.alsa_device: str = alsa_device
        # Configure audio sink
        self._setup_audio_sink(preferred_sink)

        # Set up message bus
        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::eos", self._on_bus_eos)
        self._bus.connect("message::error", self._on_bus_error)
        self._bus.connect("message::buffering", self._on_buffering_message)
        self._bus.connect("message::stream-start", self._on_track_start)

        # Initialize state utils
        self._shuffle = False
        self._playing = False
        self._repeat_type = RepeatType.NONE

        self.id_list: List[str] = []

        self.queue: List[Track] = []
        self.current_mix_album_playlist: Union[Mix, Album, Playlist] | None = None
        self._tracks_to_play: List[Track] = []
        self.tracks_to_play: List[Track] = []
        self._shuffled_tracks_to_play: List[Track] = []
        self.played_songs: List[Track] = []
        self.playing_track: Track | None = None
        self.song_album: Album | None = None
        self.duration = self.query_duration()
        self.manifest: Any | None = None
        self.stream: Any | None = None
        self.update_timer: Any | None = None
        self.seek_after_sink_reload: int | None = None
        self.seeked_to_end = False
        self.last_stream_reload: int = 0
        # when the uri of the playing track was loaded, its stream urls expire after
        # about an hour (wall-clock time, to count time spent suspended)
        self.stream_loaded_at: float = 0
        # to notice when the stream of the playing track gets stuck
        self.last_position: int | None = None
        self.position_stuck_since: float = 0.0
        # how many times a stuck stream has been reconnected (reset on progress)
        self.stream_reconnects: int = 0
        self.MAX_STREAM_RECONNECTS: int = 5
        # the last observed buffering level and when it last grew, to tell a slow
        # download (buffer growing, leave it alone) from a dead connection
        self.last_buffer_per: int = -1
        self.buffer_grew_since: float = 0.0
        self.BUFFER_DEAD_FOR: float = 30.0
        # a stream that hasn't started playing within this long is too slow;
        # reconnect to try a faster edge instead of buffering forever
        self.MAX_STREAM_LOAD: float = 60.0
        self.stream_load_started: float = 0.0
        # where the last seek went, as the position can't be queried until it's done
        self.last_seek_position: int | None = None

        # next track variables for gapless
        self.next_track: Any | None = None
        # (track id, stream, manifest, fetch time) of the next track, fetched
        # before the current one ends so its uri can be set in about-to-finish
        self.prefetched: tuple | None = None
        self.prefetch_id: int | None = None
        # track whose uri was set in about-to-finish from the prefetched stream
        self.handed_off_track: Track | None = None
        # a gapless track is being loaded / the current one ended before it was
        self.loading_gapless = False
        self.reached_eos = False
        # the last track requested for gapless playback, dropped if another track
        # is played directly while it loads
        self.gapless_request: Track | None = None

        # for not caching on metered networks
        self.monitor = Gio.NetworkMonitor.get_default()

        # music cache: tracks get queued as they play, and the download only
        # runs while playback isn't using the network, so it doesn't starve
        # the stream of bandwidth
        self._cache_queue: List[Track] = []
        self._cache_queued_ids: set = set()
        self._cache_thread: threading.Thread | None = None
        self._cache_lock = threading.Lock()
        # the ffmpeg process of the running cache download, so it can be
        # stopped on quit instead of being orphaned
        self._cache_process: subprocess.Popen | None = None
        # download-then-play: a generation counter so a new track request
        # cancels a running pre-download, and its download process, so it can be
        # killed on cancellation or quit instead of being orphaned
        self._download_gen: int = 0
        self._download_process: subprocess.Popen | None = None
        self._download_lock = threading.Lock()

        self._clean_stale_manifests()

    @GObject.Property(type=bool, default=False)
    def playing(self) -> bool:
        return self._playing

    @playing.setter
    def playing(self, _playing: bool) -> None:
        self._playing = _playing
        self.notify("playing")

    @GObject.Property(type=bool, default=False)
    def shuffle(self) -> bool:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, _shuffle: bool) -> None:
        if self._shuffle == _shuffle:
            return

        self._shuffle = _shuffle
        self.notify("shuffle")
        self._update_shuffle_queue()
        # self.emit("song-changed")

    @GObject.Property(type=int, default=0)
    def repeat_type(self) -> RepeatType:
        return self._repeat_type

    @repeat_type.setter
    def repeat_type(self, _repeat_type: RepeatType) -> None:
        self._repeat_type = _repeat_type
        self.notify("repeat-type")

    def _setup_audio_sink(self, sink_type: AudioSink) -> None:
        """Configure the audio sink using parse_launch for simplicity."""
        sink_map = {
            AudioSink.AUTO: "autoaudiosink",
            AudioSink.PULSE: "pulsesink",
            AudioSink.ALSA: f"alsasink device={self.alsa_device}",
            AudioSink.JACK: "jackaudiosink",
            AudioSink.OSS: "osssink",
            AudioSink.PIPEWIRE: "pipewiresink",
        }

        sink_name = sink_map.get(sink_type, "autoaudiosink")

        # add normalization to pipeline if set by settings
        normalization = ""
        if self.normalize:
            # the pre-amp value is set to match tidal webs volume
            normalization = (
                f"taginject name=rgtags {self.most_recent_rg_tags} ! "
                f"rgvolume name=rgvol pre-amp=4.0 fallback-gain=-10 headroom=6.0 ! "
                f"rglimiter ! audioconvert !"
            )

        # Keep a large decoded-audio buffer so short network stalls don't stop
        # playback. The queue defaults to one second, which is far too little
        # for hi-res streams.
        buffer_ns = 30 * Gst.SECOND
        pipeline_str = (
            f"queue max-size-buffers=0 max-size-bytes=0 max-size-time={buffer_ns} ! "
            f"audioconvert ! {normalization} audioresample ! {sink_name}"
        )

        if sink_type == AudioSink.PIPEWIRE:
            self.gapless_enabled = False
        else:
            self.gapless_enabled = True

        try:
            audio_bin = Gst.parse_bin_from_description(pipeline_str, True)
            if not audio_bin:
                raise RuntimeError("Failed to create audio bin")

            self.playbin.set_property("audio-sink", audio_bin)
        except GLib.Error:
            logger.exception("Error creating pipeline")
            self.playbin.set_property(
                "audio-sink", Gst.ElementFactory.make("autoaudiosink", None)
            )

    def _on_element_setup(self, playbin: Any, element: Any) -> None:
        """Configure the elements that playbin3 creates for the pipeline."""
        # Buffer DASH fragments further ahead than the default 30 seconds, so
        # a slow segment doesn't stall playback
        if element.find_property("max-buffering-time"):
            element.set_property("max-buffering-time", 60)

    def change_audio_sink(self, sink_type: AudioSink) -> None:
        """Change the audio sink while maintaining playback state.

        Args:
            sink_type (int): The audio sink `AudioSink` enum
        """
        self.use_about_to_finish = False
        # Play the same track again after reload
        self.next_track = self.playing_track
        was_playing: bool = self.playing
        position: int = self.query_position()
        duration: int = self.query_duration()

        self.pipeline.set_state(Gst.State.NULL)
        self._setup_audio_sink(sink_type)

        if was_playing and duration != 0:
            self.pipeline.set_state(Gst.State.PLAYING)
            self.seek_after_sink_reload = position / duration
        self.use_about_to_finish = True

    def _on_bus_eos(self, *args) -> None:
        """Handle end of stream."""
        if not self.gapless_enabled:
            GLib.idle_add(self.play_next)
        elif self.loading_gapless:
            # The next uri wasn't set in time, _play_track_url will play it
            logger.warning("Track ended before the next one was loaded")
            self.reached_eos = True
        elif self.next_track or self.tracks_to_play or self.queue:
            logger.warning("Track ended without switching to the next one")
            self.play_next()
        elif not self._has_next_track():
            self.pause()

    def _on_bus_error(self, bus: Any, message: Any) -> None:
        """Handle pipeline errors."""
        err, debug = message.parse_error()
        logger.error(f"Error: {err.message}")
        logger.error(f"Debug info: {debug}")

        # Use string compare instead of error codes (Seems be just generic error)
        if "Internal data stream error" in err.message and "not-linked" in debug:
            logger.error(
                "Stream error: Element not linked. Attempting to restart pipeline..."
            )
            self.play_track(self.playing_track)

        elif (
            "Error outputting to audio device" in err.message
            and "disconnected" in err.message
        ):
            utils.send_toast(_("ALSA Audio Device is not available"), 5)
            self.pause()
            self.pipeline.set_state(Gst.State.NULL)

        # The stream URLs expire after a while (e.g. after a long pause or suspend),
        # so reload the track to get new ones and continue from the same position
        elif "Couldn't download fragments" in err.message:
            now = GLib.get_monotonic_time()
            if now - self.last_stream_reload < 30_000_000:
                return
            self.last_stream_reload = now

            logger.error(
                "Stream error: Couldn't download fragments. Reloading track..."
            )
            self._reload_track()

    def _reload_track(self) -> None:
        """Load the playing track again and continue from the same position"""
        duration = self.query_duration()
        if duration:
            self.seek_after_sink_reload = self.query_position() / duration
        self.play_track(self.playing_track)

    def _stream_expires_soon(self) -> bool:
        """Whether the stream urls of the playing track expire before it's downloaded"""
        if not self.playing_track or not self.stream_loaded_at or self.next_track:
            return False
        uri = self.playbin.get_property("current-uri") or ""
        if uri.startswith("file://") and not uri.endswith(".mpd"):
            # Playing from the music cache
            return False
        # They last about an hour, but very long tracks would never finish in time,
        # so only bother after at least a short pause
        age = time.time() - self.stream_loaded_at
        remaining = max(self.query_duration() - self.query_position(), 0) / Gst.SECOND
        return age > 10 * 60 and age + remaining > 50 * 60

    def _on_buffering_message(self, bus: Any, message: Any) -> None:
        buffer_per: int = message.parse_buffering()
        mode, avg_in, avg_out, buff_left = message.parse_buffering_stats()

        if buffer_per > self.last_buffer_per:
            # Data is arriving; a download is in progress
            self.buffer_grew_since = time.monotonic()
        self.last_buffer_per = buffer_per

        self.emit("buffering", buffer_per)

    def set_track(self, track: Track | None = None):
        """Sets the currently Playing track

        Args:
            track: If set, the playing track is set to it.
            Otherwise self.next_track is used
        """
        if not track and not self.next_track:
            # This method has already been called in _play_track_url
            return
        if track:
            self.playing_track = track
        else:
            self.playing_track = self.next_track
            self.next_track = None
        self.song_album = self.playing_track.album
        self.can_go_next = len(self._tracks_to_play) > 0
        self.can_go_prev = len(self.played_songs) > 0
        self.duration = self.query_duration()
        # Should only trigger when track is enqued on start without playback
        if not self.duration:
            # self.duration is microseconds, but self.playing_track.duration is seconds
            self.duration = self.playing_track.duration * 1_000_000_000
        self.notify("can-go-prev")
        self.notify("can-go-next")
        self.emit("song-changed")

    def _on_track_start(self, bus: Any, message: Any):
        """This Method is called when a new track starts playing

        Args:
            bus: required by Gst
            message: required by Gst
        """
        # apply replaygain first to avoid volume clipping
        # (Idk if that will happen but its the only thing that has effect on audio in here)
        if self.stream:
            self.apply_replaygain_tags()
        self.set_track()
        self.stream_loaded_at = time.time()

        # Cache the track in the background, when it's safe to do so
        self._queue_cache(self.playing_track)

        if self.discord_rpc_enabled and self.playing_track:
            discord_rpc.set_activity(self.playing_track, 0)

        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = GLib.timeout_add(1000, self._update_slider_callback)

        self.seeked_to_end = False
        self.last_seek_position = None
        if self.seek_after_sink_reload:
            # Continue exactly where it was, without repeating part of the track
            self.seek(self.seek_after_sink_reload, accurate=True)
            self.seek_after_sink_reload = None

        self.can_go_prev = len(self.played_songs) > 0
        # Only notify to deactivate
        if not self.can_go_prev:
            self.notify("can-go-prev")
            GLib.timeout_add(2000, self.previous_timer_callback)

    def play_this(
        self,
        thing: Union[Mix, Album, Playlist, List[Track], Track],
        index: int = 0,
        tracks: List[Track] | None = None,
    ) -> None:
        """Play tracks from a mix, album, playlist, or artist.

        Args:
            thing: An object (Mix, Album, Playlist, Artist, or list of Tracks) to play
            index (int): The index of the track to start playing (default: 0)
            tracks: The tracks of thing, if already fetched (default: None)
        """
        self.current_mix_album_playlist = thing
        if tracks is None:
            tracks = self.get_track_list(thing)

        if not tracks:
            logger.info("No tracks found to play")
            return

        self.id_list = [track.id for track in tracks]

        self._tracks_to_play = tracks[index:] + tracks[:index]
        if not self._tracks_to_play:
            return

        track: Track = self._tracks_to_play.pop(0)

        if not track.available:
            self.play_this(thing, index + 1, tracks)
        else:
            self.tracks_to_play = self._tracks_to_play
            self.played_songs = []

            if self.shuffle:
                self._update_shuffle_queue()

            # Will result in play() call later
            self.playing = True
            self.play_track(track)

    def shuffle_this(
        self, thing: Union[Mix, Album, Playlist, List[Track], Track]
    ) -> None:
        """Same as play_this, but enables shuffle mode.

        Args:
            thing: An object (Mix, Album, Playlist, Artist, or list of Tracks) to play
        """
        tracks: List[Track] = self.get_track_list(thing)
        self.play_this(thing, random.randint(0, len(tracks)), tracks)
        self.shuffle = True

    def get_track_list(
        self, thing: Union[Mix, Album, Playlist, Artist, List[Track], Track]
    ) -> List[Track]:
        """Convert various sources into a list of tracks.

        Args:
            thing: A TIDAL object (Mix, Album, Playlist, Artist, or list of Tracks)

        Returns:
            list: List of Track objects, or None if conversion failed
        """
        tracks_list: List[Track] | None = None

        if isinstance(thing, Mix):
            tracks_list = thing.items()
        elif isinstance(thing, Album):
            tracks_list = thing.tracks()
        elif isinstance(thing, Playlist):
            tracks_list = thing.tracks()
        elif isinstance(thing, Artist):
            tracks_list = thing.get_top_tracks()
        elif isinstance(thing, list):
            tracks_list = thing
        elif isinstance(thing, Track):
            tracks_list = [thing]

        return tracks_list

    def play(self) -> None:
        """Start playback of the current track."""
        self.playing = True
        if self._stream_expires_soon():
            # Resuming after a long pause: get new stream urls (or play the track from
            # the cache) now, instead of failing to download the rest of it
            logger.info("Stream urls expire soon, reloading track")
            self._reload_track()
            return
        self.pipeline.set_state(Gst.State.PLAYING)

        if self.discord_rpc_enabled and self.playing_track:
            discord_rpc.set_activity(
                self.playing_track, self.query_position() / 1_000_000_000
            )
        if self.update_timer:
            GLib.source_remove(self.update_timer)
        self.update_timer = GLib.timeout_add(1000, self._update_slider_callback)

    def pause(self) -> None:
        """Pause playback of the current track."""
        self.playing = False
        self.pipeline.set_state(Gst.State.PAUSED)

        # Caching can now use the network
        self._maybe_start_cache()

        if self.discord_rpc_enabled:
            discord_rpc.set_activity()

    def play_pause(self) -> None:
        """Toggle between play and pause states."""
        if self.playing:
            self.pause()
        else:
            self.play()

    def play_track(self, track: Track, gapless=False) -> None:
        """Play a specific track immediately or enqueue it for gapless playback

        Args:
            track: The Track object to play
            gapless: Whether to enqueue the track for gapless playback
        """
        handed_off_track = self.handed_off_track
        self.handed_off_track = None
        if gapless and track is handed_off_track:
            # The uri was already set in play_next_gapless
            self.next_track = track
            return
        if gapless:
            self.gapless_request = track
            self.loading_gapless = True
        threading.Thread(
            daemon=True,
            target=self._play_track_thread,
            args=(track, gapless),
        ).start()

    def _play_track_thread(self, track: Track, gapless=False) -> None:
        self.stream = None
        self.manifest = None

        # A new (non-gapless) track request cancels any running pre-download
        gen = self._cancel_download() if not gapless else 0

        cached_music = self._cached_music_path(track)
        if cached_music:
            self._play_cached_track(track, cached_music, gapless)
            return

        try:
            self.stream = track.get_stream()
            self.manifest = self.stream.get_stream_manifest()
            urls = self.manifest.get_urls()

            if not gapless:
                # Download the full track into the music cache before playing,
                # so playback reads from disk and doesn't depend on the CDN
                # sustaining the stream bitrate (it throttles below lossless)
                downloaded = self._download_track_to_cache(track, gen)
                if self._download_gen != gen:
                    # Cancelled by a newer track request
                    return
                if downloaded:
                    cached_music = self._cached_music_path(track)
                    if cached_music:
                        self._play_cached_track(track, cached_music, gapless)
                        # A local file doesn't post "buffering" bus messages, so
                        # hide the download spinner explicitly
                        GLib.idle_add(lambda: self.emit("buffering", 100))
                        return

            music_url = self._get_cached_or_stream_url(track, gapless)

            GLib.idle_add(self._play_track_url, track, music_url, gapless)
        except Exception:
            logger.exception("Error getting track URL")
            if gapless:
                self.loading_gapless = False
                if self.gapless_request is track:
                    self.gapless_request = None

    def _play_cached_track(self, track, cached_music, gapless=False) -> None:
        """Play a track from the music cache.

        Its stream info (ReplayGain, quality label) comes from TIDAL, but a slow or
        missing connection (e.g. offline, which retries for minutes) shouldn't hold
        up a track that's on disk, so it plays without it after a short wait.
        """
        info = {}

        def get_info():
            try:
                stream = track.get_stream()
                info["manifest"] = stream.get_stream_manifest()
                info["stream"] = stream
            except Exception:
                logger.warning(f"Couldn't get the stream info of {track.id}")
                return
            if info.get("late"):
                GLib.idle_add(self._set_late_stream_info, track, info)

        thread = threading.Thread(target=get_info, daemon=True)
        thread.start()
        thread.join(2)
        if "stream" in info:
            self.stream, self.manifest = info["stream"], info["manifest"]
        else:
            info["late"] = True
            logger.warning(f"Playing {track.id} from the music cache without its stream info")
        logger.info(f"Playing from music cache: {track.id}")
        GLib.idle_add(self._play_track_url, track, f"file://{cached_music}", gapless)

    def _set_late_stream_info(self, track, info) -> None:
        if self.playing_track is track and not self.stream:
            self.stream, self.manifest = info["stream"], info["manifest"]
            self.apply_replaygain_tags()

    def _cached_music_path(self, track) -> Path | None:
        """Get the track's file in the music cache, if it's there."""
        if not track or not getattr(utils, "MUSIC_DIR", None):
            return None
        cached_music = utils.MUSIC_DIR / f"{track.id}_{utils.session.audio_quality}.m4a"
        return cached_music if cached_music.exists() else None

    def _get_cached_or_stream_url(self, track, gapless=False):
        """Get URL from cache or stream, caching if not cached."""
        cached_music = self._cached_music_path(track)
        if cached_music:
            logger.info(f"Playing from music cache: {track.id}")
            return f"file://{cached_music}"

        return self._get_stream_url(track, gapless)

    def _get_stream_url(self, track, gapless=False):
        """Get stream URL and queue the track for caching."""
        if not gapless:
            self.apply_replaygain_tags()

        if self.stream.manifest_mime_type == ManifestMimeType.MPD:
            data = self.stream.get_manifest_data()
            if not data:
                raise AttributeError("No MPD manifest available!")

            major, minor, micro, nano = Gst.version()
            if (major, minor) >= (1, 26):
                # Write the manifest to a per-track file for GStreamer, so a
                # prefetched track can't overwrite the one that's playing
                mpd_path = Path(utils.CACHE_DIR, f"manifest_playback_{track.id}.mpd")
                with open(mpd_path, "w") as f:
                    f.write(data)

                self._queue_cache(track)

                return f"file://{mpd_path}"
            else:
                if isinstance(data, str):
                    mpd_bytes = data.encode("utf-8")
                else:
                    mpd_bytes = data
                mpd_b64 = base64.b64encode(mpd_bytes).decode("ascii")
                self._queue_cache(track)
                return "data:application/dash+xml;base64," + mpd_b64

        elif self.stream.manifest_mime_type == ManifestMimeType.BTS:
            urls = self.manifest.get_urls()
            stream_url = urls[0] if isinstance(urls, list) else urls

            self._queue_cache(track)

            return stream_url

        raise AttributeError(f"Unhandled manifest mime type: {self.stream.manifest_mime_type}")

    def _cancel_download(self) -> int:
        """Cancel any running pre-download. Returns the new generation."""
        with self._download_lock:
            self._download_gen += 1
            proc = self._download_process
            self._download_process = None
        if proc:
            proc.kill()
        return self._download_gen

    def _download_track_to_cache(self, track, gen) -> bool:
        """Download a track fully into the music cache before playing it.

        Playback then reads from disk, so it doesn't depend on the CDN
        sustaining the stream bitrate (Fastly throttles below the lossless
        bitrate). Returns True if the track is now in the cache.
        """
        # Stop the previous track so the download gets the full bandwidth, and
        # show this track as loading
        GLib.idle_add(self._show_downloading, track)
        try:
            if self.stream.manifest_mime_type == ManifestMimeType.MPD:
                data = self.stream.get_manifest_data()
                if not data:
                    return False
                mpd_path = Path(utils.CACHE_DIR, f"manifest_playback_{track.id}.mpd")
                with open(mpd_path, "w") as f:
                    f.write(data)
                try:
                    self._download_mpd(track, mpd_path, gen)
                finally:
                    mpd_path.unlink(missing_ok=True)
            elif self.stream.manifest_mime_type == ManifestMimeType.BTS:
                urls = self.manifest.get_urls()
                stream_url = urls[0] if isinstance(urls, list) else urls
                self._download_bts(track, stream_url, gen)
            else:
                return False
        except Exception:
            logger.exception(f"Error downloading track {track.id}")
            return False
        return self._cached_music_path(track) is not None

    def _show_downloading(self, track) -> bool:
        """Stop playback and show the track as loading (download-then-play).

        Returns False so the GLib idle source isn't repeated.
        """
        # Stop without _maybe_start_cache, so a background cache download
        # doesn't compete with the pre-download for bandwidth
        self.playing = False
        self.pipeline.set_state(Gst.State.NULL)
        self.set_track(track)
        self.emit("buffering", 0)
        return False

    def _download_mpd(self, track, mpd_path, gen) -> None:
        """Download an MPD track into the music cache (blocking).

        Cancels if a newer track was requested (gen changed).
        """
        cached_music = utils.MUSIC_DIR / f"{track.id}_{utils.session.audio_quality}.m4a"
        tmp = cached_music.with_suffix(".tmp")
        proc = subprocess.Popen(
            [
                "ffmpeg",
                # Leave the terminal alone when started from one: reading it from
                # the sandbox's background process group stops the whole app
                "-nostdin",
                "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
                "-i", str(mpd_path),
                "-f", "mp4",
                "-c", "copy",
                "-y", str(tmp)
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._download_lock:
            self._download_process = proc
        try:
            while proc.poll() is None:
                if self._download_gen != gen:
                    proc.kill()
                    return
                time.sleep(0.5)
            stderr = proc.stderr.read()
            proc.stderr.close()
            if proc.returncode == 0:
                tmp.rename(cached_music)
                logger.info(f"Downloaded track for playback: {track.id}")
            else:
                logger.warning(
                    f"ffmpeg download failed for {track.id}: "
                    f"{stderr.decode(errors='replace')}"
                )
                tmp.unlink(missing_ok=True)
        finally:
            with self._download_lock:
                self._download_process = None

    def _download_bts(self, track, stream_url, gen) -> None:
        """Download a BTS track into the music cache (blocking).

        Cancels if a newer track was requested (gen changed).
        """
        import requests
        cached_music = utils.MUSIC_DIR / f"{track.id}_{utils.session.audio_quality}.m4a"
        tmp = cached_music.with_suffix(".tmp")
        try:
            response = requests.get(stream_url, stream=True)
            if response.status_code != 200:
                logger.warning(
                    f"Download failed for {track.id}: HTTP {response.status_code}"
                )
                return
            with open(tmp, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if self._download_gen != gen:
                        return
                    f.write(chunk)
            tmp.rename(cached_music)
            logger.info(f"Downloaded track for playback: {track.id}")
        except Exception:
            logger.warning(f"Failed to download track {track.id}")
            tmp.unlink(missing_ok=True)

    def _queue_cache(self, track) -> None:
        """Queue a track for caching.

        The download only starts while playback isn't using the network, so
        it doesn't compete with the stream for bandwidth.
        """
        if not track or not getattr(utils, "MUSIC_DIR", None):
            return
        if self._cached_music_path(track):
            return
        if self.monitor.get_network_metered():
            return
        with self._cache_lock:
            if track.id in self._cache_queued_ids:
                return
            self._cache_queued_ids.add(track.id)
            self._cache_queue.append(track)
        self._maybe_start_cache()

    def _playback_uses_network(self) -> bool:
        """Whether playback is streaming, i.e. using the network.

        A stream is an http(s) url, or a local manifest whose segments are
        remote. A plain file url is the music cache.
        """
        if not self.playing:
            return False
        uri = self.playbin.get_property("current-uri") or ""
        return not uri.startswith("file://") or uri.endswith(".mpd")

    def _maybe_start_cache(self) -> None:
        """Start caching the next queued track, if it's safe to do so."""
        with self._cache_lock:
            if self._cache_thread and self._cache_thread.is_alive():
                return
            if self._playback_uses_network():
                return
            if not self._cache_queue:
                return
            track = self._cache_queue.pop(0)
            self._cache_queued_ids.discard(track.id)
            self._cache_thread = threading.Thread(
                daemon=True, target=self._cache_worker, args=(track,)
            )
            self._cache_thread.start()

    def _cache_worker(self, track) -> None:
        """Download a queued track into the music cache."""
        try:
            if self._cached_music_path(track):
                return
            # Fetch a fresh stream for caching, independent of the one that's
            # playing
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            if stream.manifest_mime_type == ManifestMimeType.MPD:
                data = stream.get_manifest_data()
                if not data:
                    return
                mpd_path = Path(utils.CACHE_DIR, f"manifest_cache_{track.id}.mpd")
                with open(mpd_path, "w") as f:
                    f.write(data)
                try:
                    self._cache_mpd_track(track, mpd_path)
                finally:
                    mpd_path.unlink(missing_ok=True)
            elif stream.manifest_mime_type == ManifestMimeType.BTS:
                urls = manifest.get_urls()
                stream_url = urls[0] if isinstance(urls, list) else urls
                self._cache_bts_track(track, stream_url)
        except Exception:
            logger.exception(f"Error caching track {track.id}")
        finally:
            with self._cache_lock:
                self._cache_thread = None
            # Continue with the next queued track, if it's safe to do so
            GLib.idle_add(self._maybe_start_cache)

    def _cache_mpd_track(self, track, mpd_path):
        """Download and cache MPD track via ffmpeg in background.

        The download is paused while playback is streaming, so it doesn't
        compete with it for bandwidth.
        """
        cached_music = utils.MUSIC_DIR / f"{track.id}_{utils.session.audio_quality}.m4a"
        tmp = cached_music.with_suffix(".tmp")

        try:
            logger.info(f"Caching MPD track: {track.id}")
            proc = subprocess.Popen(
                [
                    "ffmpeg",
                    # Leave the terminal alone when started from one: reading it from
                    # the sandbox's background process group stops the whole app
                    "-nostdin",
                    "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
                    "-i", str(mpd_path),
                    "-f", "mp4",
                    "-c", "copy",
                    "-y", str(tmp)
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._cache_process = proc
            try:
                self._pause_cache_while_streaming(proc)
            finally:
                self._cache_process = None

            stderr = proc.stderr.read()
            proc.stderr.close()

            if proc.returncode == 0:
                tmp.rename(cached_music)
                logger.info(f"Cached MPD track: {track.id}")
            else:
                logger.warning(
                    f"ffmpeg failed for {track.id}: {stderr.decode(errors='replace')}"
                )
                tmp.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to cache MPD track {track.id}: {e}")
            tmp.unlink(missing_ok=True)

    def _pause_cache_while_streaming(self, proc) -> None:
        """Pause the cache download while playback is streaming.

        TIDAL throttles per connection, so a cache download running in
        parallel with a stream can starve it. ffmpeg is stopped and resumed
        at the OS level, so the progress is kept.
        """
        paused = False
        while proc.poll() is None:
            streaming = self._playback_uses_network()
            if streaming and not paused:
                os.kill(proc.pid, signal.SIGSTOP)
                paused = True
                logger.info("Pausing track caching while streaming")
            elif not streaming and paused:
                os.kill(proc.pid, signal.SIGCONT)
                paused = False
                logger.info("Resuming track caching")
            time.sleep(0.5)

    def _cache_bts_track(self, track, stream_url):
        """Download and cache BTS track in background."""
        cached_music = utils.MUSIC_DIR / f"{track.id}_{utils.session.audio_quality}.m4a"
        tmp = cached_music.with_suffix(".tmp")

        try:
            import requests
            logger.info(f"Caching BTS track: {track.id}")
            response = requests.get(stream_url, stream=True)
            if response.status_code == 200:
                with open(tmp, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        # Pause the download while playback is streaming, so it
                        # doesn't compete with it for bandwidth
                        while self._playback_uses_network():
                            time.sleep(1)
                        f.write(chunk)
                tmp.rename(cached_music)
                logger.info(f"Cached BTS track: {track.id}")
            else:
                logger.warning(f"BTS download failed for {track.id}: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to cache BTS track {track.id}: {e}")
            tmp.unlink(missing_ok=True)

    def _clean_stale_manifests(self) -> None:
        """Remove manifest files from previous sessions.

        They're only needed while their track is playing or being cached.
        """
        try:
            for manifest in Path(utils.CACHE_DIR).glob("manifest_*.mpd"):
                if time.time() - manifest.stat().st_mtime > 24 * 3600:
                    manifest.unlink(missing_ok=True)
        except Exception:
            logger.exception("Error cleaning stale manifests")

    def shutdown(self) -> None:
        """Stop any running downloads, if any are running.

        The download threads are daemon, so quitting the app would otherwise
        leave ffmpeg running as an orphan.
        """
        self._cancel_download()
        proc = self._cache_process
        if proc and proc.poll() is None:
            # If it was stopped while streaming, wake it so the terminate
            # below isn't deferred until the process would resume on its own
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def apply_replaygain_tags(self):
        """Apply ReplayGain normalization tags to the current track if enabled."""
        audio_sink = self.playbin.get_property("audio-sink")

        if audio_sink:
            rgtags = audio_sink.get_by_name("rgtags")

        tags = ""

        # https://github.com/EbbLabs/python-tidal/issues/332
        # Rather quiet album than broken eardrums
        if self.stream.track_replay_gain != 1.0:
            tags = (
                f"replaygain-track-gain={self.stream.track_replay_gain},"
                f"replaygain-track-peak={self.stream.track_peak_amplitude}"
            )

        if self.stream.album_replay_gain != 1.0:
            tags = (
                f"replaygain-album-gain={self.stream.album_replay_gain},"
                f"replaygain-album-peak={self.stream.album_peak_amplitude}"
            )

        if rgtags:
            rgtags.set_property("tags", tags)
            logger.info("Applied RG Tags")
        # Save replaygain tags for every song to avoid missing tags when
        # toggling the option
        self.most_recent_rg_tags = f"tags={tags}"

    def _play_track_url(self, track, music_url, gapless=False):
        """Set up and play track from URL."""
        if gapless:
            if track is not self.gapless_request:
                logger.info(
                    f"Dropping gapless track {track.id}: another track was played"
                )
                return
            self.gapless_request = None
            self.loading_gapless = False
            if self.reached_eos:
                # Too late for gapless, the previous track already ended
                gapless = False
        if not gapless:
            # A track played directly replaces the one queued for gapless playback,
            # which would otherwise be shown as playing when this one starts
            self.gapless_request = None
            self.next_track = None
            self.reached_eos = False
            self.use_about_to_finish = False
            self.pipeline.set_state(Gst.State.NULL)
            self.playbin.set_property("volume", self.playbin.get_property("volume"))
        self.playbin.set_property("uri", music_url)

        logger.info(music_url)

        if gapless:
            self.next_track = track
        else:
            self.set_track(track)
            self.stream_loaded_at = time.time()
            # A fresh stream starts downloading from an empty buffer
            self.last_buffer_per = -1
            now = time.monotonic()
            self.buffer_grew_since = now
            self.stream_load_started = now

        if not gapless and self.playing:
            self.play()

        if not gapless:
            self.use_about_to_finish = True

    def play_next_gapless(self, playbin: Any):
        """Enqueue the next track for gapless playback.

        Args:
            playbin: required by Gst
        """
        # playbin is need as arg but we access it later over self
        if self.gapless_enabled and self.use_about_to_finish and self._has_next_track():
            # The current track can reach EOS soon after this signal, and then
            # a uri set later is ignored, so set it now if it's ready
            self._set_prefetched_uri()
            GLib.idle_add(self.play_next, True)
            logger.info("Trying gapless playbck")
        else:
            logger.info("Ignoring about to finish event")

    def _peek_next_track(self) -> Track | None:
        """Get the track play_next(gapless=True) would play, without removing it"""
        if self._repeat_type == RepeatType.SONG:
            return self.playing_track
        if self.queue:
            return self.queue[0]
        if self.shuffle:
            track_list = self._shuffled_tracks_to_play
        else:
            track_list = self._tracks_to_play
        return track_list[0] if track_list else None

    def _prefetch_next_track(self) -> None:
        """Fetch the stream of the next track in the background"""
        if self.next_track or self.handed_off_track or self.loading_gapless:
            return
        if not self.tracks_to_play:
            return
        track = self._peek_next_track()
        if not track or track.id == self.prefetch_id:
            return
        self.prefetch_id = track.id
        threading.Thread(
            daemon=True,
            target=self._prefetch_thread,
            args=(track,),
        ).start()

    def _prefetch_thread(self, track: Track) -> None:
        try:
            stream = track.get_stream()
            manifest = stream.get_stream_manifest()
            self.prefetched = (track.id, stream, manifest, GLib.get_monotonic_time())
            logger.info(f"Prefetched stream: {track.id}")
        except Exception:
            logger.exception("Error prefetching track stream")

    def _set_prefetched_uri(self) -> None:
        """Set the uri of the next track from its prefetched stream, if any.

        Called from a streaming thread in about-to-finish.
        """
        prefetched = self.prefetched
        self.prefetched = None
        self.prefetch_id = None
        track = self._peek_next_track()
        if not prefetched or not track:
            return
        track_id, stream, manifest, fetched_at = prefetched
        # Stream urls expire an hour after they are fetched
        too_old = GLib.get_monotonic_time() - fetched_at > 30 * 60 * 1_000_000
        if track_id != track.id or too_old:
            return
        try:
            self.stream = stream
            self.manifest = manifest
            music_url = self._get_cached_or_stream_url(track, gapless=True)
        except Exception:
            logger.exception("Error using prefetched stream")
            return
        self.playbin.set_property("uri", music_url)
        self.handed_off_track = track
        logger.info(f"Using prefetched stream: {music_url}")

    def _has_next_track(self) -> bool:
        """Whether play_next will play a track after the current one"""
        return bool(
            self.tracks_to_play
            or self.queue
            or (self.playing_track and self._repeat_type != RepeatType.NONE)
        )

    def play_next(self, gapless=False):
        """Play the next track in the queue or playlist.

        Args:
            gapless: Whether to enqueue the track in gapless mode
        """

        # A track is already enqueued from an about-to-finish
        if self.next_track:
            logger.info("Using already enqueued track from gapless")
            track = self.next_track
            self.next_track = None
            self.play_track(track, gapless=gapless)
            return

        if self._repeat_type == RepeatType.SONG and not gapless:
            self.seek(0)
            self.apply_replaygain_tags()
            return
        if self._repeat_type == RepeatType.SONG:
            self.play_track(self.playing_track, gapless=True)
            return

        if self.playing_track:
            self.played_songs.append(self.playing_track)

        if self.queue:
            track = self.queue.pop(0)
            self.play_track(track, gapless=gapless)
            return

        if not self.tracks_to_play and self._repeat_type == RepeatType.LIST:
            self._tracks_to_play = self.played_songs
            self.played_songs = []
            self._update_shuffle_queue()

        if not self.tracks_to_play:
            self.pause()
            return

        track_list = []
        if self.shuffle:
            track_list = self._shuffled_tracks_to_play
        else:
            track_list = self._tracks_to_play

        if track_list and len(track_list) > 0:
            track = track_list.pop(0)
            self.play_track(track, gapless=gapless)

    def skip_blocked(self) -> None:
        """Leave out blocked tracks from what is left to play.

        Removes them from the rest of the playing list and the played songs, and
        skips the playing track if it was just blocked, as the official apps do.
        """
        self._drop_blocked()

        if self.playing_track and utils.is_blocked_by_artist(self.playing_track):
            has_next = (
                self.queue
                or self._tracks_to_play
                or (self._repeat_type == RepeatType.LIST and self.played_songs)
            )
            if self._repeat_type == RepeatType.SONG or not has_next:
                self.pause()
            else:
                self.play_next()
                # play_next adds the blocked track to the played songs
                self._drop_blocked()

        self.can_go_next = len(self._tracks_to_play) > 0
        self.emit("song-added-to-queue")

    def _drop_blocked(self) -> None:
        for track_list in (
            self._tracks_to_play,
            self._shuffled_tracks_to_play,
            self.played_songs,
        ):
            track_list[:] = [
                track
                for track in track_list
                if not utils.is_blocked_by_artist(track)
            ]

    def play_previous(self):
        """Play the previous track or restart current track if near beginning."""
        # if not in the first 2 seconds of the track restart song
        if self.query_position() > 2 * Gst.SECOND:
            self.seek(0)
            self.can_go_prev = len(self.played_songs) > 0
            # only notify when can't go to previous
            if not self.can_go_prev:
                self.notify("can-go-prev")
                GLib.timeout_add(2000, self.previous_timer_callback)
            return

        if not self.played_songs:
            return

        last_index = len(self.played_songs) - 1
        track = self.played_songs.pop(last_index)
        if self.playing_track:
            self._tracks_to_play.insert(0, self.playing_track)
        self.play_track(track)

    def previous_timer_callback(self):
        """Send a notify Event after 2s of the song playing"""
        self.can_go_prev = True
        self.notify("can-go-prev")

    def _update_shuffle_queue(self):
        if self.shuffle:
            self._shuffled_tracks_to_play = self._tracks_to_play.copy()
            random.shuffle(self._shuffled_tracks_to_play)
            self.tracks_to_play = self._shuffled_tracks_to_play
        else:
            self.tracks_to_play = self._tracks_to_play

    def add_to_queue(self, track):
        """Add a track to the end of the play queue.

        Args:
            track: The Track object to add to the queue
        """
        self.queue.append(track)
        self.emit("song-added-to-queue")

    def add_next(self, track):
        """Add a track to the top of the queue.

        Args:
            track: The Track object to play next
        """
        self.queue.insert(0, track)
        self.emit("song-added-to-queue")

    def query_volume(self):
        """Get the current playback volume.

        Returns:
            float: Current volume level (0.0 to 1.0), adjusted for quadratic scaling if enabled
        """
        volume = self.playbin.get_property("volume")
        if self.quadratic_volume:
            return round(volume ** (1 / 2), 1)
        else:
            return round(volume, 1)

    def change_volume(self, value):
        """Set the playback volume.

        Args:
            value (float): Volume level (0.0 to 1.0), will be squared if quadratic volume is enabled
        """
        if self.quadratic_volume:
            self.playbin.set_property("volume", value**2)
        else:
            self.playbin.set_property("volume", value)
        self.emit("volume-changed", value)

    def _update_slider_callback(self):
        """Update playback slider and duration."""
        if not self.duration:
            logger.warning("Duration missing, trying again")
            self.duration = self.query_duration()
        if self.gapless_enabled and self.duration:
            if self.duration - self.query_position() < 30 * Gst.SECOND:
                self._prefetch_next_track()
        self._check_stream_stuck()
        self.emit("update-slider")
        if not self.playing:
            # Returning False removes the timer. While it keeps running, keep its id
            # so the next play() or track start replaces it instead of adding another
            self.update_timer = None
        return self.playing

    def _check_stream_stuck(self) -> None:
        """Recover when the stream of the playing track is stuck.

        Continue from the music cache if the track is cached (e.g. the internet
        went out). Otherwise, if the buffer stopped growing (a dead connection,
        not a slow download), reconnect: re-fetch the stream for a fresh set of
        urls and a fresh connection to the CDN, which may land on a faster edge.
        """
        position = self.query_position(-1)
        now = time.monotonic()
        if not self.playing or position != self.last_position:
            self.last_position = position
            self.position_stuck_since = now
            self.stream_reconnects = 0
            return
        # During a gapless change the next track is loading
        if now - self.position_stuck_since < 3 or self.next_track:
            return
        uri = self.playbin.get_property("current-uri") or ""
        cached_music = self._cached_music_path(self.playing_track)
        if cached_music and uri != f"file://{cached_music}":
            self.position_stuck_since = now
            self.stream_reconnects = 0
            if position <= 0 and self.last_seek_position is not None:
                # Stuck right after a seek, before anything could be downloaded
                position = self.last_seek_position
            duration = self.query_duration()
            if duration and position > 0:
                self.seek_after_sink_reload = position / duration
            logger.warning("Stream stuck, continuing from the music cache")
            self._play_track_url(self.playing_track, f"file://{cached_music}")
            return
        # A plain local file has nothing to reconnect; only a network stream (.mpd)
        # can be re-fetched.
        if uri.startswith("file://") and not uri.endswith(".mpd"):
            return
        # A frozen position also happens while a slow download is still filling
        # the buffer, and a reconnect discards that buffer. Only reconnect when
        # the buffer stopped growing (dead connection), or, during the initial
        # load, when it's taking so long that a fresh edge is worth a try.
        buffer_dead = now - self.buffer_grew_since >= self.BUFFER_DEAD_FOR
        load_too_long = (
            position <= 0
            and self.stream_load_started
            and now - self.stream_load_started >= self.MAX_STREAM_LOAD
        )
        if not buffer_dead and not load_too_long:
            return
        if self.stream_reconnects >= self.MAX_STREAM_RECONNECTS:
            # Gave up; don't retry on every timer tick
            self.position_stuck_since = now
            return
        self.stream_reconnects += 1
        self.position_stuck_since = now
        logger.warning(
            f"Stream stuck, buffer dead, reconnecting "
            f"({self.stream_reconnects}/{self.MAX_STREAM_RECONNECTS})"
        )
        self._reload_track()

    def query_duration(self):
        """Get the duration of the current track.

        Returns:
            int: Duration in nanoseconds, or 0 if query failed
        """
        success, duration = self.playbin.query_duration(Gst.Format.TIME)
        return duration if success else 0

    def query_position(self, default=0) -> int | None:
        """Get the current playback position.

        Args:
            default (int): Default value to return if query fails (default: 0)

        Returns:
            int: Position in nanoseconds, or default value if query failed
        """
        success, position = self.playbin.query_position(Gst.Format.TIME)
        return position if success else default

    def seek(self, seek_fraction, accurate=False):
        """Seek to a position in the current track.

        Args:
            seek_fraction (float): Position as a fraction of total duration (0.0 to 1.0)
            accurate (bool): Seek to the exact position instead of the nearest
                keyframe, which can be seconds earlier in a stream
        """

        # If a seek close to the end is performed then skip
        # Avoids UI desync and stuck tracks
        if not self.seeked_to_end and seek_fraction > 0.98:
            self.use_about_to_finish = False
            self.seeked_to_end = True
            self.play_next()
            return
        position = int(seek_fraction * self.query_duration())
        flags = Gst.SeekFlags.ACCURATE if accurate else Gst.SeekFlags.KEY_UNIT
        self.last_seek_position = position
        self.playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | flags, position
        )

        if self.discord_rpc_enabled:
            discord_rpc.set_activity(self.playing_track, position // 1_000_000_000)

    def set_discord_rpc(self, enabled: bool = True):
        """Enable or disable Discord Rich Presence integration.

        Args:
            enabled (bool): Whether to enable Discord RPC (default: True)
        """
        self.discord_rpc_enabled = enabled
        if enabled and self.playing:
            discord_rpc.set_activity(
                self.playing_track,
                self.query_position() / 1_000_000_000,
            )
        elif enabled:
            discord_rpc.set_activity()
        else:
            discord_rpc.disconnect()

    def get_index(self):
        """Get the index of the currently playing track in the playlist.

        Returns:
            int: Index of current track, or 0 if not found
        """
        for index, track_id in enumerate(self.id_list):
            if track_id == self.playing_track.id:
                return index
        return 0
