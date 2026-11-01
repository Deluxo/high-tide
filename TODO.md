# TODO

## Play the related radio when the playlist runs out

**Status:** planned, not implemented. Prerequisite (`393acdd`, playlist
consumption fix) is done — without it, radio tracks would double-play like
playlist tracks did.

### Goal

When there is nothing left in the queue or the playlist, keep playing instead
of stopping (or looping): continue with TIDAL's *track radio* for the track
that just finished, gaplessly, and keep going.

### API (tidalapi 0.8.8, already in the flatpak)

```python
Track.get_track_radio(limit=100) -> list[Track]
```

Returns related tracks directly (no `Mix` page fetch). It is the same radio as
the existing *"Go to track radio"* page (`src/pages/mix_page.py` uses
`Track.get_radio_mix()` → `Mix.items()`, which is the page-wrapped variant).
Import `Track` from `tidalapi.media`.

### Design (`src/lib/player_object.py`)

1. **Trigger** — in `_update_slider_callback()`, next to the existing
   `if self.duration - self.query_position() < 30 * Gst.SECOND:` block.
   Deliberately *outside* the `if self.gapless_enabled` branch, so radio works
   on the pipewire sink too (where gapless is off). Call something like
   `self._extend_with_radio_if_exhausted()`.
2. **Guard** — only act when all of:
   - `not self.queue and not self._tracks_to_play` (nothing left),
   - `self._repeat_type != RepeatType.SONG`,
   - no radio fetch in flight (`self._radio_fetching`),
   - the seed track has not already been tried (`self._radio_tried_for`).
3. **Worker** (daemon thread, like `_prefetch_thread`):
   `tracks = seed.get_track_radio(limit=100)`, then filter:
   - `utils.is_blocked_by_artist(track)` and `not track.available`,
   - the seed itself (`track.id == seed.id`),
   - ids already in `self._tracks_to_play` / `self.queue`,
   - ids in a bounded recent-radio set (avoid immediate repeats, e.g. last 50),
   then `GLib.idle_add(self._add_radio_tracks, seed, tracks)`.
4. **On the main thread** — `_add_radio_tracks(seed, tracks)`:
   - append to `self._tracks_to_play` *in place*, and to
     `self._shuffled_tracks_to_play` when `self.shuffle` (same aliasing rules
     as `_update_shuffle_queue`),
   - `self.can_go_next = len(self._tracks_to_play) > 0` + `notify`,
   - `self.emit("song-added-to-queue")` so the queue widget shows them,
   - `logger.info(f"Radio: added {len(tracks)} tracks from {seed.id}")`,
   - call `self._prefetch_next_track()` immediately: we are already inside the
     30 s window, so the first radio track gets armed on the concat and the
     transition stays gapless.
5. **Chaining** — when a radio batch is exhausted, the same path fetches a new
   radio from the track that played last (`self.playing_track` at that moment),
   so playback continues indefinitely.
6. **Reset** — in `play_this()` (new listening session): clear
   `self._radio_fetching`, `self._radio_tried_for`, the recent-id set. Keep
   `_radio_fetching` guarded with `try/finally` in the worker so a failed fetch
   cannot wedge it.
7. **Failure** — log and fall back to today's behaviour (stop / repeat-list
   wrap). Bounded by the same 30 s lead the prefetch already relies on, so a
   slow fetch cannot hang anything.

### Open questions (ask before implementing)

1. **Semantics vs repeat-list.** The device has `repeat = 2` (repeat-all).
   - (A) radio only when repeat is off (NONE) — standard, recommended: off →
     radio, repeat-all → loop the list, repeat-one → loop the song.
   - (B) radio wins over repeat-all (the list then never loops).
2. **Chaining** — keep extending after each batch (recommended), or stop after
   the first batch of 100?

### Validation

- Local `python3 -m py_compile src/lib/player_object.py`, plus ruff — it is not
  on `PATH` in the agent shell, use the store binary directly, e.g.
  `$(find /nix/store -maxdepth 3 -name ruff -type f | head -1) check
  src/lib/player_object.py`.
- Headless check inside the flatpak on `flx1sp` (runs the real module against
  stub `Track` objects — no network): copy `src` to
  `/home/furios/htpkg/high_tide`, then
  `flatpak run --command=python3 io.github.nokse22.high-tide <script>`,
  assert: filter drops the seed/blocked/duplicates, append keeps
  `tracks_to_play` alias in sync, radio kicks in only when the list is empty,
  and the next track is armed right after.
- On-device listening: log line `Radio: added N tracks from <id>` once per
  exhaustion, transitions still gapless, no repeats of the same track.

### Notes

- The repeat-list wrap-around is currently *not* gapless
  (`_prefetch_next_track()` returns early when `tracks_to_play` is empty, so
  `play_next()` restarts the list directly). Radio continuation makes the wrap
  mostly moot; only revisit if repeat-all stays in use.
