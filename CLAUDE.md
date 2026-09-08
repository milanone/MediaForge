# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Cross-platform (Windows/macOS/Linux) GUI for batch video conversion to HEVC or AV1, with
automatic hardware-encoder detection (Intel QSV, AMD AMF, NVIDIA NVENC, Apple VideoToolbox, or
software libx265/libsvtav1) and smart scaling to a max of 1080p. Also handles muxing (track
add/remove/extract) and chapter editing. Evolution of an earlier batch script,
`all2mkva_max1080p_qsv.bat`.

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
- `costruisci_filtri_video()` — builds the ffmpeg filter chain (scale/aspect-ratio/FPS/flip)
- `bitrate_video_esatto()` — exact real video bitrate via summing packet sizes from ffprobe
  (`packet=size`), not an estimate from container-level bitrate minus audio (that approach was
  tried first and found unreliable, especially on `.mkv` files with no declared audio bitrate)
- `build_ffmpeg_cmd()` — assembles the full encode command from `opts` (stream selection, trim,
  scaling, encoder args, timestamp handling)
- `converti_file()` / `worker()` — runs conversion in a background thread, streaming log lines
  back to the UI via a `queue.Queue`
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
