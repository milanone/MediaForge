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
  flags; VideoToolbox uses an inverted 1-100 quality scale vs. the GUI's CRF-style 1-51; `hevc_qsv`
  also gets `-preset` from the 3 real QSV levels veryfast/medium/veryslow, `QSV_PRESETS`).
  The "Rate control" row switches between quality (CRF) and a target average bitrate in kbps
  (`opts["bitrate_kbps"]`, `_bitrate_target()`): `encoder_bitrate_args()` emits single-pass VBR
  (`-b:v`, plus `-rc vbr_peak` for AMF and `-rc vbr` for NVENC). The optional "2-pass" box
  (`usa_2_passate()`, `converti_2_passate()`) is software-only (libx265/libsvtav1; QSV/AMF/NVENC
  have no 2nd pass): pass 1 = analysis to `-f null` with the stats file in a temp dir, then
  `converti_file()` runs as pass 2 with `-pass 2` (measured 600 kbps target: libx265 596,
  libsvtav1 601 two-pass vs 671 single-pass; the 2-pass quality gain itself is not measured). Verified on real files
  with `hevc_qsv` (1000/3000 kbps -> 1025/3070 measured) and `libx265`; AMF, NVENC, VideoToolbox,
  SVT-AV1 and `av1_qsv` (not working on this machine even in CRF mode) are untested
- `crf_suggerito()` — default CRF from the selected file's resolution (higher resolution → higher
  CRF, same perceived quality) and a -4 offset for the AV1 family (more efficient than HEVC/H.264
  at the same CRF number). QSV encoders (`QSV_ENCODERS`) use a separate measured curve instead:
  `QSV_CRF_POINTS` (480p=14, 720p=16, 1080p=18, 4K=20, log-interpolated; `av1_qsv` reuses it with +1, unmeasured).
  History: until 2026-11-10 it was 16/18/20/22 (from VMAF runs on 4K films with `hevc_qsv`, ported from MediaForge web);
  lowered by 2 on 2026-11-11 because a 4K film kept at 4K (Dune Part Two) came out at 2700 kbps at CRF 22 and the author
  wants 3500-4000 (about -30% bitrate per +2 CRF). Only the 4K and downscaled-4K points rest on a real result;
  480p/720p/1080p follow the same shift, unmeasured (check them with Predict bitrate).
  Applied on file selection and on encoder change via
  `_aggiorna_quality_da_risoluzione()`, until the user edits the value by hand
  (`_quality_manuale`) — the "↺ Auto" button re-enables it
