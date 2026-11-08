# Plan: pause + auto-retry when the TIDAL stream stalls

## Goal

When the audio buffer runs dry and the TIDAL server stops sending data, stop
pretending playback is running. Pause the player, then automatically reconnect
with fresh stream URLs and resume at the position where it stalled. If retries
are exhausted, stay paused and tell the user.

## Context

Target branch: `master` (upstream `origin/master`, `49f2472`). Do **not** port
the custom pipeline / cache work from `fix/cache-playback`; master still uses a
single `playbin3`.

What master does today (`src/lib/player_object.py`):

- `playing` only changes in `play()` / `pause()` / end-of-queue (lines 418-444).
  The pipeline's real state is never consulted, so an underrun, a dead HTTP
  connection or a GStreamer error leaves the app claiming "playing".
- The 1 s UI timer `_update_slider_callback` (line 770) keeps re-querying
  `query_position()` and returning `self.playing`, so a frozen position is
  rendered forever while the play button still shows "pause" and MPRIS reports
  `Playing`.
- `_on_buffering_message` (line 277) only forwards the percentage to the
  spinner. Nothing tracks whether the buffer is actually growing.
- `_on_bus_error` (line 256) handles only `"Internal data stream error"` +
  `"not-linked"` and the ALSA-disconnect case. Every other pipeline error
  (HTTP/DASH timeout, connection reset, …) is logged and forgotten.
- No stall watchdog exists. If the server simply stops sending without an
  error or a buffering message, nothing notices.
- `seek_after_sink_reload` already exists and is consumed in `_on_track_start`
  (lines 331-333); it is used by `change_audio_sink()` to resume at a position
  after the pipeline is torn down. Reuse this for stall resumption.
- `_play_track_thread` (line 455) fetches the stream manifest off-thread and
  calls `_play_track_url` via `GLib.idle_add`. `_play_track_url` only starts
  playback when `self.playing` is already true.
- `utils.send_toast(title, timeout)` is available for user feedback
  (used by the existing ALSA error branch).

Design decisions (confirmed with the user):

- Symptom is either "slider frozen but UI says playing" or "audio skips ahead
  on recovery"; both are fixed by pausing the timeline and resuming at the
  stall position.
- On stall: pause, then **auto-retry with fresh stream URLs after a cooldown**.
- Manual play after a stall pause also does **re-fetch + seek**, never a bare
  `set_state(PLAYING)`.
- Detect after **3 s** with no progress.

## Steps

- [ ] 1. Add stall bookkeeping fields + constants to `PlayerObject.__init__`:
      `_current_uri`, `_last_position`, `_progress_at`, `_buffer_percent`,
      `_buffer_changed_at`, `_stall_paused`, `_stall_position`,
      `_stall_track_id`, `_retry_attempts`, `_retry_timer`, `_resume_at`,
      `_retrying`.
      Constants: `STALL_AFTER = 3.0`, `START_STALL_AFTER = 10.0`,
      `BUFFER_DEAD_FOR = 3.0`, `RETRY_DELAYS = (3, 6, 12)`,
      `MAX_STALL_RETRIES = 3`, `HEALTHY_AFTER = 30.0` (seconds; use
      `time.monotonic()`).
- [ ] 2. Track the URI actually handed to playbin in `_play_track_url`
      (`self._current_uri = music_url`). Used to distinguish a network stream
      from a `file://` music-cache file.
- [ ] 3. Extend `_on_buffering_message`: on a percentage increase set
      `_buffer_changed_at = time.monotonic()`; always store
      `_buffer_percent`; keep emitting `"buffering"`.
- [ ] 4. Implement `_check_stall()` and call it from
      `_update_slider_callback` while `self.playing`:
      - `position = query_position(default=-1)`.
      - If the position changed: `_progress_at = now`; if a retry has been
        running for `HEALTHY_AFTER` with progress, reset `_retry_attempts`.
      - If the position is unchanged, it is > 0, `now - _progress_at >=
        STALL_AFTER`, and `now - _buffer_changed_at >= BUFFER_DEAD_FOR`
        (buffer not growing), call `_begin_stall()`.
      - If the position is still 0 (never started), only trip after
        `START_STALL_AFTER`, with the same buffer-growth rule.
      - Skip altogether for `file://` URIs (cache file: pause only, no retry;
        simpler: no detection at all for local files).
- [ ] 5. Implement `_begin_stall()`:
      remember the position (fall back to the last seek target if the query
      returns 0), remember `_stall_track_id`, set `_stall_paused = True`,
      call `pause()`, toast once (`_("Stream stalled, retrying…")`), and
      arm the retry timer.
- [ ] 6. Implement retry scheduling/execution:
      - `_schedule_retry()` uses `RETRY_DELAYS[min(_retry_attempts, len-1)]`
        via a one-shot `GLib.timeout_add`; store the source id in
        `_retry_timer`.
      - `_retry()`: abort if `playing_track` changed, the user started
        playback, or `_stall_paused` was cleared. If
        `_retry_attempts >= MAX_STALL_RETRIES`, stay paused, toast
        `_("Playback paused: no data from TIDAL")`, keep `_stall_paused` so
        a manual play re-fetches. Otherwise increment the counter, set
        `_retrying = True`, set
        `seek_after_sink_reload = _stall_position / duration` (only when
        `duration > 0`), and call `play_track(self.playing_track)` to fetch
        fresh URLs.
