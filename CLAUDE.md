# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Cross-platform (Windows/macOS/Linux) video toolkit, not just a batch encoder: encode to HEVC/AV1
with automatic hardware-encoder detection (Intel QSV, AMD AMF, NVIDIA NVENC, Apple VideoToolbox,
or software libx265/libsvtav1), mux tracks (add/remove/extract), and edit chapters — each with
fine per-file control (stream selection, trim, real bitrate, track titles, tag fixes) for files
that differ from one another. The Encode tab *can* run across a whole folder at once, which is
the useful case for a batch of similar-source footage (e.g. clips from the same phone/camcorder
sharing the same settings) — but that's one mode among several, not the app's main purpose.
Encoding grew out of an earlier batch script, `all2mkva_max1080p_qsv.bat`; Mux and Chapters were
added afterward as genuinely per-file tools.

## Running the Application

```bash
pythonw MediaForge.pyw
```

## Dependencies

- `ffmpeg`/`ffprobe` in PATH (hard requirement)
- `tkinter` (standard library)
- `tkinterdnd2` — optional, enables drag-and-drop in the Encode/Mux tabs; the app works without
  it, just without that shortcut
- `mkvpropedit` (mkvtoolnix) — optional, used when available for chapter/tag edits that would
  otherwise require a full remux (see `usa_mkvpropedit_per()`, `trova_mkvpropedit()`)

Available encoders are detected at startup by actually probing ffmpeg (`encoder_compilati()` +
`encoder_funziona()` — compile-time presence in `ffmpeg -encoders` isn't enough, since
hevc_qsv/amf/nvenc show up even without the matching GPU; a real 1-frame encode confirms it
actually works on this machine), so the app suggests the right one per machine (AMF on an AMD PC,
QSV on Intel, VideoToolbox on Apple Silicon…).

## Architecture

Single file (`MediaForge.pyw`), one main class: `App` (a `tkinter`/`ttk` app, `TkinterDnD`
subclass when `tkinterdnd2` is available). Three tabs (`ttk.Notebook`) sharing one log panel below:

### Tab: Codifica (Encode)

Batch-converts a folder of video files to HEVC/AV1. Key pieces:
- `encoder_video_args()` — per-encoder ffmpeg args (QSV/AMF/NVENC each need different rate-control
  flags; VideoToolbox uses an inverted 1-100 quality scale vs. the GUI's CRF-style 1-51)
- `crf_suggerito()` — default CRF from the selected file's resolution (higher resolution → higher
  CRF, same perceived quality) and a -4 offset for the AV1 family (more efficient than HEVC/H.264
  at the same CRF number); applied on file selection and on encoder change via
  `_aggiorna_quality_da_risoluzione()`, until the user edits the value by hand
  (`_quality_manuale`) — the "↺ Auto" button re-enables it
- `costruisci_filtri_video()` — builds the ffmpeg filter chain (scale/aspect-ratio/FPS/flip)
- `bitrate_video_esatto()` — exact real video bitrate via summing packet sizes from ffprobe
  (`packet=size`), not an estimate from container-level bitrate minus audio (that approach was
  tried first and found unreliable, especially on `.mkv` files with no declared audio bitrate)