- Default CRF for a 4K source scaled down (QSV): `crf_suggerito(..., source_side)` returns 14 when the source long side
  is >= `QSV_SOURCE_4K_MIN` (3000) and the output is smaller (`QSV_CRF_DOWNSCALED_4K`); a 4K kept at 4K gets 20
  (was 22, see above). Why: Loki S01E01 (4K HDR, 23 Mbps), `hevc_qsv` to 1080p, three 60 s clips at
  13:44, 20:00 and 32:51. Mean video bitrate over the clips: CRF 14 2594 kbps, 16 1625, 18 1081, 20 776, 22 573 (the
  user's own 4K -> 1080p encodes at CRF 22 came out at 600-700 kbps). Detail kept, measured as high-frequency
  (Laplacian) energy of the luma against the source scaled to 1080p, relative to CRF 8 (12.4 Mbps, which keeps all
  of it: the scaler is not the cause): in the two detailed frames 39%/33% at CRF 14, 23%/18% at 16, 17%/14% at 18,
  14%/10% at 22; in a plain frame 88/84/80/74%. The bitrate of the most detailed clip fell 86% from CRF 14 to 22
  against 71-72% for the others: high CRFs flatten the detailed scenes most. The metric cannot tell grain from real
  detail, and it is 3 frames of one clip of one series: an indication, not a proof. The user chose 14 (was 16) after a
  full Loki encode at CRF 14: about 2100 kbps, detailed and still small; Predict bitrate with 20 clips of 0.2 min gave 2300
  and 2400 (stable, about 10-14% above the real size)
- "Predict bitrate" (`predict_bitrate()`, `encode_sample()`, row under Rate control): encodes N clips (1-10, default
  3) of X minutes (default 1) of the selected file with the CURRENT settings, through `build_ffmpeg_cmd()` (same
  filter chain, encoder, CRF, GPU decode with the same fallback steps), video stream only, in a temp folder; then
  `summarize_bitrates()` gives mean/min/max and the video size the mean predicts (audio and subtitles not included).
  Start times: random (`predict_start_times()`: 5%..95% of the film cut in N bins, one random start per bin) or typed
  (`parse_start_times()`: minutes or mm:ss). The "🎲 New points" button (`_predict_new_points()`) draws a fresh set
  with the same rule and writes it, as h:mm:ss (`format_clock()`), in the Selected-times field, so the points are
  visible and editable before predicting; the label shows the chosen points, then "Clip 2 of 3, at 0:20:00…" (the
  thread sends `predict_status` lines through the log queue), and the final summary lists every point with its
  bitrate. Runs in a thread, the button turns into Cancel, the result is in the label
  under the row and in the log; Start is refused while it runs. Verified on the Loki file through the engine and through
  the real window: the three clips above gave 1870/1757/1247 kbps at CRF 16 (the hand-made encodes gave 1871/1758/1248).
  Random clips differ from run to run: one run with random times gave a mean of 1266 (916-1468) against 1625 for the
  three fixed points, so with 3 clips of 1 minute the estimate can be off by 25% or more — use more clips for a firmer
  number
- Bitrate over time (`BitrateChartWindow`, `read_video_packets()`, `bucket_bitrates()`): video bitrate per 30 s / 1 / 2 / 5 min
  of film time, from the packet sizes (exact: the 30 s buckets of a 60 s clip averaged 1756.5 kbps against 1757 for the whole
  file). Opens by itself at the end of a single-file encode (`("chart", ...)` message from `converti_file()`, only when
  `self._run_files == 1`, so a batch does not open one window per file), and from the "📈 Bitrate chart" button for any file;
  optional comparison with the source and "Save CSV…". The packets are read once (ffprobe on the container, no decoding): a few
  seconds on a local file, but over a network drive the whole file is read, and with the NAS busy it was ~3 MB/s (a 8 GB film did
  not finish in 15 minutes), so it runs in a thread and the comparison with the source is off by default. Not tried yet: a full
  chart on the Loki file (the read did not finish). CSV autosave: the "Bitrate CSV" checkbox (default on, `opts["save_bitrate_csv"]`
  — it must be in the dict built by `_avvia`, not only in the preview one) makes `converti_file()` start `save_bitrate_csv()` in a
  thread, which writes `<output>_bitrate.csv` (1-minute steps, `bitrate_csv_text()`, times as h:mm:ss) next to the file, also in a
  batch. Tried in the real app on a 130 s synthetic clip (libx265 -> mp4): the CSV appeared and the window opened at the end
- Live charts (`LiveStats`, `LiveChartWindow`): while a file encodes, a window shows encoding speed (fps), speed against
  real time and the video bitrate (instantaneous over the last ~20 s, and average), x axis = time elapsed since the file
  started, a dashed line at each new pass or retry. Same method as the web (`record_progress`): ffmpeg's `size=`/`bitrate=`
  cover the whole file, so `audio_output_kbps()` is subtracted (re-encoded tracks count their NEW bitrate, 384 for AC-3, not
  the source's 640; copied tracks their declared bitrate; unknown -> nothing subtracted and the title says "video + audio";
  right for constant-bitrate audio, an estimate for TrueHD/FLAC/DTS-HD). `converti_file()` puts one `LiveStats` in
  `opts["live"]` (it travels through the 2-pass copy), the watchdog feeds it every progress line, a sample goes to the GUI
  every `HISTORY_STEP` = 10 s as `("live", sample)`; `("live_start", name)` opens or clears the window (closed by the
  user = it stays closed until the next Start). Not for Predict bitrate. Tried in the real app on a 100 s 720p clip
  (libx265, audio AC-3 640k 5.1 -> 384k): 8 samples, title "384 kbps of audio subtracted". Not tried: 2-pass, a retry, a batch
- `costruisci_filtri_video()` — builds the ffmpeg filter chain (scale/aspect-ratio/FPS/flip)
- `bitrate_video_esatto()` — exact real video bitrate via summing packet sizes from ffprobe
  (`packet=size`), not an estimate from container-level bitrate minus audio (that approach was
  tried first and found unreliable, especially on `.mkv` files with no declared audio bitrate)
- `build_ffmpeg_cmd()` — assembles the full encode command from `opts` (stream selection, trim,
  scaling, encoder args, timestamp handling). With `usa_hwaccel_decode()` (Windows + QSV encoder)
  frames stay on the GPU. GPU decoding is tried in the order of `HW_DECODE_MODES`: first direct QSV
  (`-hwaccel qsv -hwaccel_output_format qsv`, frames already QSV surfaces, no hwmap), then `d3d11va`
  (`-hwaccel d3d11va -hwaccel_output_format d3d11`, then `hwmap=derive_device=qsv,format=qsv`), then software:
  `converti_file()` steps down one level on each failure (`hw_chain`). The resolution cap as `scale_qsv=w=W:h=H` (`costruisci_filtri_video(scala_gpu=True)`;
  scale_qsv doesn't accept `-2`, so even sizes are computed in Python). Any other filter is software
  and goes after `hwdownload,format=nv12|p010le`. The d3d11va decode needs `-extra_hw_frames 24`: with the default surface pool the QSV encoder (look-ahead, hwmap) starves the decoder, which then DROPS frames while ffmpeg exits with code 0 (a 1080p AV1 film: 1841 of 2700 frames in 90 s, A/V out of sync). As a safety net `esegui_ffmpeg_con_watchdog(fatal_patterns=GPU_DECODE_ERRORS)` stops the attempt at the first decoder-error line so `converti_file()` falls back to software decoding. Direct QSV (the new first choice) does not have the problem. Verified on this machine (Iris Xe): AV1 1080p excerpt of the
  real film 3000/3000 frames and a synthetic HEVC 4K 10-bit HDR10 192/192 with mastering-display and MaxCLL
  metadata identical to the d3d11va and software paths, resolution cap through `scale_qsv`, and both fallback steps
  (forced failures); Also verified on two real 4K HDR10 HEVC films (Dune Part Two, Superman 2025; 90 s excerpts, 2315 and 2366 frames): direct QSV and d3d11va both gave every frame and the same mastering-display/MaxCLL metadata, with and without the 1080p cap (scale_qsv), at the same speed, no fallback needed. Verified on a 100 s excerpt: 3000/3000 frames with the fix; with the old command the attempt is stopped and the software retry gives 3000/3000 GPU decoding is gated on the source
  (`sorgente_decodificabile_hw()`: h264/hevc/av1/vp9/mpeg2/vc1, 4:2:0 8/10 bit); anything else (4:2:2,
  4:4:4, 12 bit, ProRes…) goes straight to software decoding instead of failing first. List measured
  on an N150 in the web version; mjpeg dropped from it (d3d11va has no MJPEG decoder, not tested here).
  `converti_file()` retries without hwaccel
  (plain software chain) if the GPU path fails on a file
- "Stop and keep file" (`_termina_e_tieni()`, `opts["finish_ev"]`): unlike "Interrompi" (terminate,
  partial output deleted) it sends `q` to ffmpeg's stdin (so stdin is a pipe in
  `esegui_ffmpeg_con_watchdog()`, outcome `"terminato"`), which flushes the encoder and writes the
  container trailer: the partial file is kept and plays. Remaining files are skipped; postmuxed
  subtitles are not added to a partial file; stopping during 2-pass pass 1 keeps nothing; if ffmpeg
  doesn't close within 30 s it is terminated and the file discarded. Tested on mkv/mp4 with
  hevc_qsv and libx265 (incl. 2-pass pass 2): files decode with no errors
- Startup with a file argument ("Apri con" from XYplorer): the file is preselected via `after()` in `_preseleziona_file_iniziale()`, after the window is shown. Reading it (ffprobe + exact bitrate) took ~15 s on a film on Google Drive and the window did not appear until then (measured the same on the older version)
- Non-square pixels: `rileva_pixel_non_quadrati()` shows a warning (what SAR means, what to pick) with a
  "Fix: Deforma + Originale" button (`_correggi_sar()`) that sets Aspect ratio = stretch, ratio = Originale
  (and leaves "Copia stream" if "Solo correggi DAR" had selected it). Checked on a 700x480 SAR 8:9 file:
  output 622x480, SAR 1:1; the layout was checked through widget state, not visually (screen capture
  came out black in this session)
- "Check original FPS" (`_verifica_fps_originali()`): finds the real content frame rate, e.g. a
  24 fps film stored as 30 with duplicated frames. `fps_contenuto()` takes 5 clips of 8 s (15/30/
  50/70/85%), scales to 320 px wide and counts the frames left by `mpdecimate` (defaults
  hi=768/lo=320/frac=0.33, `-fps_mode passthrough`), keeps the MAX of the 5 (near-still scenes can
  only drop more frames). If it is within 4% of a `FPS_STANDARD` rate and the declared rate is at
  least 7% higher, it proposes "Limita FPS" at that standard as an exact fraction (29.97 ->
  `30000/1001`; same family, x/1001 or integer, as the declared rate), passed as
  `opts["fps_exact"]` (`self._fps_exact`, cleared if the user edits the number). Otherwise it
  offers to clear the limit. `analizza_fps()` (packet timestamps) only adds a constant/variable
  frame rate line and, with no duplicates but VFR, offers a constant rate. Runs in a thread and
  hands the result to the GUI through a queue (`self.after` from a thread is not safe). Takes
  ~10 s on a 60 s file. Tested on synthetic files only (24->30, 25->50, 29.97->59.94 duplicates,
  true 30, VFR), not on real footage
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

## Parity with the sibling app (desktop <-> web)

`MediaForge` (desktop, `../MediaForge`) and `mediaforge-web` (`../mediaforge-web`, Docker on gaia) are separate
codebases that must keep the same encoding rules. Status as of 2026-10-18; `mediaforge-web/tests/test_parity.py`
enforces the "same" rows.

| Rule | Desktop | Web | Status |
|---|---|---|---|
| Default CRF by resolution (QSV) | `QSV_CRF_POINTS` | `CRF_PUNTI` in `index.html` | same (tested through node) |
| QSV presets | 3 levels, `hevc_qsv` only | 3 levels | same |
| GPU decode gating | `HW_DECODE_*` | `HW_DECODE_*` | same, web also allows `mjpeg` (Linux QSV decoder) |
| FPS check (mpdecimate on 5 clips) | `fps_contenuto()` | `detect_fps()` | same method; desktop adds a timestamp (VFR) check |
| Stats tags cleared, `ENCODING_SETTINGS` | yes | yes | same |
| Target bitrate, stop-and-keep (clean stop, partial file kept) | yes | yes | same (web: UI + `/api/finish`) |
| 2-pass | libx265, libsvtav1 | libx265 only | same idea; QSV has no 2nd pass |
| Per-job GPU decode switch, output bitrate/size after encode | `gpu_decode`, final log line | `hw_accel`, status message | same |
| Aspect ratio: crop / pad / DAR / stretch, "Originale", SAR/DAR rules | yes | yes | same (SAR parsing, DAR first, even-size rounding tested) |
| Non-square pixels warning with one-click fix (Deforma + Originale), warnings in the command preview | yes | yes | same |
| Real bitrate, declared bitrate, fix of a stale bitrate tag | yes | yes | same method; the web computes the real bitrate on every probe, the desktop only when nothing is declared or on request |
| GPU decoder drops frames (error lines in the ffmpeg log) | stops the attempt, software fallback by itself | warning in the page: continue / stop / redo in software | different on purpose (software is very slow on gaia) |
| GPU path | direct QSV first (`-hwaccel qsv`, as the web), then `d3d11va` + `hwmap`, then software | `-hwaccel qsv`, then software | same first choice; the extra d3d11va step exists only on Windows |
| Default CRF for a 4K source scaled down (QSV) | 14 (`QSV_CRF_DOWNSCALED_4K`, via `crf_suggerito(source_side=)`) | 14 (`QSV_CRF_DOWNSCALED_4K` in `index.html`; the page now also feeds the OUTPUT size after the resolution cap, as the desktop does) | same (tested through node) |
| Live charts while encoding (fps, speed, video bitrate instantaneous + average, audio subtracted) | `LiveStats`, `parse_progress_line()`, `audio_output_kbps()`, `LiveChartWindow` (a separate window) | `record_progress()`, `parse_progress_line()`, `audio_output_kbps()`, `drawChart()` (inside the page) | same numbers and rules (tested against each other); the desktop shows them in a window, the web in the page |
| Predict bitrate (N clips of X min, random or typed times, mean/min/max, predicted size) | `predict_bitrate()` | `/api/predict`, `run_predict()` | same method (start-time and summary functions tested against each other); the web runs it as a hidden job and has no software fallback beyond software decoding |
| Encoders | QSV, AMF, NVENC, VideoToolbox, x265, SVT-AV1 | QSV, x265 | different on purpose |
| Mux, Chapters, batch folder / library, upload, background jobs | desktop only / web only | | different on purpose |

When you change a "same" rule on one side, change the other and run both test suites.

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