- [ ] 7. Resume through the fetch: in `_play_track_url`, when `_retrying` is
      set, start playback even though `playing` is still False (we stayed
      paused during the fetch), clear `_retrying`. `_on_track_start` already
      applies `seek_after_sink_reload`, which resumes at the stall position.
      In `_play_track_thread`'s `except` branch, if `_retrying` was set,
      schedule the next retry instead of dying silently (covers
      `track.get_stream()` failing because the TIDAL API is unreachable).
- [ ] 8. Manual play: in `play()`, if `_stall_paused`, cancel the retry timer,
      reset `_retry_attempts`, clear `_stall_paused`, and go through the same
      re-fetch + seek path instead of `set_state(PLAYING)`. Keep this
      non-recursive (the flag is cleared before `play_track` runs).
- [ ] 9. Handle bus errors: keep the two existing special cases, then route
      any other pipeline error to `_begin_stall()` when `self.playing`.
      Pipeline errors leave GStreamer in an unusable state, so the retry must
      do a full URI re-fetch (which the path above already does).
- [ ] 10. Reset the stall state on real user actions: clear the timer, flags
      and `seek_after_sink_reload` when a different track is requested
      (`play_track` with a track whose id differs from `_stall_track_id`), on
      `seek()`, on end-of-queue `pause()`, and do not run detection while a
      gapless next track is armed (`next_track` set) so the handoff window is
      not mistaken for a stall.
- [ ] 11. UI (`src/window.py`): hide `buffer_spinner` when playback becomes
      paused (extend `update_controls`, which already listens to
      `notify::playing`), so a paused-for-stall player does not spin forever.

## Acceptance

- [ ] With a track playing, blackhole the TIDAL CDN (e.g. `iptables`/DNS
      override): within ~3-5 s the play button flips to "play", the position
      stops, MPRIS `PlaybackStatus` becomes `Paused`, and the spinner is
      hidden.
- [ ] Restore the network before the retry budget is exhausted: playback
      resumes by itself at the same position with a freshly fetched stream
      (GStreamer log shows a new URI, no restart from 0).
- [ ] Keep the network blocked: after 3 attempts (3 s / 6 s / 12 s) the
      player stays paused with the "no data from TIDAL" toast; pressing play
      starts a new retry with fresh URLs.
- [ ] Press next/seek/pause during the cooldown: no stale retry fires, no
      seek is applied to the wrong track, and the new action plays normally.
- [ ] Playback from the local music cache (`file://`) never triggers a
      network retry.
- [ ] `python3 -m compileall src` passes; `ruff check --select E9,F` reports
      no new findings on the changed files (the two remaining warnings, an
      unused `urls` and an unused `tidalapi` import, exist identically on
      unmodified master).

No automated test suite exists in this repo; verification is manual plus the
two static checks above. A reproduction harness would need TIDAL credentials.

## Decisions

- 2026-11-08: Implement on `master`'s `playbin3` player, not on the
  `fix/cache-playback` rework. Master is the upstream branch the fix belongs
  on; the custom pipeline branch will need a separate port.
- 2026-11-08: Pause **and** auto-retry rather than a hard pause only
  (user choice).
- 2026-11-08: On manual play after a stall, always re-fetch stream URLs and
  seek back to the stall position; the old HTTP connection is usually dead
  after a timeout (user choice).
- 2026-11-08: Stall threshold 3 s (user choice). A slow-but-progressing
  download is protected by requiring the buffering percentage to be stale
  for 3 s as well.
- 2026-11-08: Retry budget 3 attempts at 3 s / 6 s / 12 s. Rationale: covers
  a brief uplink blip without flapping forever; after that the user can
  retry manually.
- 2026-11-08: Position 0 (track never started) gets a 10 s grace instead of
  3 s, so a normal initial buffer on a slow link is not treated as a stall.
- 2026-11-08: `file://` playback gets no stall retry; there is no stream to
  refresh.
- 2026-11-08: The UI flips to "Paused" during each cooldown and back to
  "Playing" when the retry starts. This is deliberate: the UI should mirror
  the real state instead of claiming playback continues.

## Assumptions

- GStreamer's position query is frozen while `playbin3` is buffering or while
  the source is waiting for data; a changing position is a reliable "audio is
  moving" signal. (If a sink keeps the position advancing through an underrun,
  the buffering-staleness check still catches the dead stream.)
- `souphttpsrc`/DASH network failures surface either as a BUS ERROR or as a
  silent stall; both paths are covered.
- `track.get_stream()` blocks the worker thread on network access, so a
  failed re-fetch raises in `_play_track_thread` and must re-arm the retry
  there.
- Resuming with `seek_after_sink_reload` after a fresh `play_track()` lands at
  the stored position; this is the same mechanism already used for audio-sink
  reloads.