- `build_ffmpeg_cmd()` — assembles the full encode command from `opts` (stream selection, trim,
  scaling, encoder args, timestamp handling). With `usa_hwaccel_decode()` (Windows + QSV encoder)
  frames stay on the GPU: `-hwaccel d3d11va -hwaccel_output_format d3d11`, then `hwmap=derive_device=qsv,
  format=qsv` and the resolution cap as `scale_qsv=w=W:h=H` (`costruisci_filtri_video(scala_gpu=True)`;
  scale_qsv doesn't accept `-2`, so even sizes are computed in Python). Any other filter is software
  and goes after `hwdownload,format=nv12|p010le`. `converti_file()` retries without hwaccel
  (plain software chain) if the GPU path fails on a file
- `converti_file()` / `worker()` — runs conversion in a background thread, streaming log lines
  back to the UI via a `queue.Queue`
- `esegui_ffmpeg_con_watchdog()` — runs one ffmpeg attempt and detects a STALL (`time=` in the
  progress line frozen for 5s — checked every loop iteration, not only when no output arrives at
  all, since a stalled process can still flood non-progress lines, e.g. repeated `Starting new
  cluster due to timestamp`) distinctly from a normal error exit; typical cause is a subtitle
  track with a corrupted timestamp confusing the muxer's interleaving. `converti_file()` reacts to
  a stall by retrying once without subtitles, then muxes them back in via
  `build_postmux_sottotitoli_cmd()`/`esegui_postmux_sottotitoli()` (pure stream copy, no
  re-encoding) — the same "exclude, mux back in afterward" path also exists as the manual
  `subs=="mux"` option (see `build_ffmpeg_cmd()`, which treats it like `"no"` for the main encode).
  `build_postmux_sottotitoli_cmd()` applies the same `-ss`/`-t` trim as the main encode to the
  source input it pulls subtitles from — otherwise, with a trim active, they'd come back full
  length instead of cut to match the video/audio (bug found on a file trimmed to 16 minutes whose
  postmuxed subtitles still spanned nearly the whole original film)
- A "solo audio" (audio-only) mode exists too — `_build_audio_cmd()`, output extension picked
  from `AUDIO_ONLY_EXT` based on the chosen audio codec (`copy` → `.mka`)
- Stream selection panel, live command preview, single-file metadata/trim panel, real-bitrate
  verification, and two maintenance operations independent of encoding:
  - `fix_hvc1_tag()` — fixes the HEVC tag some players (notably Apple's) need to play HEVC in MP4
  - `fix_bitrate_tag()` — rewrites a stale/wrong declared bitrate tag to match the stream's real
    bitrate (`bitrate_scarto_reale()` decides if the discrepancy is worth fixing, with a 10%
    tolerance for normal VBR variance)

### Tab: Mux

Combine/replace tracks across files without re-encoding: `build_mux_cmd()`, `_mux_carica()`
(loads a source file's tracks), `_mux_aggiungi_tracce()` (adds external audio/subtitle files),
per-track title editing, `_mux_estrai()` / `build_extract_cmd()` (extract a single track back out
to its own file, e.g. pulling out an audio track). Executed via `esegui_mux()`/`mux_worker()`.

### Tab: Capitoli (Chapters)

Chapter list editing, with import/export in OGM chapter format (`formatta_capitoli_ogm()`/
`parsa_capitoli_ogm()`) as well as via loading/writing an ffmetadata file
(`scrivi_ffmetadata_capitoli()`). Two application paths depending on what's available:
`mkvpropedit` (fast, no remux — `usa_mkvpropedit_per()`) or a full ffmpeg remux fallback
(`build_capitoli_cmd()`) when it isn't. Driven by `genera_capitoli()`/`capitoli_worker()`.

### Log panel

Shared by all three tabs (`_poll_log()` drains each tab's `queue.Queue`). Progress lines
(frame/fps/time/speed) overwrite the previous one in place instead of accumulating — tracked via
`self._progresso_range`, set by `_sostituisci_riga_progresso()` and cleared (without deleting the
text) by `_dimentica_riga_progresso()` whenever any other line is written, so a progress line that
gets followed by e.g. a warning becomes permanent history instead of being silently deleted along
with it. Auto-scroll and the live progress rewrite both pause while the user has an active
selection or has scrolled away from the bottom (`_log_in_fondo()`), so the log stays selectable/
copyable (Ctrl+C) during a run instead of being yanked away by the next line.

## Conventions

- Method/function names and comments are in Italian throughout (`leggi`/`aggiorna`/`carica`/
  `costruisci`/`genera` etc.), consistent with the sibling projects in this workspace
  (LabSpectrumManager, KleistekManager).
- Every `subprocess.run()` call passes `stdin=subprocess.DEVNULL` and
  `creationflags=CREATE_NO_WINDOW` (Windows) — required under `pythonw` (no console): without an
  explicit `stdin`, ffmpeg/ffprobe can inherit an invalid stdin handle and probes can fail or hang.

## Editing conventions
- Edits must be surgical and non-destructive
- Never refactor or rename existing methods unless explicitly asked
- Preserve all existing comments and docstrings
- When in doubt, ask before modifying
