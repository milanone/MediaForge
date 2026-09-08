#!/usr/bin/env python3
"""
GUI cross-platform (Windows / macOS / Linux) per conversione video HEVC o AV1
con encoder hardware selezionabile — Intel QSV, AMD AMF, NVIDIA NVENC, Apple
VideoToolbox — o software (libx265 / libsvtav1), e scaling intelligente a max 1080p.
Gli encoder disponibili vengono rilevati automaticamente da ffmpeg all'avvio,
così il programma propone quello giusto su ogni macchina (AMF su PC AMD, QSV su
Intel, VideoToolbox su Mac Apple Silicon…).
Richiede ffmpeg/ffprobe nel PATH.
Modulo opzionale tkinterdnd2 (pip install tkinterdnd2): se presente abilita il
drag & drop dei file nei riquadri di Codifica e Mux; se assente il programma
funziona comunque, semplicemente senza quella scorciatoia.
Evoluzione multi-encoder di all2mkva_max1080p_qsv.bat
"""

import ctypes
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, font, messagebox, scrolledtext, ttk

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import ctypes.wintypes

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

ESTENSIONI_INPUT       = ["*.mkv", "*.avi", "*.webm", "*.m4v", "*.mp4", "*.mov"]
ESTENSIONI_INPUT_AUDIO = ["*.dts", "*.mp3", "*.aac", "*.mka", "*.wav", "*.m4a"]
# Estensione output automatica in modalità "solo audio", in base al codec scelto
# (per "copy" si usa .mka: contenitore Matroska audio, accetta qualsiasi codec sorgente)
AUDIO_ONLY_EXT = {"copy": "mka", "aac": "aac", "ac3": "ac3", "mp3": "mp3"}

TIPO_ICONA = {"video": "🎬", "audio": "🔊", "subtitle": "💬", "data": "📦"}


def bitrate_video_esatto(path: Path, durata_sec: float):
    """Bitrate REALE (non stimato) del flusso video: somma i byte di ogni
    singolo pacchetto video (ffprobe -show_entries packet=size, un conteggio
    esatto, non un'approssimazione) e li divide per la durata. Sostituisce un
    precedente approccio per sottrazione dal bitrate totale del contenitore,
    rivelatosi impreciso (verificato: quando anche l'audio non ha un bitrate
    noto, comune nei file mkv, la sottrazione non ha nulla da togliere e
    attribuisce tutto il totale al video, sbagliando anche del doppio).
    Costa un'interrogazione ffprobe aggiuntiva — demuxing, non decodifica:
    pochi secondi anche su un film intero (verificato su un file di 2h30/
    233mila pacchetti) — quindi va chiamata solo alla selezione di un file,
    mai nei ricalcoli frequenti dell'anteprima, e il risultato va tenuto in
    cache (vedi App._bitrate_video_cache). Ritorna bit/s (int) o None se non
    calcolabile (nessuno stream video, durata assente, errore ffprobe)."""
    if not durata_sec or durata_sec <= 0:
        return None
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=size", "-of", "csv=p=0", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        totale = sum(int(riga) for riga in r.stdout.splitlines() if riga.strip().isdigit())
        if totale <= 0:
            return None
        return round(totale * 8 / durata_sec)
    except Exception:
        return None


def video_ha_bitrate_noto(video: dict) -> bool:
    """True se lo stream video ha già un bitrate leggibile SENZA ricalcolarlo
    (campo bit_rate genuino, o tag BPS/BPS-eng): usato da _mostra_stream/
    _mux_carica per decidere se vale la pena chiamare bitrate_video_esatto
    (costa secondi) o se c'è già qualcosa da mostrare all'istante, anche se
    potenzialmente stantio — la correttezza in quel caso è responsabilità
    del pulsante "🔍 Verifica bitrate reale", non del caricamento del file."""
    if not video:
        return False
    if video.get("bit_rate") and str(video.get("bit_rate")).isdigit():
        return True
    tags = video.get("tags", {})
    return bool(tags.get("BPS") or tags.get("BPS-eng"))


def bitrate_scarto_reale(dichiarato, reale, tolleranza: float = 0.10):
    """Confronta bitrate dichiarato e reale con una tolleranza (un encode
    VBR oscilla comunque un po' attorno al target dichiarato) — stessa
    soglia usata da _verifica_bitrate_reale, dal riepilogo di "Correggi tag
    bitrate" e dalla sua anteprima comando, centralizzata qui per non
    doverla tenere sincronizzata in tre punti diversi. Ritorna True se lo
    scarto supera la tolleranza (serve una correzione), False se è già a
    posto, None se non c'è abbastanza dati per giudicare (nessun valore
    dichiarato, o bitrate reale non calcolabile)."""
    if dichiarato is None or not reale:
        return None
    return abs(dichiarato - reale) / reale > tolleranza


def descrivi_stream(i: int, s: dict, includi_titolo: bool = True, bitrate_video: int = None) -> str:
    """Etichetta leggibile per uno stream ffprobe (usata sia dal pannello
    selezione stream in Codifica sia dall'elenco tracce del tab Mux).
    includi_titolo=False omette il "[titolo]" finale — usato nel tab Mux, che
    ha una colonna Titolo separata ed editabile: mostrarlo due volte sarebbe
    ridondante e allargherebbe la finestra senza motivo.
    bitrate_video, se passato, è il valore già calcolato da
    bitrate_video_esatto (bit/s) da usare quando lo stream non riporta un suo
    "bit_rate" diretto — va calcolato UNA volta dal chiamante, mai qui dentro
    (interrogazione ffprobe non banale, vedi bitrate_video_esatto)."""
    tipo   = s.get("codec_type", "?")
    codec  = s.get("codec_name", "?")
    tags   = s.get("tags", {})
    lang   = tags.get("language", "")
    # Il nome traccia sta sotto "title" in mkv, ma alcuni file mp4/mov usano
    # invece (o in aggiunta) l'atomo "name" a livello di singolo stream.
    title  = tags.get("title") or tags.get("name") or ""
    icona  = TIPO_ICONA.get(tipo, "❓")

    if tipo == "video":
        # Priorità: 1) campo "bit_rate" genuino (calcolato dal muxer stesso,
        # sempre affidabile — verificato che per mp4 riflette il file reale
        # anche dopo una ricodifica); 2) tag "BPS"/"BPS-eng" letto SUBITO,
        # senza ricalcolarlo — può essere stantio dopo una ricodifica esterna
        # a questa app (-map_metadata 0 lo copierebbe alla cieca, verificato),
        # ma ricalcolarlo per OGNI file costerebbe secondi inutili nella
        # stragrande maggioranza dei casi in cui è corretto: se c'è un dubbio
        # su un valore specifico, va verificato/corretto col pulsante
        # "🔍 Verifica bitrate reale" (_verifica_bitrate_reale), non qui;
        # 3) bitrate_video, il valore ESATTO passato dal chiamante (calcolato
        # da bitrate_video_esatto) solo quando non c'è proprio nient'altro.
        br = s.get("bit_rate")
        br = int(br) if br and str(br).isdigit() else None
        if br is None:
            tag_br = tags.get("BPS") or tags.get("BPS-eng")
            br = int(tag_br) if tag_br and str(tag_br).isdigit() else None
        if br is None:
            br = bitrate_video
        br_s = f"  {br//1000}kbps" if br else ""
        extra = f"{s.get('width','?')}x{s.get('height','?')}  {s.get('r_frame_rate','?')} fps{br_s}"
    elif tipo == "audio":
        ch_n = int(s.get("channels") or 0)
        ch_layout = s.get("channel_layout", "")
        ch_label = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch_n) \
                   or ch_layout or (f"{ch_n}ch" if ch_n else "?ch")
        sr  = s.get("sample_rate", "?")
        br  = s.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng")
        br_s = f"  {int(br)//1000}kbps" if br and str(br).isdigit() else ""
        extra = f"{ch_label}  {sr}Hz{br_s}"
    elif tipo == "subtitle":
        extra = ""  # la lingua è già mostrata nella sua colonna dedicata, poco sopra
    else:
        extra = ""

    titolo_s = f"  [{title}]" if title and includi_titolo else ""
    return f"{icona} #{i}  {tipo:<8}  {codec:<10}  {lang:<4}  {extra}{titolo_s}"


def sar_numerico(video: dict) -> float:
    """Sample aspect ratio del video come numero (1.0 se assente/non valido/
    esplicitamente 1:1) — per correggere i calcoli di crop/pad/deforma su
    contenuto anamorfico (vedi costruisci_filtri_video/dimensioni_target_pari)."""
    if not video:
        return 1.0
    sar = (video.get("sample_aspect_ratio") or "").strip()
    if not sar or sar in ("0:1", "N/A"):
        return 1.0
    if ":" in sar:
        try:
            w, h = sar.split(":")
            w, h = float(w), float(h)
            return w / h if h else 1.0
        except ValueError:
            return 1.0
    try:
        val = float(sar)
        return val if val > 0 else 1.0
    except ValueError:
        return 1.0


def rileva_pixel_non_quadrati(video: dict) -> str:
    """Se il video ha pixel non quadrati (sample_aspect_ratio ≠ 1:1 — tipico
    di contenuto anamorfico da vecchi DVD/broadcast), ritorna un avviso che
    consiglia "Deforma" (correzione reale, SAR→1:1) invece di "Solo correggi
    DAR" (solo etichetta: alcuni player la ignorano). Stringa vuota altrimenti
    (compreso "0:1"/assente, che ffprobe usa per SAR sconosciuto, non per
    "non quadrato")."""
    if not video:
        return ""
    sar = (video.get("sample_aspect_ratio") or "").strip()
    if not sar or sar in ("1:1", "0:1", "N/A"):
        return ""
    dar = video.get("display_aspect_ratio") or "?"
    return (f"⚠ Pixel non quadrati (SAR {sar}, DAR {dar}): usa \"Deforma\" per una "
            "correzione reale (funziona ovunque). \"Solo correggi DAR\" è più "
            "veloce ma è solo un'etichetta: alcuni player la ignorano.")


# Estensione del contenitore "nudo" per estrarre una traccia nel suo formato
# nativo (stream copy, nessuna ricodifica). Dove il codec non ha un contenitore
# proprio adatto (es. mov_text, PGS/dvd_subtitle), si ripiega su un contenitore
# permissivo che lo accetta comunque via -c copy.
ESTENSIONE_NATIVA = {
    "aac": "aac", "ac3": "ac3", "eac3": "eac3", "mp3": "mp3", "dts": "dts",
    "truehd": "thd", "flac": "flac", "opus": "opus", "vorbis": "ogg",
    "subrip": "srt", "ass": "ass", "ssa": "ssa", "webvtt": "vtt",
}


def estensione_nativa(codec_type: str, codec_name: str) -> str:
    if codec_name in ESTENSIONE_NATIVA:
        return ESTENSIONE_NATIVA[codec_name]
    if codec_type == "audio":
        return "mka"  # contenitore Matroska audio: accetta qualsiasi codec via copy
    return "mkv"      # sottotitoli/altro senza contenitore nudo proprio (es. mov_text, PGS)


def sanitizza_nome_file(testo: str) -> str:
    """Rimuove i caratteri non validi nei nomi file (Windows è il più
    restrittivo: \\/:*?"<>|), per poter usare un tag titolo/nome traccia
    dentro un nome di file estratto."""
    return re.sub(r'[\\/:*?"<>|]', "", testo).strip()


# Codici/nomi lingua più comuni, per riconoscere la lingua di un file esterno
# dal suo nome (es. "film.eng.srt", "film_track10.ita.forced.srt") quando lo
# si aggiunge come traccia nel tab Mux. Normalizzati al codice ISO 639-2 a 3
# lettere, la convenzione usata da ffmpeg/Matroska per il tag "language".
# Niente codici a 2 lettere: sono troppo ambigui contro segmenti qualunque di
# un nome file (es. "no" = negazione inglese oltre che norvegese, "it" = "it"
# come pronome, "el"/"da" = parole comuni) e darebbero falsi positivi.
LINGUE_CODICI = {
    "eng": "eng", "english": "eng", "inglese": "eng",
    "ita": "ita", "italian": "ita", "italiano": "ita",
    "fre": "fre", "fra": "fre", "french": "fre", "francese": "fre",
    "ger": "ger", "deu": "ger", "german": "ger", "tedesco": "ger",
    "spa": "spa", "spanish": "spa", "spagnolo": "spa",
    "por": "por", "portuguese": "por", "portoghese": "por",
    "rus": "rus", "russian": "rus", "russo": "rus",
    "jpn": "jpn", "japanese": "jpn", "giapponese": "jpn",
    "chi": "chi", "zho": "chi", "chinese": "chi", "cinese": "chi",
    "kor": "kor", "korean": "kor", "coreano": "kor",
    "dut": "dut", "nld": "dut", "dutch": "dut", "olandese": "dut",
    "pol": "pol", "polish": "pol", "polacco": "pol",
    "swe": "swe", "swedish": "swe", "svedese": "swe",
    "nor": "nor", "norwegian": "nor", "norvegese": "nor",
    "dan": "dan", "danish": "dan", "danese": "dan",
    "fin": "fin", "finnish": "fin", "finlandese": "fin",
    "gre": "gre", "greek": "gre", "greco": "gre",
    "tur": "tur", "turkish": "tur", "turco": "tur",
    "ara": "ara", "arabic": "ara", "arabo": "ara",
}


def rileva_lingua_da_nome(path: Path) -> str:
    """Cerca un codice/nome lingua noto tra i segmenti del nome file
    (separati da '.', '_', '-' o spazio) per pre-compilare il campo Lingua
    quando si aggiunge una traccia esterna nel tab Mux. Stringa vuota se non
    trova corrispondenze."""
    segmenti = re.split(r"[._\-\s]+", path.stem)
    for seg in segmenti:
        codice = LINGUE_CODICI.get(seg.lower())
        if codice:
            return codice
    return ""


# ---------------------------------------------------------------------------
# Gestione timestamp file (cross-platform)
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    _EPOCH_WIN = datetime(1601, 1, 1, tzinfo=timezone.utc)

    def _datetime_to_filetime(dt: datetime):
        """datetime → FILETIME Windows (intervalli da 100ns dal 1601-01-01)."""
        dt_utc    = dt.astimezone(timezone.utc)
        intervals = int((dt_utc - _EPOCH_WIN).total_seconds() * 10_000_000)
        return ctypes.wintypes.FILETIME(intervals & 0xFFFFFFFF, intervals >> 32)

    def _set_file_times_win(path: Path, dt: datetime) -> bool:
        """Imposta CreationTime, LastAccessTime e LastWriteTime su Windows."""
        ft = _datetime_to_filetime(dt)
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path),
            0x40000000,          # GENERIC_WRITE
            0, None,
            3,                   # OPEN_EXISTING
            0x02000000,          # FILE_FLAG_BACKUP_SEMANTICS
            None
        )
        if handle == ctypes.wintypes.HANDLE(-1).value:
            return False
        try:
            return bool(ctypes.windll.kernel32.SetFileTime(
                handle,
                ctypes.byref(ft),   # CreationTime
                ctypes.byref(ft),   # LastAccessTime
                ctypes.byref(ft),   # LastWriteTime
            ))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def imposta_timestamp_file(path: Path, dt: datetime) -> bool:
    """Imposta le date del file in modo portabile.
    Windows: CreationTime + LastAccess + LastWrite via API Win32.
    macOS:   modifica/accesso via os.utime + data di creazione via SetFile (se presente).
    Linux:   modifica/accesso via os.utime (la data di creazione non è impostabile)."""
    if IS_WINDOWS:
        return _set_file_times_win(path, dt)
    ts = dt.timestamp()
    try:
        os.utime(path, (ts, ts))
    except Exception:
        return False
    if sys.platform == "darwin":
        # Su macOS prova a impostare anche la data di creazione (birthtime) con SetFile,
        # incluso negli Xcode Command Line Tools. Se manca, restano solo mtime/atime.
        try:
            stamp = dt.astimezone().strftime("%m/%d/%Y %H:%M:%S")
            subprocess.run(["SetFile", "-d", stamp, str(path)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    return True


ESTENSIONI_OUTPUT = ["mp4", "mkv", "avi"]

# ---------------------------------------------------------------------------
# Encoder video supportati
# ---------------------------------------------------------------------------
# Ogni encoder usa parametri ffmpeg diversi per qualità e look-ahead, quindi
# non basta cambiare il nome del codec: vedi encoder_video_args(). Il secondo
# elemento della tupla (famiglia di codec) serve a decidere quando forzare il
# tag hvc1 per la compatibilità QuickTime (solo per l'HEVC, vedi build_ffmpeg_cmd).
VIDEO_ENCODERS = {
    "hevc_qsv":          ("HEVC Intel QuickSync (hevc_qsv)", "hevc"),
    "hevc_amf":          ("HEVC AMD AMF (hevc_amf)", "hevc"),
    "hevc_nvenc":        ("HEVC NVIDIA NVENC (hevc_nvenc)", "hevc"),
    "hevc_videotoolbox": ("HEVC Apple VideoToolbox (hevc_videotoolbox)", "hevc"),
    "libx265":           ("HEVC software (libx265)", "hevc"),
    "av1_qsv":           ("AV1 Intel QuickSync (av1_qsv)", "av1"),
    "av1_amf":           ("AV1 AMD AMF (av1_amf)", "av1"),
    "av1_nvenc":         ("AV1 NVIDIA NVENC (av1_nvenc)", "av1"),
    "libsvtav1":         ("AV1 software (libsvtav1, SVT-AV1)", "av1"),
}

VCODEC_COPY_LABEL = "Copia stream (nessuna ricodifica)"


def valore_aspect_ratio(testo: str):
    """Converte una stringa aspect ratio ('16:9', '1.85:1', '2.39', '1,85')
    nel suo valore numerico (float), o None se non interpretabile."""
    testo = testo.strip().replace(",", ".")
    if not testo:
        return None
    if ":" in testo:
        parti = testo.split(":")
        if len(parti) != 2:
            return None
        try:
            w, h = float(parti[0]), float(parti[1])
        except ValueError:
            return None
        if h <= 0 or w <= 0:
            return None
        return w / h
    try:
        val = float(testo)
    except ValueError:
        return None
    return val if val > 0 else None


def rapporto_originale(video: dict):
    """Rapporto di visualizzazione ATTUALE del video (per l'opzione 'Originale'
    nel menu Rapporto): usa il DAR già scritto nel file se presente e valido,
    altrimenti lo calcola da larghezza/altezza/SAR. None se non determinabile
    (nessun file selezionato/probato)."""
    if not video:
        return None
    dar = (video.get("display_aspect_ratio") or "").strip()
    if dar and dar not in ("0:1", "N/A"):
        val = valore_aspect_ratio(dar)
        if val:
            return val
    try:
        iw, ih = float(video.get("width") or 0), float(video.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if iw <= 0 or ih <= 0:
        return None
    return iw * sar_numerico(video) / ih


def risolvi_rapporto_ar(testo: str, video: dict):
    """Risolve il testo del campo Rapporto al suo valore numerico: gestisce il
    caso speciale 'Originale' (rapporto attuale del file, vedi
    rapporto_originale) oltre al normale parsing di valore_aspect_ratio."""
    testo = (testo or "").strip()
    if testo.lower() == "originale":
        return rapporto_originale(video)
    return valore_aspect_ratio(testo)


def dimensioni_target_pari(iw: float, ih: float, rapporto: float, modo: str, sar: float = 1.0):
    """Replica in Python la stessa formula/arrotondamento usati nelle
    espressioni ffmpeg per crop/pad/stretch (vedi costruisci_filtri_video),
    per poter avvisare l'utente quando le dimensioni richieste vengono
    corrette a numeri pari (richiesti dagli encoder H.264/H.265 per il
    subsampling 4:2:0). sar tiene conto di pixel non quadrati (contenuto
    anamorfico): crop/pad ragionano sulla larghezza "a schermo" iw*sar, non
    su iw grezza, per centrare davvero il rapporto voluto. Ritorna
    (w_grezza, h_grezza, w_finale, h_finale)."""
    if modo == "crop":
        w_grezza = min(iw * sar, ih * rapporto) / sar
        h_grezza = min(ih, iw * sar / rapporto)
        arrotonda = lambda v: 2 * math.floor(v / 2)
    elif modo == "pad":
        # pad può solo aggiungere spazio: arrotonda per eccesso, mai per
        # difetto, altrimenti rischia di scendere sotto le dimensioni
        # sorgente e far fallire il filtro.
        w_grezza = max(iw * sar, ih * rapporto) / sar
        h_grezza = max(ih, iw * sar / rapporto)
        arrotonda = lambda v: 2 * math.ceil(v / 2)
    elif modo == "stretch":
        # "Deforma" forza sempre SAR=1 in uscita (vedi costruisci_filtri_video):
        # il SAR sorgente non entra nel calcolo, il target dipende solo da ih.
        w_grezza = ih * rapporto
        h_grezza = ih
        arrotonda = lambda v: 2 * math.floor(v / 2)
    else:
        return None
    return w_grezza, h_grezza, arrotonda(w_grezza), arrotonda(h_grezza)


def encoder_video_args(encoder: str, quality, look_ahead: bool) -> list:
    """Argomenti ffmpeg specifici per l'encoder scelto (qualità 1=max … 51=min)."""
    q = str(quality)
    if encoder == "hevc_qsv":
        args = ["-c:v", "hevc_qsv", "-global_quality", q]
        if look_ahead:
            args += ["-look_ahead", "1"]
    elif encoder == "hevc_amf":
        # AMF: rate control CQP con QP uguali su I/P/B; look-ahead = pre-analysis
        args = ["-c:v", "hevc_amf", "-rc", "cqp",
                "-qp_i", q, "-qp_p", q, "-qp_b", q]
        if look_ahead:
            args += ["-preanalysis", "1"]
    elif encoder == "hevc_nvenc":
        args = ["-c:v", "hevc_nvenc", "-rc", "constqp", "-qp", q]
        if look_ahead:
            args += ["-rc-lookahead", "20"]
    elif encoder == "hevc_videotoolbox":
        # Apple Silicon: qualità costante 1..100 (100=migliore), inversa rispetto
        # alla scala CRF 1..51 della GUI. Nessun look-ahead esposto.
        q_vt = max(1, min(100, round((51 - int(quality)) / 50 * 100)))
        args = ["-c:v", "hevc_videotoolbox", "-q:v", str(q_vt)]
    elif encoder == "libx265":
        # libx265 ha il proprio look-ahead interno: nessun flag dedicato
        args = ["-c:v", "libx265", "-crf", q, "-preset", "medium"]
    elif encoder == "av1_qsv":
        args = ["-c:v", "av1_qsv", "-global_quality", q]
        if look_ahead:
            args += ["-look_ahead", "1"]
    elif encoder == "av1_amf":
        # Come hevc_amf: CQP con QP uguali su I/P/B, pre-analysis come look-ahead
        args = ["-c:v", "av1_amf", "-rc", "cqp",
                "-qp_i", q, "-qp_p", q, "-qp_b", q]
        if look_ahead:
            args += ["-preanalysis", "1"]
    elif encoder == "av1_nvenc":
        args = ["-c:v", "av1_nvenc", "-rc", "constqp", "-qp", q]
        if look_ahead:
            args += ["-rc-lookahead", "20"]
    elif encoder == "libsvtav1":
        # SVT-AV1 ha il proprio look-ahead interno: nessun flag dedicato
        args = ["-c:v", "libsvtav1", "-crf", q, "-preset", "6"]
    else:
        args = ["-c:v", encoder, "-global_quality", q]
    return args


def encoder_compilati() -> list:
    """Encoder noti compilati in ffmpeg (da `ffmpeg -encoders`).
    Attenzione: elenca il supporto a *compile-time*, non se l'hardware è presente
    (hevc_qsv/amf/nvenc compaiono comunque). Per l'uso reale vedi encoder_funziona()."""
    presenti = set()
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            parts = line.split()
            # righe encoder: "<6 flag> nome  Descrizione" (es. " V....D hevc_amf …")
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
                presenti.add(parts[1])
    except Exception:
        pass
    return [e for e in VIDEO_ENCODERS if e in presenti] or list(VIDEO_ENCODERS)


def encoder_funziona(encoder: str):
    """Prova a codificare 1 frame per capire se l'encoder è realmente usabile con
    l'hardware presente (i codec HW risultano 'compilati' anche senza GPU).
    Ritorna (ok: bool, dettaglio: str).
    Nota: stdin=DEVNULL + -nostdin sono necessari sotto pythonw (nessuna console),
    altrimenti ffmpeg eredita uno stdin non valido e il probe può fallire."""
    if encoder in ("libx265", "libsvtav1"):
        return True, "software"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.1",
           "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return True, "ok"
        err = (r.stderr or "").strip()
        # La prima riga è quasi sempre la causa reale (es. "Current codec type
        # is unsupported"); le successive sono conseguenze a cascata che
        # finiscono nel generico "Nothing was written into output file".
        return False, (err.splitlines()[0] if err else f"exit {r.returncode}")
    except FileNotFoundError:
        return False, "ffmpeg non trovato nel PATH"
    except Exception as e:
        return False, f"errore: {e}"


os.system("")  # abilita ANSI su Windows (non usato in tkinter ma utile per debug)


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def ffprobe_json(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format",
           "-print_format", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(r.stdout)
    except Exception:
        return {}


def get_creation_time(data: dict):
    """
    Estrae creation_time dai metadati: prima format_tags, poi stream_tags video.
    Restituisce un oggetto datetime o None.
    """
    fmt_tags = data.get("format", {}).get("tags", {})
    ct = fmt_tags.get("creation_time") or fmt_tags.get("Creation_time")
    if not ct:
        for s in data.get("streams", []):
            ct = s.get("tags", {}).get("creation_time")
            if ct:
                break
    if ct:
        try:
            # Formato ISO 8601 con o senza fuso orario
            ct = ct.replace("Z", "+00:00")
            return datetime.fromisoformat(ct)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Logica di conversione
# ---------------------------------------------------------------------------

def _build_audio_cmd(src: Path, dst: Path, opts: dict, streams: list) -> list:
    """Comando ffmpeg per la modalità 'solo audio': nessun flusso video (-vn),
    codec audio scelto, estensione output dedotta dal codec (vedi AUDIO_ONLY_EXT)."""
    cmd = ["ffmpeg", "-y"]
    if opts.get("ss"):
        cmd += ["-ss", opts["ss"]]
    cmd += ["-i", str(src)]
    if opts.get("t"):
        cmd += ["-t", opts["t"]]

    if opts.get("stream_map"):
        # Selezione manuale (file singolo): tiene solo gli indici che sono audio,
        # anche se in stream_map fossero rimasti indici video/sottotitoli spuntati
        for idx in opts["stream_map"]:
            if idx < len(streams) and streams[idx].get("codec_type") == "audio":
                cmd += ["-map", f"0:{idx}"]
    else:
        cmd += ["-map", "0:a"]

    cmd += ["-map_metadata", "0", "-vn"]

    audio = opts.get("audio", "copy")
    if audio == "copy":
        cmd += ["-c:a", "copy"]
    elif audio == "aac":
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    elif audio == "ac3":
        cmd += ["-c:a", "ac3", "-b:a", "384k"]
    elif audio == "mp3":
        cmd += ["-c:a", "mp3", "-b:a", "128k", "-ar", "44100"]

    cmd.append(str(dst))
    return cmd


def costruisci_filtri_video(opts: dict, video: dict, warnings: list = None) -> list:
    """Costruisce la lista di filtri -vf (scala/aspect ratio/fps/flip) dalle
    opzioni di conversione. Condivisa tra build_ffmpeg_cmd e l'anteprima
    frame (estrai_frame_anteprima), così quel che si vede in anteprima
    coincide sempre con l'output reale della conversione."""
    vf_filters = []
    # iw_eff/ih_eff seguono le dimensioni "effettive" via via che i filtri
    # precedenti (qui, lo scaling) le modificano: servono più sotto per
    # calcolare correttamente l'avviso di arrotondamento su crop/pad/stretch,
    # che altrimenti userebbe le dimensioni originali pre-scala.
    iw_eff = ih_eff = None
    limite = opts.get("limite_res")  # 1080, 720, o None
    if video:
        iw_eff = int(video.get("width", 0)) or None
        ih_eff = int(video.get("height", 0)) or None
    if video and limite and iw_eff and ih_eff:
        portrait = ih_eff > iw_eff
        if portrait and iw_eff > limite:
            # Portrait: 1080p = larghezza 1080 (es. 2160x3840 → 1080x1920)
            vf_filters.append(f"scale={limite}:-2")
            ih_eff = 2 * round(ih_eff * limite / iw_eff / 2)
            iw_eff = limite
        elif not portrait and ih_eff > limite:
            # Landscape: 1080p = altezza 1080 (es. 3840x2160 → 1920x1080)
            vf_filters.append(f"scale=-2:{limite}")
            iw_eff = 2 * round(iw_eff * limite / ih_eff / 2)
            ih_eff = limite

    # --- Aspect ratio ---
    ar_mode = opts.get("ar_mode", "nessuna")
    if ar_mode != "nessuna":
        testo_ratio = opts.get("ar_ratio", "")
        # "Originale" (vedi risolvi_rapporto_ar) usa il rapporto ATTUALE del
        # file invece di un numero fisso: utile ad es. con "Solo correggi DAR"
        # per riscrivere/confermare il tag senza doverlo calcolare a mano.
        valore = risolvi_rapporto_ar(testo_ratio, video)
        if not valore and warnings is not None:
            if testo_ratio.strip().lower() == "originale":
                warnings.append(
                    "Rapporto 'Originale' non disponibile: seleziona un file per usarlo.")
            else:
                warnings.append(
                    f"Aspect ratio '{testo_ratio}' non valido (usa es. 16:9 o 1.85): "
                    "nessuna modifica applicata.")
        rapporto = f"({valore})" if valore else ""
        if not rapporto:
            pass
        elif ar_mode == "crop":
            # Ritaglia dal centro fino al rapporto scelto: espressioni iw/ih
            # valutate da ffmpeg stesso, funziona senza conoscere in anticipo
            # la risoluzione (utile anche in batch su più file diversi).
            # "sar" (variabile nativa ffmpeg) tiene conto di pixel non
            # quadrati: si ragiona sulla larghezza "a schermo" iw*sar, non su
            # iw grezza, altrimenti su contenuto anamorfico il ritaglio
            # centrerebbe il rapporto sbagliato. crop non cambia il sar
            # esistente (corretto: non stiamo alterando la densità dei pixel).
            # 2*trunc(.../2) arrotonda a un numero pari: molti encoder
            # H.264/H.265 rifiutano dimensioni dispari (subsampling 4:2:0) —
            # verificato che senza questo arrotondamento la codifica fallisce.
            vf_filters.append(
                f"crop=w='2*trunc((min(iw*sar,ih*{rapporto})/sar)/2)':"
                f"h='2*trunc(min(ih,iw*sar/{rapporto})/2)'")
        elif ar_mode == "pad":
            # Aggiunge barre nere centrate fino al rapporto scelto, senza
            # perdere nulla dell'immagine originale. Stessa correzione per
            # sar del crop, qui sopra. Arrotonda per ECCESSO (ceil, non
            # trunc): pad può solo aggiungere spazio, mai toglierne — se
            # l'altezza sorgente fosse dispari, arrotondare per difetto
            # potrebbe far scendere il risultato sotto ih e far fallire il
            # filtro ("cannot pad to a size smaller than input").
            vf_filters.append(
                f"pad=w='2*ceil((max(iw*sar,ih*{rapporto})/sar)/2)':"
                f"h='2*ceil(max(ih,iw*sar/{rapporto})/2)':"
                "x='(ow-iw)/2':y='(oh-ih)/2'")
        elif ar_mode == "dar":
            # Corregge solo il rapporto dichiarato (metadata), senza toccare
            # i pixel: utile se un file ha il DAR scritto male.
            vf_filters.append(f"setdar={rapporto}")
        elif ar_mode == "stretch":
            # Come "dar" ma cambia davvero i pixel: altezza invariata (salvo
            # arrotondamento a pari), larghezza forzata al rapporto scelto
            # (distorce l'immagine se il rapporto è molto diverso
            # dall'originale). Il sar sorgente non entra nel calcolo: qui si
            # sovrascrive del tutto la geometria, non la si preserva come in
            # crop/pad. setsar=1 è FONDAMENTALE: senza, ffmpeg ricalcola da
            # solo un sar compensativo per mantenere INVARIATO il DAR
            # originale, vanificando "Deforma" (verificato: scale=1440:1080
            # su un sorgente 16:9 dà sar=4:3, dar ancora 16:9 — nessuna
            # distorsione visibile a un player conforme finché non si forza
            # sar=1 esplicitamente).
            vf_filters.append(f"scale=w='2*trunc((ih*{rapporto})/2)':h='2*trunc(ih/2)'")
            vf_filters.append("setsar=1")

        # Avvisa se le dimensioni richieste non sono numeri pari e sono state
        # corrette (l'espressione ffmpeg lo fa comunque da sola: qui è solo
        # per informare l'utente — serve conoscere già iw/ih, quindi solo
        # quando c'è un file/probe reale dietro, es. nell'anteprima comando a
        # selezione singola). Usa iw_eff/ih_eff (dopo l'eventuale scaling a
        # 1080p/720p), non le dimensioni originali del sorgente, altrimenti
        # l'avviso mostrerebbe numeri diversi da quelli che il crop/pad/
        # stretch vede davvero a valle dello scale. Il nostro scale-to-1080p
        # preserva il rapporto originale (un solo lato esplicito, l'altro
        # -2), quindi non altera il sar: quello letto dal probe resta valido.
        if ar_mode in ("crop", "pad", "stretch") and iw_eff and ih_eff and warnings is not None:
            if valore:
                sar = sar_numerico(video)
                risultato = dimensioni_target_pari(iw_eff, ih_eff, valore, ar_mode, sar)
                if risultato:
                    w_grezza, h_grezza, w_fin, h_fin = risultato
                    if abs(w_fin - w_grezza) > 0.01 or abs(h_fin - h_grezza) > 0.01:
                        warnings.append(
                            f"Il rapporto scelto richiederebbe {w_grezza:.0f}x{h_grezza:.0f}: "
                            f"corretto a {w_fin}x{h_fin} (servono numeri pari, richiesti dall'encoder).")

    # --- FPS ---
    if opts.get("limit_fps") and opts.get("fps_value", 0) > 0:
        vf_filters.append(f"fps={opts['fps_value']}")

    # --- Flip ---
    if opts.get("hflip"):
        vf_filters.append("hflip")
    if opts.get("vflip"):
        vf_filters.append("vflip")

    return vf_filters


def estrai_frame_anteprima(src: Path, ss: str, opts: dict, video: dict, dst_png: Path):
    """Estrae un singolo frame con gli stessi filtri (scala/aspect ratio/
    flip) che verrebbero applicati nella conversione reale, per un'anteprima
    visiva rapida prima di lanciare la codifica vera e propria.
    Ritorna (ok: bool, dettaglio: str)."""
    vf_filters = costruisci_filtri_video(opts, video)

    if opts.get("ar_mode") == "dar":
        # setdar (già in vf_filters) non tocca i pixel: cambia solo il
        # metadato che dice al player come stirare l'immagine. Su un PNG
        # statico letto da Tk non c'è nessuno "stiramento a riproduzione" da
        # mostrare — l'anteprima sarebbe identica in ogni caso. Solo qui,
        # SOLO per l'anteprima (mai nella conversione reale), simuliamo
        # visivamente lo stiramento che un player conforme al DAR
        # applicherebbe, ridimensionando davvero i pixel.
        valore = risolvi_rapporto_ar(opts.get("ar_ratio", ""), video)
        if valore:
            vf_filters.append(f"scale=ih*({valore}):ih")

    # Limite solo per la visualizzazione (non fa parte della conversione
    # reale): evita immagini enormi in finestra su sorgenti 4K/8K.
    vf_filters = vf_filters + ["scale='min(960,iw)':-2"]
    cmd = ["ffmpeg", "-y", "-ss", ss, "-i", str(src), "-frames:v", "1",
           "-vf", ",".join(vf_filters), str(dst_png)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not dst_png.exists():
            err = (r.stderr or "").strip().splitlines()
            return False, (err[-1] if err else f"exit {r.returncode}")
        return True, "ok"
    except Exception as e:
        return False, str(e)


def usa_hwaccel_decode(vcodec: str) -> bool:
    """True se per questo encoder ha senso provare la decodifica hardware
    dell'input (vedi build_ffmpeg_cmd/hwaccel_decode): solo con un encoder
    Intel QSV su Windows. Con un encoder hardware si è visto che ffmpeg
    decodifica comunque via software (CPU al 50%, iGPU al 50% invece che
    quasi ferma) — verificato che aggiungere "-hwaccel qsv" in decodifica
    fallisce direttamente su questo hardware/driver ("Error during QSV
    decoding"), mentre "-hwaccel d3d11va" (il percorso nativo Windows)
    funziona ed è comunque compatibile in uscita con l'encoder qsv. Non
    verificato su AMD/NVENC/macOS/Linux: lì l'encoder resta software-decode
    come prima, nessun cambiamento."""
    return sys.platform == "win32" and vcodec in ("hevc_qsv", "av1_qsv")


def build_ffmpeg_cmd(src: Path, dst: Path, opts: dict, data: dict, warnings: list = None,
                      hwaccel_decode: bool = True) -> list:
    """warnings, se passata, viene riempita con eventuali avvisi da mostrare
    all'utente (es. downgrade forzato di un codec sottotitoli incompatibile).
    hwaccel_decode=False forza la decodifica software anche quando
    usa_hwaccel_decode() sarebbe True: usato da converti_file per riprovare
    senza hwaccel se la decodifica hardware fallisce su un file specifico
    (non tutti i codec/sorgenti sono decodificabili in hardware)."""
    streams  = data.get("streams", [])
    if opts.get("mode") == "audio":
        return _build_audio_cmd(src, dst, opts, streams)
    video    = next((s for s in streams if s.get("codec_type") == "video"), None)

    vf_filters = costruisci_filtri_video(opts, video, warnings)

    # "Solo correggi DAR" + "Copia stream" (nessuna ricodifica): il filtro
    # setdar richiede comunque una pipeline di decodifica/codifica, quindi è
    # incompatibile con "-c:v copy" ("Filtering and streamcopy cannot be used
    # together"). In questo caso il rapporto va scritto come flag "-aspect" a
    # livello di contenitore invece che come filtro: stesso risultato (i
    # player conformi lo rispettano), zero ricodifica — è esattamente quel
    # che "Solo correggi DAR" promette all'utente.
    aspect_flag = None
    if opts.get("ar_mode") == "dar" and opts.get("vcodec") == "copy":
        rimanenti = []
        for f in vf_filters:
            if f.startswith("setdar=") and aspect_flag is None:
                aspect_flag = f[len("setdar="):].strip("()")
            else:
                rimanenti.append(f)
        vf_filters = rimanenti

    cmd = ["ffmpeg", "-y"]
    if hwaccel_decode and usa_hwaccel_decode(opts.get("vcodec")):
        # hwaccel_output_format nv12 forza i frame decodificati in memoria di
        # sistema (non surface GPU "zero-copy"): così i filtri software già
        # esistenti (crop/pad/scale/setdar/hflip/ecc., vedi vf_filters sopra)
        # continuano a funzionare invariati, e l'encoder qsv in uscita accetta
        # comunque i frame nv12 (li ricarica lui stesso sulla GPU).
        cmd += ["-hwaccel", "d3d11va", "-hwaccel_output_format", "nv12"]
    if opts.get("ss"):
        cmd += ["-ss", opts["ss"]]
    cmd += ["-i", str(src)]
    if opts.get("t"):
        cmd += ["-t", opts["t"]]

    # --- Selezione stream ---
    subs = opts.get("subs", "copy")
    if opts.get("stream_map"):
        # Modalità file singolo: stream selezionati manualmente
        for idx in opts["stream_map"]:
            # Se sottotitoli esclusi, salta gli stream subtitle
            if subs == "no" and idx < len(streams) and streams[idx].get("codec_type") == "subtitle":
                continue
            cmd += ["-map", f"0:{idx}"]
    else:
        # Batch: tutti gli stream
        cmd += ["-map", "0"]
        if subs == "no":
            cmd += ["-map", "-0:s"]  # esclude tutti i sottotitoli

    cmd += ["-map_metadata", "0"]

    if vf_filters:
        cmd += ["-vf", ",".join(vf_filters)]

    # --- Video ---
    if opts.get("vcodec") == "copy":
        cmd += ["-c:v", "copy"]
        if aspect_flag:
            cmd += ["-aspect", aspect_flag]
        video_is_hevc = video is not None and video.get("codec_name") == "hevc"
    else:
        cmd += encoder_video_args(opts.get("vcodec", "hevc_qsv"),
                                  opts["quality"], opts["look_ahead"])
        _, famiglia = VIDEO_ENCODERS.get(opts.get("vcodec"), (None, "hevc"))
        video_is_hevc = famiglia == "hevc"
        if video is not None:
            # -map_metadata 0 copia alla cieca il tag statistico "BPS"/
            # "BPS-eng" del sorgente anche quando il video viene RICODIFICATO
            # a un bitrate completamente diverso: verificato con un file
            # reale che il tag resta quello vecchio (es. 2763kbps) anche se
            # il file finale pesa una frazione e il bitrate vero è crollato —
            # ingannando MediaInfo e qualunque altro strumento, questa stessa
            # app inclusa (vedi descrivi_stream). Lo svuotiamo esplicitamente
            # sullo stream video di output (":v:0", quasi sempre l'unico:
            # più tracce video non sono un caso gestito da questa app).
            cmd += ["-metadata:s:v:0", "BPS=", "-metadata:s:v:0", "BPS-eng="]

    if video_is_hevc and opts.get("output_ext") == "mp4":
        # ffmpeg marca l'HEVC in MP4 come "hev1" di default: QuickTime/iOS/macOS
        # lo riconoscono solo con il tag "hvc1", altrimenti il video non si avvia
        # (audio ok, video nero/non supportato).
        cmd += ["-tag:v", "hvc1"]

    # --- Audio ---
    if opts["audio"] == "copy":
        cmd += ["-c:a", "copy"]
    else:
        if opts["audio"] == "aac":
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        elif opts["audio"] == "ac3":
            cmd += ["-c:a", "ac3", "-b:a", "384k"]
        elif opts["audio"] == "mp3":
            cmd += ["-c:a", "mp3", "-b:a", "128k", "-ar", "44100"]
        # Stesso problema del video qui sopra: pulisce il tag BPS/BPS-eng
        # ereditato dal sorgente su ogni stream audio effettivamente
        # ricodificato (non più valido col nuovo bitrate fisso scelto).
        if opts.get("stream_map"):
            n_audio = sum(1 for i in opts["stream_map"]
                          if i < len(streams) and streams[i].get("codec_type") == "audio")
        else:
            n_audio = sum(1 for s in streams if s.get("codec_type") == "audio")
        for i in range(n_audio):
            cmd += [f"-metadata:s:a:{i}", "BPS=", f"-metadata:s:a:{i}", "BPS-eng="]

    # --- Sottotitoli ---
    subs = opts.get("subs", "copy")
    if subs == "no":
        # Rimuove eventuali stream sottotitoli già mappati
        pass
    else:
        if opts.get("stream_map"):
            sub_streams = [streams[i] for i in opts["stream_map"]
                           if i < len(streams) and streams[i].get("codec_type") == "subtitle"]
        else:
            sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

        if sub_streams:
            sub_codec = "srt" if subs == "srt" else "copy"

            if opts.get("output_ext") == "mp4":
                # Il muxer mp4 accetta solo sottotitoli testuali "mov_text": codec
                # come ass/ssa o subrip (srt) fanno fallire l'INTERA conversione
                # (file di 0 byte), non solo il flusso sottotitoli. mov_text è
                # l'unico formato compatibile, quindi lo forziamo quando serve.
                if subs == "srt":
                    incompatibili = {"subrip (srt)"}
                else:
                    incompatibili = {s.get("codec_name") for s in sub_streams} - {"mov_text"}
                if incompatibili:
                    sub_codec = "mov_text"
                    if warnings is not None:
                        nomi = ", ".join(sorted(incompatibili))
                        warnings.append(
                            f"Sottotitoli {nomi} non supportati dal contenitore mp4: "
                            "conversione forzata in mov_text (perde lo styling avanzato "
                            "ASS/SSA, mantiene testo e timing).")

            cmd += ["-c:s", sub_codec]

    cmd.append(str(dst))
    return cmd


def build_fixtag_cmd(src: Path, dst: Path) -> list:
    """Remux puro per correggere il tag del codec HEVC in mp4 già codificati
    (ffmpeg scrive 'hev1', QuickTime/iOS/macOS richiedono 'hvc1'): tutti gli
    stream audio/video/sottotitoli e i metadati vengono copiati inalterati,
    nessuna ricodifica (vedi fix_hvc1_tag per il resto: data file preservata)."""
    return ["ffmpeg", "-y", "-i", str(src), "-map", "0", "-map_metadata", "0",
            "-c", "copy", "-tag:v", "hvc1", str(dst)]


def build_fixbitrate_cmd(src: Path, dst: Path, bitrate_video: int) -> list:
    """Remux puro per correggere il tag 'BPS'/'BPS-eng' del video quando è
    stantio (copiato alla cieca da -map_metadata 0 durante una ricodifica
    precedente a un bitrate diverso, vedi build_ffmpeg_cmd): lo riscrive col
    valore REALE appena calcolato (bitrate_video_esatto), senza ricodificare
    nulla (vedi fix_bitrate_tag per il resto: data file preservata).
    bitrate_video None/0 -> il tag viene solo svuotato (nessun valore
    attendibile da scrivere)."""
    valore = str(bitrate_video) if bitrate_video else ""
    return ["ffmpeg", "-y", "-i", str(src), "-map", "0", "-map_metadata", "0", "-c", "copy",
            "-metadata:s:v:0", f"BPS={valore}", "-metadata:s:v:0", f"BPS-eng={valore}",
            str(dst)]


def build_mux_cmd(src: Path, src_data: dict, stream_map: list,
                   extra_tracks: list, extra_probes: dict,
                   output_ext: str, dst: Path, warnings: list = None,
                   stream_titles: dict = None) -> list:
    """Comando ffmpeg per il tab Mux: remux puro (-c copy, nessuna ricodifica).
    Mantiene dal file sorgente solo gli stream indicati in stream_map, e
    aggiunge in coda una traccia per ciascun elemento di extra_tracks — dict
    con "path" (un file esterno = un input separato mappato per intero,
    tipicamente un .srt o una singola traccia audio), più "lang"/"title"
    (stringhe, vuote se non impostate) e "default"/"forced" (bool) applicati
    come -metadata:s/-disposition:s sulla traccia appena aggiunta. Le tracce
    mantenute dal sorgente restano con lingua/default/forced originali
    (copiati automaticamente da -map/-c copy); il loro titolo può però essere
    corretto/impostato tramite stream_titles ({indice sorgente: nuovo titolo}),
    utile per rinominare le tracce anche senza aggiungerne di esterne. Un
    titolo vuoto in stream_titles non forza nulla: resta quello già presente
    nel file (o il "name" promosso a "title", vedi sotto). extra_probes mappa
    str(path) -> dati ffprobe già calcolati altrove, per non rifare la probe
    ad ogni aggiornamento anteprima.
    Se il contenitore finale è mp4 e tra i sottotitoli risultanti ce n'è uno
    incompatibile (es. ass/ssa/subrip: mp4 supporta solo mov_text, vedi
    build_ffmpeg_cmd), forza mov_text. Stesso discorso per il tag hvc1 sul
    video HEVC (vedi build_fixtag_cmd).
    Esclude inoltre eventuali stream sorgente non audio/video/sottotitoli
    (es. tracce "data" residue di alcuni muxer mp4): nessuno dei contenitori
    proposti (mkv/mp4/avi) li accetta via semplice -map/-c copy, e includerli
    fa fallire l'intera scrittura ("Only audio, video, and subtitles are
    supported for Matroska")."""
    streams = src_data.get("streams", [])
    stream_titles = stream_titles or {}

    TIPI_VALIDI = {"video", "audio", "subtitle"}
    stream_map_validi = [idx for idx in stream_map
                         if idx < len(streams) and streams[idx].get("codec_type") in TIPI_VALIDI]
    esclusi = [idx for idx in stream_map if idx not in stream_map_validi]
    if esclusi and warnings is not None:
        tipi = ", ".join(sorted({streams[idx].get("codec_type", "?") for idx in esclusi}))
        warnings.append(
            f"Stream #{', #'.join(map(str, esclusi))} (tipo: {tipi}) esclusi: nessun "
            "contenitore supporta tracce diverse da audio/video/sottotitoli.")
    stream_map = stream_map_validi

    cmd = ["ffmpeg", "-y", "-i", str(src)]
    for t in extra_tracks:
        cmd += ["-i", str(t["path"])]

    for idx in stream_map:
        cmd += ["-map", f"0:{idx}"]
    for i in range(len(extra_tracks)):
        cmd += ["-map", str(i + 1)]

    cmd += ["-map_metadata", "0", "-c", "copy"]

    # Titolo delle tracce mantenute: priorità al titolo impostato a mano
    # dall'utente (stream_titles); altrimenti, se il file usa il tag "name"
    # invece di "title" (tipico di alcuni mp4 — vedi descrivi_stream), lo
    # promuoviamo esplicitamente, perché il muxer Matroska scrive l'elemento
    # "Name" leggendo solo "title" e altrimenti quel nome andrebbe perso in
    # un remux verso mkv.
    for out_idx, idx in enumerate(stream_map):
        if idx >= len(streams):
            continue
        s_tags = streams[idx].get("tags", {})
        titolo = stream_titles.get(idx, "").strip()
        if not titolo:
            nome = s_tags.get("name")
            if nome and not s_tags.get("title"):
                titolo = nome
        if titolo:
            cmd += [f"-metadata:s:{out_idx}", f"title={titolo}"]

    for i, t in enumerate(extra_tracks):
        out_idx = len(stream_map) + i
        if t.get("lang"):
            cmd += [f"-metadata:s:{out_idx}", f"language={t['lang']}"]
        if t.get("title"):
            cmd += [f"-metadata:s:{out_idx}", f"title={t['title']}"]
        flags = [nome for nome, on in (("default", t.get("default")), ("forced", t.get("forced"))) if on]
        if flags:
            # A differenza di -metadata:s:N (indice assoluto), -disposition:s:N
            # userebbe un indice relativo al TIPO di stream — con -disposition
            # va quindi usato l'indice assoluto senza prefisso "s:" (verificato
            # empiricamente: -disposition:s:2 su un file con un solo sottotitolo
            # non applica nulla, -disposition:2 sì).
            cmd += [f"-disposition:{out_idx}", "+".join(flags)]

    # Avviso (non bloccante): più di una traccia sottotitoli forzata nella
    # STESSA lingua è ambigua — i player in genere ne usano solo una. Tracce
    # forzate in lingue diverse restano normali (una a testa è la convenzione
    # corretta). Lingue non specificate non vengono confrontate tra loro.
    forzate_per_lingua = {}
    for idx in stream_map:
        if idx >= len(streams):
            continue
        s = streams[idx]
        if s.get("codec_type") != "subtitle" or not s.get("disposition", {}).get("forced"):
            continue
        lang = s.get("tags", {}).get("language")
        if lang:
            forzate_per_lingua.setdefault(lang, []).append(f"#{idx} (mantenuta)")
    for t in extra_tracks:
        if not t.get("forced"):
            continue
        edata = extra_probes.get(str(t["path"]), {})
        if not any(s.get("codec_type") == "subtitle" for s in edata.get("streams", [])):
            continue
        lang = t.get("lang")
        if lang:
            forzate_per_lingua.setdefault(lang, []).append(f"{t['path'].name} (aggiunta)")
    if warnings is not None:
        for lang, elenco in forzate_per_lingua.items():
            if len(elenco) > 1:
                warnings.append(
                    f"Più tracce sottotitoli forzate per la lingua '{lang}': "
                    f"{', '.join(elenco)} — probabilmente solo la prima verrà "
                    "riconosciuta dai player, le altre risulteranno ridondanti.")

    if output_ext == "mp4":
        video = next((streams[i] for i in stream_map
                      if i < len(streams) and streams[i].get("codec_type") == "video"), None)
        if video and video.get("codec_name") == "hevc":
            cmd += ["-tag:v", "hvc1"]

        incompatibili = set()
        for idx in stream_map:
            if idx < len(streams) and streams[idx].get("codec_type") == "subtitle":
                cn = streams[idx].get("codec_name")
                if cn and cn != "mov_text":
                    incompatibili.add(cn)
        for t in extra_tracks:
            edata = extra_probes.get(str(t["path"]), {})
            for s in edata.get("streams", []):
                if s.get("codec_type") == "subtitle":
                    cn = s.get("codec_name")
                    if cn and cn != "mov_text":
                        incompatibili.add(cn)
        if incompatibili:
            cmd += ["-c:s", "mov_text"]
            if warnings is not None:
                nomi = ", ".join(sorted(incompatibili))
                warnings.append(
                    f"Sottotitoli {nomi} non supportati dal contenitore mp4: "
                    "conversione forzata in mov_text (perde lo styling avanzato).")

    cmd.append(str(dst))
    return cmd


def build_extract_cmd(src: Path, indice: int, dst: Path) -> list:
    """Estrae una singola traccia nel suo formato nativo: stream copy puro,
    nessuna ricodifica (l'estensione di dst va scelta con estensione_nativa())."""
    return ["ffmpeg", "-y", "-i", str(src), "-map", f"0:{indice}", "-c", "copy", str(dst)]


def applica_timestamp(src: Path, dst: Path, modalita: str, data: dict, log_q: queue.Queue):
    if modalita == "nessuno":
        return

    dt = None
    if modalita == "metadati":
        dt = get_creation_time(data)
        if dt:
            log_q.put(("detail", f"  Timestamp da metadati: {dt.strftime('%Y-%m-%d %H:%M:%S')}"))
        else:
            log_q.put(("detail", "  creation_time non trovato nei metadati, uso date file"))

    if dt is None:
        # Fallback o modalità "file": legge la data di creazione reale del sorgente.
        # Windows: st_ctime = CreationTime. macOS: st_birthtime. Linux: nessuna
        # birthtime → si ripiega su st_ctime (change time).
        src_stat = src.stat()
        ct = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
        mt = src_stat.st_mtime
        # Usa la data più vecchia tra creazione e modifica come riferimento
        ts = min(ct, mt)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        log_q.put(("detail", f"  Timestamp copiato dal file sorgente: {dt.strftime('%Y-%m-%d %H:%M:%S')}"))

    ok = imposta_timestamp_file(dst, dt)
    if not ok:
        log_q.put(("detail", "  Avviso: impossibile impostare la data del file"))


def converti_file(src: Path, opts: dict, log_q: queue.Queue, stop_ev: threading.Event) -> bool:
    ext = opts["output_ext"]
    dst = src.parent / f"{src.stem}_enc.{ext}"

    log_q.put(("info", f"\n▶ {src.name}"))

    data = ffprobe_json(src)

    # Se l'encoder scelto supporta la decodifica hardware (usa_hwaccel_decode),
    # il primo tentativo la usa; se fallisce si riprova UNA volta in
    # decodifica software invece di considerare il file irrecuperabile — non
    # tutti i codec/sorgenti sono decodificabili in hardware (verificato che
    # perfino "-hwaccel qsv" fallisce in decodifica su alcuni file/driver, pur
    # con l'encoder qsv scelto perfettamente funzionante).
    tentativi_hwaccel = [True, False] if usa_hwaccel_decode(opts.get("vcodec")) else [False]

    for tentativo, hwaccel in enumerate(tentativi_hwaccel):
        try:
            warnings = []
            cmd = build_ffmpeg_cmd(src, dst, opts, data, warnings, hwaccel_decode=hwaccel)
            for w in warnings:
                log_q.put(("warning", f"  ⚠ {w}"))
            log_q.put(("cmd", "  $ " + " ".join(cmd)))

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            interrotto = False
            for line in proc.stdout:
                if stop_ev.is_set():
                    # Termina subito il processo ffmpeg in corso, non solo la coda
                    # dei file successivi: prima "Interrompi" agiva solo tra un
                    # file e l'altro, lasciando proseguire indisturbata la codifica
                    # già avviata (il caso più comune con un solo film selezionato).
                    interrotto = True
                    proc.terminate()
                    break
                line = line.rstrip()
                if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                    log_q.put(("progress", line))
                elif line:
                    log_q.put(("detail", "  " + line))
            proc.wait()

            if interrotto:
                log_q.put(("detail", "  Interrotto dall'utente."))
                dst.unlink(missing_ok=True)  # rimuove l'output parziale/incompleto
                return False

            if proc.returncode != 0:
                ultimo_tentativo = tentativo == len(tentativi_hwaccel) - 1
                if not ultimo_tentativo:
                    log_q.put(("warning",
                        "  ⚠ Decodifica hardware non riuscita per questo file: ripiego "
                        "su decodifica software e riprovo."))
                    dst.unlink(missing_ok=True)
                    continue
                log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
                # Rimuove l'eventuale file di destinazione (parziale, o un
                # residuo di un tentativo precedente): altrimenti, dopo un
                # fallimento, quel file resterebbe lì scambiabile per un
                # risultato riuscito — l'esistenza di dst deve sempre voler
                # dire "conversione riuscita", mai "c'era già qualcosa".
                dst.unlink(missing_ok=True)
                return False

            applica_timestamp(src, dst, opts["timestamp"], data, log_q)

            size_src = src.stat().st_size / 1_048_576
            size_dst = dst.stat().st_size / 1_048_576
            risparmio = (1 - size_dst / size_src) * 100 if size_src else 0
            log_q.put(("ok", f"  ✓ {dst.name}  [{size_src:.1f} MB → {size_dst:.1f} MB  {risparmio:+.0f}%]"))
            return True

        except Exception as e:
            log_q.put(("error", f"  ✗ Eccezione: {e}"))
            dst.unlink(missing_ok=True)
            return False

    return False  # non dovrebbe mai arrivarci: il loop ritorna sempre prima


def worker(files: list, opts: dict, log_q: queue.Queue, stop_ev: threading.Event):
    ok = err = 0
    for f in files:
        if stop_ev.is_set():
            log_q.put(("detail", "\nConversione interrotta dall'utente."))
            break
        if converti_file(f, opts, log_q, stop_ev):
            ok += 1
        else:
            err += 1

    log_q.put(("summary", f"\n{'='*60}\nCompletato: {ok} OK, {err} errori\n{'='*60}"))
    log_q.put(("done", None))


def fix_hvc1_tag(src: Path, log_q: queue.Queue, stop_ev: threading.Event) -> bool:
    """Corregge il tag del codec HEVC in un mp4 già codificato (hev1 → hvc1) per
    la compatibilità QuickTime/iOS/macOS. Remux puro: -c copy su tutti gli stream,
    -map_metadata 0 preserva tutti i tag (inclusa la data di codifica interna).
    Il file viene sostituito sullo stesso path, poi la data del file (creazione
    e modifica) viene ripristinata identica all'originale."""
    log_q.put(("info", f"\n▶ {src.name}"))

    src_stat = src.stat()
    ct = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
    orig_dt = datetime.fromtimestamp(min(ct, src_stat.st_mtime), tz=timezone.utc)

    tmp = src.with_name(f".{src.stem}.hvc1fix{src.suffix}")
    try:
        cmd = build_fixtag_cmd(src, tmp)
        log_q.put(("cmd", "  $ " + " ".join(cmd)))

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        interrotto = False
        for line in proc.stdout:
            if stop_ev.is_set():
                interrotto = True
                proc.terminate()
                break
            line = line.rstrip()
            if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                log_q.put(("progress", line))
            elif line:
                log_q.put(("detail", "  " + line))
        proc.wait()

        if interrotto:
            log_q.put(("detail", "  Interrotto dall'utente."))
            tmp.unlink(missing_ok=True)
            return False

        if proc.returncode != 0 or not tmp.exists():
            log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
            tmp.unlink(missing_ok=True)
            return False

        tmp.replace(src)
        if not imposta_timestamp_file(src, orig_dt):
            log_q.put(("detail", "  Avviso: impossibile ripristinare la data del file"))

        log_q.put(("ok", f"  ✓ {src.name}  [tag hvc1 applicato]"))
        return True

    except Exception as e:
        log_q.put(("error", f"  ✗ Eccezione: {e}"))
        tmp.unlink(missing_ok=True)
        return False


def fixtag_worker(files: list, log_q: queue.Queue, stop_ev: threading.Event):
    ok = err = 0
    for f in files:
        if stop_ev.is_set():
            log_q.put(("detail", "\nOperazione interrotta dall'utente."))
            break
        if fix_hvc1_tag(f, log_q, stop_ev):
            ok += 1
        else:
            err += 1

    log_q.put(("summary", f"\n{'='*60}\nCompletato: {ok} OK, {err} errori\n{'='*60}"))
    log_q.put(("done", None))


def fix_bitrate_tag(src: Path, log_q: queue.Queue, stop_ev: threading.Event,
                     bitrate_precalcolato: int = None) -> bool:
    """Corregge il tag 'BPS' (bitrate video) quando è stantio (es. un file
    ricodificato PRIMA che build_ffmpeg_cmd lo ripulisse da sé, vedi il tag
    ereditato ciecamente da -map_metadata 0), senza ricodificare nulla.
    bitrate_precalcolato, se passato, salta il ricalcolo (già fatto altrove,
    es. dal riepilogo mostrato all'utente PRIMA di chiedere conferma — vedi
    App._avvia/App._verifica_bitrate_reale): evita di rifare la stessa
    scansione dei pacchetti due volte per lo stesso file."""
    log_q.put(("info", f"\n▶ {src.name}"))

    mkvpropedit = trova_mkvpropedit() if src.suffix.lower() in (".mkv", ".mka") else None
    if mkvpropedit:
        # mkvpropedit ricalcola DA SOLO le statistiche di TUTTE le tracce
        # (audio comprese, non solo il video) e modifica il file sul posto,
        # senza riscriverlo: niente file temporaneo, niente copia dei dati
        # audio/video. Non serve calcolare nulla con bitrate_video_esatto
        # PRIMA di questo passo: sarebbe un secondo conto ridondante, dato
        # che mkvpropedit rilegge comunque i pacchetti per conto proprio
        # (verificato che i due calcoli indipendenti danno lo stesso
        # risultato, a differenza di quando serve DAVVERO il valore, cioè
        # nel ramo ffmpeg qui sotto, dove va scritto esplicitamente nel tag).
        cmd = [mkvpropedit, str(src), "--add-track-statistics-tags"]
        log_q.put(("cmd", "  $ " + " ".join(cmd)))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for riga in (r.stdout or "").splitlines():
                if riga.strip():
                    log_q.put(("detail", "  " + riga.strip()))
            if r.returncode != 0:
                log_q.put(("error", f"  ✗ mkvpropedit ha restituito il codice {r.returncode}"))
                return False
            log_q.put(("ok", f"  ✓ {src.name}  [statistiche traccia ricalcolate sul posto]"))
            return True
        except Exception as e:
            log_q.put(("error", f"  ✗ Eccezione: {e}"))
            return False

    # --- fallback ffmpeg: qui il bitrate calcolato va scritto esplicitamente
    # nel tag (-metadata:s:v:0 BPS=...), quindi serve davvero e va loggato ---
    src_stat = src.stat()
    ct = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
    orig_dt = datetime.fromtimestamp(min(ct, src_stat.st_mtime), tz=timezone.utc)

    data = ffprobe_json(src)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        log_q.put(("error", "  ✗ Nessuno stream video nel file: niente da correggere."))
        return False

    vecchio = video.get("bit_rate") or video.get("tags", {}).get("BPS") or "assente"
    if bitrate_precalcolato is not None:
        bitrate = bitrate_precalcolato
    else:
        try:
            durata = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            durata = 0.0
        log_q.put(("detail", "  Calcolo il bitrate reale (somma dei pacchetti)…"))
        bitrate = bitrate_video_esatto(src, durata)
    if not bitrate:
        log_q.put(("error", "  ✗ Impossibile calcolare il bitrate reale per questo file."))
        return False
    log_q.put(("detail",
        f"  Tag precedente: {vecchio}  →  reale: {bitrate} bps ({bitrate // 1000}kbps)"))

    tmp = src.with_name(f".{src.stem}.bitratefix{src.suffix}")
    try:
        cmd = build_fixbitrate_cmd(src, tmp, bitrate)
        log_q.put(("cmd", "  $ " + " ".join(cmd)))

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        interrotto = False
        for line in proc.stdout:
            if stop_ev.is_set():
                interrotto = True
                proc.terminate()
                break
            line = line.rstrip()
            if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                log_q.put(("progress", line))
            elif line:
                log_q.put(("detail", "  " + line))
        proc.wait()

        if interrotto:
            log_q.put(("detail", "  Interrotto dall'utente."))
            tmp.unlink(missing_ok=True)
            return False

        if proc.returncode != 0 or not tmp.exists():
            log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
            tmp.unlink(missing_ok=True)
            return False

        tmp.replace(src)
        if not imposta_timestamp_file(src, orig_dt):
            log_q.put(("detail", "  Avviso: impossibile ripristinare la data del file"))

        log_q.put(("ok", f"  ✓ {src.name}  [tag bitrate corretto a {bitrate // 1000}kbps]"))
        return True

    except Exception as e:
        log_q.put(("error", f"  ✗ Eccezione: {e}"))
        tmp.unlink(missing_ok=True)
        return False


def fixbitrate_worker(items: list, log_q: queue.Queue, stop_ev: threading.Event):
    """items: lista di (Path, bitrate_reale) — il bitrate è già stato
    calcolato e mostrato all'utente nel riepilogo PRIMA di chiedere conferma
    (vedi App._avvia), qui non va ricalcolato."""
    ok = err = 0
    for f, bitrate in items:
        if stop_ev.is_set():
            log_q.put(("detail", "\nOperazione interrotta dall'utente."))
            break
        if fix_bitrate_tag(f, log_q, stop_ev, bitrate_precalcolato=bitrate):
            ok += 1
        else:
            err += 1

    log_q.put(("summary", f"\n{'='*60}\nCompletato: {ok} OK, {err} errori\n{'='*60}"))
    log_q.put(("done", None))


def esegui_mux(src: Path, cmd: list, dst: Path, log_q: queue.Queue, stop_ev: threading.Event) -> bool:
    """Esegue il comando generato da build_mux_cmd, con supporto
    all'interruzione (stessa logica di converti_file/fix_hvc1_tag) e data del
    file di output impostata come quella del file sorgente."""
    log_q.put(("info", f"\n▶ {dst.name}"))
    log_q.put(("cmd", "  $ " + " ".join(cmd)))

    src_stat = src.stat()
    ct = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
    orig_dt = datetime.fromtimestamp(min(ct, src_stat.st_mtime), tz=timezone.utc)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        interrotto = False
        for line in proc.stdout:
            if stop_ev.is_set():
                interrotto = True
                proc.terminate()
                break
            line = line.rstrip()
            if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                log_q.put(("progress", line))
            elif line:
                log_q.put(("detail", "  " + line))
        proc.wait()

        if interrotto:
            log_q.put(("detail", "  Interrotto dall'utente."))
            dst.unlink(missing_ok=True)
            return False

        if proc.returncode != 0:
            log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
            return False

        if not imposta_timestamp_file(dst, orig_dt):
            log_q.put(("detail", "  Avviso: impossibile impostare la data del file"))

        log_q.put(("ok", f"  ✓ {dst.name}"))
        return True

    except Exception as e:
        log_q.put(("error", f"  ✗ Eccezione: {e}"))
        return False


def mux_worker(src: Path, cmd: list, dst: Path, log_q: queue.Queue, stop_ev: threading.Event):
    ok = esegui_mux(src, cmd, dst, log_q, stop_ev)
    log_q.put(("summary", f"\n{'='*60}\n{'Completato' if ok else 'Fallito'}\n{'='*60}"))
    log_q.put(("done", None))


# ---------------------------------------------------------------------------
# MKVToolNix (opzionale): mkvpropedit modifica capitoli/tag di un mkv SUL
# POSTO, senza riscrivere l'intero file (nessuna copia dei dati audio/video)
# — verificato molto più veloce di un remux ffmpeg equivalente (una frazione
# di secondo contro secondi, indipendentemente dalla dimensione del file,
# perché tocca solo il segmento metadati). Usato solo per file .mkv/.mka;
# ffmpeg resta comunque necessario per mp4/avi e per qualunque operazione che
# cambi davvero i dati (aggiungere/togliere tracce, ricodificare).
# ---------------------------------------------------------------------------

def trova_mkvpropedit():
    """Cerca l'eseguibile mkvpropedit: prima nel PATH, poi nelle cartelle di
    installazione tipiche (il pacchetto MKVToolNix di Windows non lo
    aggiunge automaticamente al PATH, verificato). None se non trovato: chi
    lo chiama ripiega allora sul remux ffmpeg, sempre disponibile ma più
    lento e con una riscrittura completa del file."""
    nome = "mkvpropedit.exe" if sys.platform == "win32" else "mkvpropedit"
    trovato = shutil.which(nome)
    if trovato:
        return trovato
    if sys.platform == "win32":
        candidati = [r"C:\Program Files\MKVToolNix\mkvpropedit.exe",
                     r"C:\Program Files (x86)\MKVToolNix\mkvpropedit.exe"]
    elif sys.platform == "darwin":
        candidati = ["/Applications/MKVToolNix-GUI.app/Contents/MacOS/mkvpropedit",
                     "/opt/homebrew/bin/mkvpropedit", "/usr/local/bin/mkvpropedit"]
    else:
        candidati = ["/usr/bin/mkvpropedit", "/usr/local/bin/mkvpropedit"]
    for c in candidati:
        if Path(c).exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Capitoli
# ---------------------------------------------------------------------------

def leggi_capitoli(path: Path) -> list:
    """Legge i capitoli esistenti in un file (ffprobe -show_chapters).
    Ritorna una lista di {"start": float secondi, "title": str}, ordinata per
    inizio. La fine di ogni capitolo non serve qui: viene ricalcolata al
    momento della scrittura (vedi scrivi_ffmetadata_capitoli) come inizio del
    capitolo successivo, o la durata del file per l'ultimo — i capitoli sono
    trattati come marcatori puntuali, non intervalli con fine scelta
    dall'utente."""
    cmd = ["ffprobe", "-v", "error", "-show_chapters", "-print_format", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(r.stdout)
    except Exception:
        return []
    capitoli = []
    for c in data.get("chapters", []):
        try:
            inizio = float(c.get("start_time", 0))
        except (TypeError, ValueError):
            inizio = 0.0
        capitoli.append({"start": inizio, "title": c.get("tags", {}).get("title", "")})
    capitoli.sort(key=lambda c: c["start"])
    return capitoli


def parsa_timestamp_capitolo(testo: str):
    """Converte 'hh:mm:ss[.mmm]', 'mm:ss[.mmm]' o secondi puri (anche
    decimali) in float (secondi). None se non interpretabile."""
    testo = testo.strip().replace(",", ".")
    if not testo:
        return None
    parti = testo.split(":")
    try:
        if len(parti) == 3:
            h, m, s = parti
            val = int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parti) == 2:
            m, s = parti
            val = int(m) * 60 + float(s)
        elif len(parti) == 1:
            val = float(parti[0])
        else:
            return None
    except ValueError:
        return None
    return val if val >= 0 else None


def formatta_timestamp_capitolo(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def escape_ffmetadata(testo: str) -> str:
    """Escape dei caratteri speciali richiesti dal formato FFMETADATA1 di
    ffmpeg (\\, =, ;, #, a capo) — verificato che senza questo escape un
    titolo contenente uno di questi caratteri romperebbe il parsing del file
    generato (es. ';' interpretato come inizio di un commento)."""
    return re.sub(r'([\\=;#\n])', r'\\\1', testo)


def scrivi_ffmetadata_capitoli(capitoli: list, durata_totale: float, percorso: Path):
    """Scrive un file FFMETADATA1 con un blocco [CHAPTER] per ogni capitolo
    (ordinati per inizio). La fine di ciascuno è implicita: l'inizio del
    successivo, o durata_totale per l'ultimo."""
    capitoli_ordinati = sorted(capitoli, key=lambda c: c["start"])
    righe = [";FFMETADATA1"]
    for i, c in enumerate(capitoli_ordinati):
        inizio_ms = round(c["start"] * 1000)
        if i + 1 < len(capitoli_ordinati):
            fine_ms = round(capitoli_ordinati[i + 1]["start"] * 1000)
        else:
            fine_ms = round(durata_totale * 1000) if durata_totale > 0 else inizio_ms + 1000
        fine_ms = max(fine_ms, inizio_ms + 1)  # END deve essere sempre > START
        righe += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={inizio_ms}", f"END={fine_ms}",
                  f"title={escape_ffmetadata(c['title'])}"]
    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")


def build_capitoli_cmd(src: Path, meta_path: Path, dst: Path) -> list:
    """Comando ffmpeg per applicare una nuova lista di capitoli: remux puro
    (-c copy), mantiene tutti gli stream e i metadati globali del sorgente
    (-map_metadata 0) ma prende i capitoli dal file FFMETADATA passato come
    secondo input (-map_chapters 1) — verificato che questo non tocca gli
    altri metadati del file (titolo, tag, ecc.), solo i capitoli."""
    return ["ffmpeg", "-y", "-i", str(src), "-i", str(meta_path),
            "-map", "0", "-map_metadata", "0", "-map_chapters", "1",
            "-c", "copy", str(dst)]


def formatta_capitoli_ogm(capitoli: list) -> str:
    """Esporta una lista di capitoli nel formato OGM/SimpleChapters (vedi
    parsa_capitoli_ogm), lo stesso usato per l'importazione — utile per
    salvare/condividere l'elenco capitoli indipendentemente dal file video."""
    capitoli_ordinati = sorted(capitoli, key=lambda c: c["start"])
    righe = []
    for i, c in enumerate(capitoli_ordinati, start=1):
        righe.append(f"CHAPTER{i:02d}={formatta_timestamp_capitolo(c['start'])}")
        righe.append(f"CHAPTER{i:02d}NAME={c['title']}")
    return "\n".join(righe) + "\n"


def parsa_capitoli_ogm(testo: str) -> list:
    """Importa capitoli dal formato OGM/SimpleChapters, il più diffuso per
    questo tipo di file (usato da mkvmerge --chapters, molti tool di rip):
        CHAPTER01=00:00:00.000
        CHAPTER01NAME=Titolo
        CHAPTER02=00:05:00.000
        CHAPTER02NAME=Altro titolo
    Ritorna una lista di {"start": float secondi, "title": str}, ordinata per
    inizio. Righe non riconosciute vengono ignorate."""
    tempi, nomi = {}, {}
    for riga in testo.splitlines():
        riga = riga.strip()
        m = re.match(r'CHAPTER(\d+)=(\d+):(\d{2}):(\d{2}(?:[.,]\d+)?)$', riga, re.IGNORECASE)
        if m:
            n, h, mi, s = m.groups()
            tempi[n] = int(h) * 3600 + int(mi) * 60 + float(s.replace(",", "."))
            continue
        m = re.match(r'CHAPTER(\d+)NAME=(.*)$', riga, re.IGNORECASE)
        if m:
            n, nome = m.groups()
            nomi[n] = nome
    capitoli = [{"start": start, "title": nomi.get(n, f"Capitolo {n}")}
                for n, start in tempi.items()]
    capitoli.sort(key=lambda c: c["start"])
    return capitoli


def usa_mkvpropedit_per(path: Path) -> bool:
    """True se conviene usare mkvpropedit invece di ffmpeg per un'operazione
    di soli metadati su questo file: serve un .mkv/.mka E il programma
    installato (vedi trova_mkvpropedit)."""
    return path.suffix.lower() in (".mkv", ".mka") and trova_mkvpropedit() is not None


def genera_capitoli(src: Path, capitoli: list, durata_totale: float,
                     log_q: queue.Queue, stop_ev: threading.Event) -> bool:
    """Applica la lista di capitoli AL FILE SORGENTE STESSO (non genera un
    file separato): se è un .mkv e mkvpropedit è disponibile, lo modifica SUL
    POSTO senza riscriverlo — nessun file temporaneo, nessuna copia dei dati
    audio/video (verificato molto più veloce di un remux ffmpeg equivalente,
    indipendentemente dalla dimensione del file). Altrimenti ripiega sul
    remux ffmpeg (file temporaneo poi sostituisce l'originale, stessa logica
    di fix_bitrate_tag): un rewrite completo è in quel caso inevitabile con
    gli strumenti disponibili, ma il file non viene comunque duplicato."""
    log_q.put(("info", f"\n▶ {src.name}"))

    mkvpropedit = trova_mkvpropedit() if src.suffix.lower() in (".mkv", ".mka") else None
    if mkvpropedit:
        chap_tmp = Path(tempfile.gettempdir()) / f"_capitoli_ogm_{os.getpid()}.txt"
        try:
            chap_tmp.write_text(formatta_capitoli_ogm(capitoli), encoding="utf-8")
            cmd = [mkvpropedit, str(src), "--chapters", str(chap_tmp)]
            log_q.put(("cmd", "  $ " + " ".join(cmd)))
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for riga in (r.stdout or "").splitlines():
                if riga.strip():
                    log_q.put(("detail", "  " + riga.strip()))
            if r.returncode != 0:
                log_q.put(("error", f"  ✗ mkvpropedit ha restituito il codice {r.returncode}"))
                return False
            log_q.put(("ok", f"  ✓ {src.name}  [{len(capitoli)} capitoli, modificati sul posto]"))
            return True
        except Exception as e:
            log_q.put(("error", f"  ✗ Eccezione: {e}"))
            return False
        finally:
            chap_tmp.unlink(missing_ok=True)

    # --- fallback ffmpeg: remux completo (inevitabile senza mkvpropedit),
    # ma sovrascrive il sorgente invece di lasciare un file duplicato ---
    src_stat = src.stat()
    ct = getattr(src_stat, "st_birthtime", src_stat.st_ctime)
    orig_dt = datetime.fromtimestamp(min(ct, src_stat.st_mtime), tz=timezone.utc)

    meta_tmp = Path(tempfile.gettempdir()) / f"_capitoli_meta_{os.getpid()}.txt"
    remux_tmp = src.with_name(f".{src.stem}.capitolifix{src.suffix}")
    try:
        scrivi_ffmetadata_capitoli(capitoli, durata_totale, meta_tmp)
        cmd = build_capitoli_cmd(src, meta_tmp, remux_tmp)
        log_q.put(("cmd", "  $ " + " ".join(cmd)))

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        interrotto = False
        for line in proc.stdout:
            if stop_ev.is_set():
                interrotto = True
                proc.terminate()
                break
            line = line.rstrip()
            if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                log_q.put(("progress", line))
            elif line:
                log_q.put(("detail", "  " + line))
        proc.wait()

        if interrotto:
            log_q.put(("detail", "  Interrotto dall'utente."))
            remux_tmp.unlink(missing_ok=True)
            return False

        if proc.returncode != 0 or not remux_tmp.exists():
            log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
            remux_tmp.unlink(missing_ok=True)
            return False

        remux_tmp.replace(src)
        if not imposta_timestamp_file(src, orig_dt):
            log_q.put(("detail", "  Avviso: impossibile impostare la data del file"))

        log_q.put(("ok", f"  ✓ {src.name}  [{len(capitoli)} capitoli]"))
        return True

    except Exception as e:
        log_q.put(("error", f"  ✗ Eccezione: {e}"))
        return False
    finally:
        meta_tmp.unlink(missing_ok=True)
        remux_tmp.unlink(missing_ok=True)


def capitoli_worker(src: Path, capitoli: list, durata_totale: float,
                     log_q: queue.Queue, stop_ev: threading.Event):
    ok = genera_capitoli(src, capitoli, durata_totale, log_q, stop_ev)
    log_q.put(("summary", f"\n{'='*60}\n{'Completato' if ok else 'Fallito'}\n{'='*60}"))
    log_q.put(("done", None))


def esegui_estrazione(cmd: list, dst: Path, log_q: queue.Queue, stop_ev: threading.Event) -> bool:
    """Esegue l'estrazione di una singola traccia (build_extract_cmd), stessa
    logica di interruzione/logging di esegui_mux/converti_file."""
    log_q.put(("info", f"\n▶ {dst.name}"))
    log_q.put(("cmd", "  $ " + " ".join(cmd)))

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        interrotto = False
        for line in proc.stdout:
            if stop_ev.is_set():
                interrotto = True
                proc.terminate()
                break
            line = line.rstrip()
            if any(k in line for k in ("frame=", "fps=", "time=", "speed=")):
                log_q.put(("progress", line))
            elif line:
                log_q.put(("detail", "  " + line))
        proc.wait()

        if interrotto:
            log_q.put(("detail", "  Interrotto dall'utente."))
            dst.unlink(missing_ok=True)
            return False

        if proc.returncode != 0:
            log_q.put(("error", f"  ✗ Errore ffmpeg (codice {proc.returncode})"))
            return False

        log_q.put(("ok", f"  ✓ {dst.name}"))
        return True

    except Exception as e:
        log_q.put(("error", f"  ✗ Eccezione: {e}"))
        return False


def extract_worker(estrazioni: list, log_q: queue.Queue, stop_ev: threading.Event):
    """estrazioni: lista di tuple (cmd, dst) da eseguire in sequenza, una per
    traccia selezionata (vedi App._mux_estrai)."""
    ok = err = 0
    for cmd, dst in estrazioni:
        if stop_ev.is_set():
            log_q.put(("detail", "\nEstrazione interrotta dall'utente."))
            break
        if esegui_estrazione(cmd, dst, log_q, stop_ev):
            ok += 1
        else:
            err += 1

    log_q.put(("summary", f"\n{'='*60}\nCompletato: {ok} OK, {err} errori\n{'='*60}"))
    log_q.put(("done", None))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class Tooltip:
    """Help balloon che appare vicino al widget dopo una breve sosta del mouse."""

    def __init__(self, widget, text: str, delay: int = 500):
        self.widget = widget
        self.text   = text
        self.delay  = delay
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip or not self.widget.winfo_ismapped():
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        ttk.Label(self._tip, text=self.text, background="#ffffe0",
                  relief="solid", borderwidth=1, padding=(6, 3),
                  foreground="#333333").pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


_BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk


def argomento_avvio():
    """Cartella (e, se specificato, file da preselezionare) da usare
    all'avvio. Se il programma è stato aperto con un percorso come argomento
    da riga di comando — es. "Apri con" di un file manager come XYplorer,
    che tipicamente passa il percorso invece di cambiare la working
    directory del processo — usa quello: cartella diretta, o cartella del
    file (con quel file preselezionato) se l'argomento è un file specifico.
    Altrimenti ripiega sulla working directory corrente del processo (utile
    per un avvio "in loco", es. da un terminale già posizionato lì)."""
    if len(sys.argv) > 1:
        arg = Path(sys.argv[1])
        if arg.is_dir():
            return arg, None
        if arg.is_file():
            return arg.parent, arg
    return Path.cwd(), None


class App(_BaseTk):
    def __init__(self):
        super().__init__()
        self.title("MediaForge — encode / mux / chapters")
        self.resizable(True, True)
        self.minsize(420, 300)
        self.geometry("1300x720")

        # Cartella (ed eventuale file specifico) da usare all'avvio — vedi
        # argomento_avvio: usato subito sotto per _var_dir, e più avanti per
        # preselezionare il file, dopo che _cerca_file() ha popolato la lista.
        self._cartella_iniziale, self._file_iniziale = argomento_avvio()

        self._all_files: list[Path] = []
        self._stop_ev   = threading.Event()
        self._log_q     = queue.Queue()
        self._stream_vars: list[tk.BooleanVar] = []
        self._probe_cache: dict = {}  # str(path) -> dati ffprobe, vedi _ottieni_probe
        self._bitrate_video_cache: dict = {}  # str(path) -> bit/s, vedi _ottieni_bitrate_video
        self._var_ss_on = None
        self._var_t_on  = None
        self._active_op = None  # None | "codifica" | "mux" | "capitoli": evita avvii concorrenti tra i tab

        # --- Stato tab Mux ---
        self._mux_src: Path | None   = None
        self._mux_cached_probe: dict = {}
        self._mux_stream_vars: list[tk.BooleanVar] = []
        self._mux_extract_vars: list[tk.BooleanVar] = []
        # Titolo editabile per le tracce MANTENUTE dal sorgente (indice
        # parallelo a stream/-vars sopra); precompilato con title/name se
        # già presente nel file, comunque modificabile senza aggiungere
        # tracce esterne.
        self._mux_title_vars: list[tk.StringVar] = []
        # Ogni elemento: {"path": Path, "lang": tk.StringVar, "title": tk.StringVar,
        #                 "default": tk.BooleanVar, "forced": tk.BooleanVar}
        self._mux_extra_tracks: list[dict] = []
        self._mux_extra_probes: dict = {}  # str(path) -> dati ffprobe della traccia esterna

        # --- Stato tab Capitoli ---
        self._capitoli_src: Path | None = None
        self._capitoli_durata: float = 0.0
        # Ogni elemento: {"start": tk.StringVar, "title": tk.StringVar}
        self._capitoli: list[dict] = []

        self._build_scroll_container()
        self._build_ui()
        self._bind_traces()
        self._toggle_vcodec()   # sincronizza stato widget con l'encoder iniziale
        self._cerca_file()      # popola subito con la cartella corrente, senza dover premere nulla
        if self._file_iniziale is not None:
            # "Apri con" su un file specifico (non solo una cartella): lo
            # preseleziona subito, come se l'utente lo avesse già cliccato.
            for i, f in enumerate(self._all_files):
                if f.name == self._file_iniziale.name:
                    self._listbox.selection_clear(0, "end")
                    self._listbox.selection_set(i)
                    self._on_selezione()
                    break
        self._poll_log()

    # -----------------------------------------------------------------------
    # Contenitore scrollabile (finestra ridimensionabile su monitor piccoli)
    # -----------------------------------------------------------------------

    def _build_scroll_container(self):
        """Avvolge tutta l'interfaccia in un Canvas scrollabile in entrambe le
        direzioni, così se la finestra viene rimpicciolita nessun controllo
        sparisce o si sovrappone senza possibilità di raggiungerlo: appaiono
        semplicemente le scrollbar."""
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical",   command=canvas.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        content = ttk.Frame(canvas)
        content_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_content_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        content.bind("<Configure>", _on_content_configure)

        def _on_canvas_configure(event):
            # Il contenuto si allarga per riempire il canvas, ma non scende mai
            # sotto la propria larghezza minima: se il canvas è più stretto,
            # compare la scrollbar orizzontale invece di sovrapporre i widget.
            min_w = content.winfo_reqwidth()
            canvas.itemconfigure(content_id, width=max(event.width, min_w))
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel_y(event):
            if IS_WINDOWS or sys.platform == "darwin":
                delta = -1 if event.delta > 0 else 1
            else:
                delta = -1 if event.num == 4 else 1
            canvas.yview_scroll(delta, "units")

        def _on_mousewheel_x(event):
            if IS_WINDOWS or sys.platform == "darwin":
                delta = -1 if event.delta > 0 else 1
            else:
                delta = -1 if event.num == 4 else 1
            canvas.xview_scroll(delta, "units")

        # Attivo lo scroll con la rotellina solo mentre il puntatore è sopra il
        # canvas, per non "rubare" la rotellina ad altre finestre/widget.
        # Shift+rotellina = scorrimento orizzontale (convenzione comune).
        def _bind_wheel(_e=None):
            canvas.bind_all("<MouseWheel>", _on_mousewheel_y)
            canvas.bind_all("<Shift-MouseWheel>", _on_mousewheel_x)
            canvas.bind_all("<Button-4>", _on_mousewheel_y)
            canvas.bind_all("<Button-5>", _on_mousewheel_y)
            canvas.bind_all("<Shift-Button-4>", _on_mousewheel_x)
            canvas.bind_all("<Shift-Button-5>", _on_mousewheel_x)

        def _unbind_wheel(_e=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Shift-MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
            canvas.unbind_all("<Shift-Button-4>")
            canvas.unbind_all("<Shift-Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        self._content = content

    def _bind_scroll_locale(self, widget):
        """Fa scorrere SOLO questo widget con la rotella quando il puntatore
        è sopra di lui, invece di far scorrere il contenitore esterno
        (che intercetta la rotella ovunque nella finestra via bind_all, vedi
        _build_scroll_container): senza questo binding locale — a priorità
        più alta di un bind_all — la rotella sopra un riquadro con
        scrollbar propria (es. i metadati) scorreva la pagina intera invece
        del riquadro stesso. return "break" ferma la propagazione
        dell'evento, evitando che scorrano entrambi insieme."""
        def _scroll(event):
            if IS_WINDOWS or sys.platform == "darwin":
                delta = -1 if event.delta > 0 else 1
            else:
                delta = -1 if event.num == 4 else 1
            widget.yview_scroll(delta, "units")
            return "break"
        widget.bind("<MouseWheel>", _scroll)
        widget.bind("<Button-4>", _scroll)
        widget.bind("<Button-5>", _scroll)

    # -----------------------------------------------------------------------
    # Costruzione UI
    # -----------------------------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=10, pady=4)
        root = self._content  # tutti i widget di primo livello vanno nel frame scrollabile

        # === Tab: Codifica / Mux / Capitoli (il Log resta condiviso, fuori dal Notebook) ===
        self._nb = ttk.Notebook(root)
        self._nb.pack(fill="both", expand=True)
        tab_cod = ttk.Frame(self._nb)
        tab_mux = ttk.Frame(self._nb)
        tab_cap = ttk.Frame(self._nb)
        self._nb.add(tab_cod, text="Codifica")
        self._nb.add(tab_mux, text="Mux")
        self._nb.add(tab_cap, text="Capitoli")
        # Riferimenti tenuti per _on_tab_changed: passando a Mux/Capitoli, il
        # file selezionato in Codifica (se uno solo) viene caricato da sé,
        # senza dover premere "Usa file da Codifica" ad ogni cambio tab.
        self._tab_mux = tab_mux
        self._tab_cap = tab_cap
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # === Cartella ===
        frm_dir = ttk.LabelFrame(tab_cod, text="Cartella sorgente")
        frm_dir.pack(fill="x", **pad)
        self._var_dir = tk.StringVar(value=str(self._cartella_iniziale))
        ttk.Entry(frm_dir, textvariable=self._var_dir).pack(fill="x", padx=5, pady=(5, 2))
        frm_dir_btns = ttk.Frame(frm_dir)
        frm_dir_btns.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Button(frm_dir_btns, text="Sfoglia…", command=self._scegli_dir).pack(side="left")
        ttk.Button(frm_dir_btns, text="Cartella corrente",
                   command=self._dir_corrente).pack(side="left", padx=(5, 0))

        # === Opzioni codifica ===
        frm_opt = ttk.LabelFrame(tab_cod, text="Opzioni di codifica")
        frm_opt.pack(fill="x", **pad)

        # Riga 0: modalità (Video / Solo audio)
        r0 = ttk.Frame(frm_opt); r0.pack(fill="x", padx=5, pady=(5, 3))
        ttk.Label(r0, text="Modalità:").pack(side="left")
        self._var_mode = tk.StringVar(value="video")
        ttk.Radiobutton(r0, text="Video", variable=self._var_mode, value="video",
                        command=self._toggle_mode).pack(side="left", padx=(6, 4))
        ttk.Radiobutton(r0, text="Solo audio (estrai/converti audio, nessun video)",
                        variable=self._var_mode, value="audio",
                        command=self._toggle_mode).pack(side="left", padx=4)
        rb_fixtag = ttk.Radiobutton(r0, text="Fix tag QuickTime (hvc1, nessuna ricodifica)",
                                    variable=self._var_mode, value="fixtag",
                                    command=self._toggle_mode)
        rb_fixtag.pack(side="left", padx=4)
        Tooltip(rb_fixtag, "Corregge solo il tag del codec HEVC (hev1 → hvc1) nei mp4\n"
                            "già codificati, così i video si aprono anche su iPhone/iPad/Mac.\n"
                            "Nessuna ricodifica: stream audio/video/sottotitoli, metadati,\n"
                            "data di codifica e data del file restano invariati.")
        rb_fixbitrate = ttk.Radiobutton(r0, text="Correggi tag bitrate (BPS, nessuna ricodifica)",
                                        variable=self._var_mode, value="fixbitrate",
                                        command=self._toggle_mode)
        rb_fixbitrate.pack(side="left", padx=4)
        Tooltip(rb_fixbitrate, "Per file mkv già ricodificati PRIMA che il programma correggesse\n"
                                "da sé questo problema: il tag 'BPS' (bitrate video) resta quello\n"
                                "del file di partenza anche dopo una ricodifica a un bitrate diverso\n"
                                "(-map_metadata 0 lo copia alla cieca, senza ricalcolarlo).\n"
                                "Premendo \"▶ Avvia\" il bitrate reale viene calcolato SUBITO per\n"
                                "ogni file selezionato (somma i byte reali dei pacchetti) e\n"
                                "confrontato con quello dichiarato: solo dopo aver visto il\n"
                                "riepilogo scegli se correggere (nessuna ricodifica, solo il tag;\n"
                                "i file già corretti non vengono toccati).")
        ttk.Separator(frm_opt, orient="horizontal").pack(fill="x", padx=5, pady=(0, 3))

        # Contenitore dei controlli specifici video (nascosto in modalità "Solo audio")
        self._frm_video_specific = ttk.Frame(frm_opt)
        self._frm_video_specific.pack(fill="x")

        # Riga 1: codec video / qualità / formato
        r1 = ttk.Frame(self._frm_video_specific); r1.pack(fill="x", padx=5, pady=3)
        ttk.Label(r1, text="Video:").pack(side="left")
        # Encoder compilati in ffmpeg; il probe hardware parte in background
        self._enc_compilati = encoder_compilati()
        self._vcodec_id_by_label = {}
        self._var_vcodec = tk.StringVar()
        self._combo_vcodec = ttk.Combobox(r1, textvariable=self._var_vcodec,
                                          width=40, state="readonly")
        self._combo_vcodec.pack(side="left", padx=(4,8))
        self._combo_vcodec.bind("<<ComboboxSelected>>", lambda _e: self._toggle_vcodec())
        # Popola subito con gli encoder compilati (nessuno ancora verificato)
        vals = self._popola_vcodec(None)
        self._var_vcodec.set(vals[0])
        threading.Thread(target=self._probe_encoder, daemon=True).start()

        ttk.Separator(r1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(r1, text="Qualità:").pack(side="left", padx=(8,4))
        self._var_quality = tk.IntVar(value=25)
        self._spin_quality = ttk.Spinbox(r1, from_=1, to=51, textvariable=self._var_quality, width=5)
        self._spin_quality.pack(side="left", padx=(0,20))
        Tooltip(self._spin_quality, "1 = qualità massima\n51 = qualità minima")

        ttk.Label(r1, text="Formato output:").pack(side="left")
        self._var_ext = tk.StringVar(value="mp4")
        ttk.Combobox(r1, textvariable=self._var_ext, values=ESTENSIONI_OUTPUT,
                     width=6, state="readonly").pack(side="left", padx=4)

        # Riga 2: look_ahead / FPS / risoluzione
        r2 = ttk.Frame(self._frm_video_specific); r2.pack(fill="x", padx=5, pady=3)
        self._var_lookahead = tk.BooleanVar(value=True)
        self._chk_lookahead = ttk.Checkbutton(r2, text="Look-ahead", variable=self._var_lookahead)
        self._chk_lookahead.pack(side="left")
        self._var_limit_fps = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Limita FPS a", variable=self._var_limit_fps,
                        command=self._toggle_fps).pack(side="left", padx=(20,4))
        self._var_fps = tk.IntVar(value=30)
        self._spin_fps = ttk.Spinbox(r2, from_=1, to=120, textvariable=self._var_fps,
                                     width=5, state="disabled")
        self._spin_fps.pack(side="left")
        ttk.Label(r2, text="fps").pack(side="left", padx=2)

        ttk.Separator(r2, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(r2, text="Risoluzione max:").pack(side="left")
        self._var_res = tk.StringVar(value="1080")
        for val, lbl in [("1080","1080p"), ("720","720p"), ("0","Nessun limite")]:
            ttk.Radiobutton(r2, text=lbl, variable=self._var_res, value=val).pack(side="left", padx=6)

        # Riga 2b: filtri video
        r2b = ttk.Frame(self._frm_video_specific); r2b.pack(fill="x", padx=5, pady=3)
        ttk.Label(r2b, text="Filtri video:").pack(side="left")
        self._var_hflip = tk.BooleanVar(value=False)
        self._var_vflip = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2b, text="Capovolgi orizzontale (hflip)",
                        variable=self._var_hflip).pack(side="left", padx=(8,4))
        ttk.Checkbutton(r2b, text="Capovolgi verticale (vflip)",
                        variable=self._var_vflip).pack(side="left", padx=4)

        # Riga 2c: aspect ratio
        r2c = ttk.Frame(self._frm_video_specific); r2c.pack(fill="x", padx=5, pady=3)
        ttk.Label(r2c, text="Aspect ratio:").pack(side="left")
        self._var_ar_mode = tk.StringVar(value="nessuna")
        for val, lbl in [("nessuna", "Nessuna modifica"), ("crop", "Ritaglia (crop)"),
                          ("pad", "Aggiungi barre (pad)"), ("dar", "Solo correggi DAR"),
                          ("stretch", "Deforma (stretch)")]:
            ttk.Radiobutton(r2c, text=lbl, variable=self._var_ar_mode, value=val,
                            command=self._toggle_ar).pack(side="left", padx=6)
        ttk.Label(r2c, text="Rapporto:").pack(side="left", padx=(12, 4))
        self._var_ar_ratio = tk.StringVar(value="16:9")
        self._combo_ar = ttk.Combobox(r2c, textvariable=self._var_ar_ratio,
                                      values=["Originale", "16:9", "4:3", "21:9", "1:1",
                                              "1.85:1", "2.35:1", "2.39:1"],
                                      width=8, state="disabled")
        self._combo_ar.pack(side="left")
        self._combo_ar.bind("<KeyRelease>", lambda _e: self._aggiorna_anteprima())
        Tooltip(self._combo_ar, "Scegli dal menu o scrivi un rapporto qualsiasi:\n"
                                 "formato 'larghezza:altezza' (es. 16:9, 2.40:1) o decimale (es. 1.85).\n"
                                 "'Originale': usa il rapporto di visualizzazione attuale del file\n"
                                 "(utile con \"Solo DAR\" per riscrivere/confermare il tag).\n"
                                 "Ritaglia/Aggiungi barre: rapporto del fotogramma finale.\n"
                                 "Solo DAR: corregge il metadato senza toccare i pixel.\n"
                                 "Deforma: come Solo DAR ma cambiando davvero i pixel (stira "
                                 "l'immagine, distorce se il rapporto è molto diverso dall'originale).")
        self._lbl_ar_valore = ttk.Label(r2c, text="", foreground="gray")
        self._lbl_ar_valore.pack(side="left", padx=(8, 0))
        ttk.Button(r2c, text="👁  Anteprima frame",
                   command=self._mostra_anteprima_filtro).pack(side="left", padx=(16, 0))

        # Riga 2d: avviso pixel non quadrati (compare solo se rilevati nel
        # file selezionato — vedi _mostra_stream/rileva_pixel_non_quadrati)
        self._lbl_sar_avviso = ttk.Label(self._frm_video_specific, foreground="#cc6600")
        # wraplength = larghezza attuale della label: così il testo va a capo
        # invece di spingere la finestra oltre la larghezza iniziale (si
        # aggiorna da sola anche se la finestra viene poi ridimensionata).
        self._lbl_sar_avviso.bind(
            "<Configure>", lambda e: self._lbl_sar_avviso.configure(wraplength=e.width))
        # Non lo pack() subito: appare/scompare dinamicamente

        # Riga 3: audio (sempre visibile, anche in modalità "Solo audio")
        r3 = ttk.Frame(frm_opt); r3.pack(fill="x", padx=5, pady=3)
        self._frm_audio_row = r3  # ancora per ripristinare l'ordine dei pannelli
        ttk.Label(r3, text="Audio:").pack(side="left")
        self._var_audio = tk.StringVar(value="copy")
        for val, lbl in [("copy","Copia originale"), ("aac","AAC 128k"),
                         ("ac3","AC3 384k"), ("mp3","MP3 128k")]:
            ttk.Radiobutton(r3, text=lbl, variable=self._var_audio, value=val).pack(side="left", padx=6)
        self._lbl_audio_ext = ttk.Label(r3, text="", foreground="gray")
        self._lbl_audio_ext.pack(side="left", padx=(12, 0))

        # Sottotitoli (nascosto in modalità "Solo audio": un contenitore audio non li supporta)
        self._frm_subs = ttk.Frame(frm_opt); self._frm_subs.pack(fill="x", padx=5, pady=3)
        ttk.Label(self._frm_subs, text="Sottotitoli:").pack(side="left")
        self._var_subs = tk.StringVar(value="copy")
        for val, lbl in [("copy","Copia"), ("srt","Converti in SRT"), ("no","Escludi")]:
            ttk.Radiobutton(self._frm_subs, text=lbl, variable=self._var_subs, value=val).pack(side="left", padx=6)

        # Riga 4: timestamp
        r4 = ttk.Frame(frm_opt); r4.pack(fill="x", padx=5, pady=3)
        self._frm_timestamp_row = r4  # ancora per ripristinare l'ordine dei pannelli
        ttk.Label(r4, text="Timestamp file output:").pack(side="left")
        self._var_ts = tk.StringVar(value="metadati")
        for val, lbl in [("metadati","Da metadati video (creation_time)"),
                         ("file",    "Copia date dal file sorgente"),
                         ("nessuno", "Non copiare")]:
            ttk.Radiobutton(r4, text=lbl, variable=self._var_ts, value=val).pack(side="left", padx=8)

        # === File trovati + Metadati (due colonne affiancate) ===
        frm_files_meta = ttk.Frame(tab_cod)
        frm_files_meta.pack(fill="both", **pad)
        frm_files_meta.columnconfigure(0, weight=1)
        frm_files_meta.columnconfigure(1, weight=1)

        frm_files = ttk.LabelFrame(frm_files_meta,
            text="File trovati  (Ctrl+A = tutti  |  selezione singola = opzioni stream)")
        frm_files.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        # Altezza righe pari a 1.5 volte quella originale (6 -> 9), stessa
        # per entrambe le colonne così il blocco risulta visivamente allineato.
        self._listbox = tk.Listbox(frm_files, selectmode="extended", height=9, exportselection=False)
        self._listbox.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        sb = ttk.Scrollbar(frm_files, orient="vertical", command=self._listbox.yview)
        sb.pack(side="right", fill="y", pady=5)
        self._listbox.configure(yscrollcommand=sb.set)
        if HAS_DND:
            self._listbox.drop_target_register(DND_FILES)
            self._listbox.dnd_bind("<<Drop>>", self._on_drop_file_list)

        # === Metadati (visibile solo con file singolo) ===
        self._frm_meta = ttk.LabelFrame(frm_files_meta, text="Metadati interni (solo file singolo)")
        # Non lo grid() subito: appare dinamicamente (vedi _mostra_metadati/
        # _nascondi_stream) — è un fratello di frm_files nella stessa griglia
        # a due colonne, quindi usa grid()/grid_remove(), non pack().
        self._txt_meta = tk.Text(self._frm_meta, height=9, state="disabled",
                                 wrap="none", font=("Consolas", 8),
                                 background="#f5f5f5")
        self._txt_meta.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        sb_meta = ttk.Scrollbar(self._frm_meta, orient="vertical", command=self._txt_meta.yview)
        sb_meta.pack(side="right", fill="y", pady=5)
        self._txt_meta.configure(yscrollcommand=sb_meta.set)
        self._bind_scroll_locale(self._txt_meta)

        frm_cerca = ttk.Frame(tab_cod); frm_cerca.pack(fill="x", padx=10)
        ttk.Button(frm_cerca, text="🔍 Cerca file", command=self._cerca_file).pack(side="left")
        self._lbl_files = ttk.Label(frm_cerca, text="")
        self._lbl_files.pack(side="left", padx=10)
        if HAS_DND:
            ttk.Label(frm_cerca, text="(trascina qui i file per aggiungerli direttamente)",
                      foreground="gray").pack(side="left", padx=10)
        # Tag colori
        self._txt_meta.tag_config("key",      foreground="#0066cc")
        self._txt_meta.tag_config("val",      foreground="#333333")
        self._txt_meta.tag_config("section",  foreground="#888888", font=("Consolas", 8, "italic"))
        self._txt_meta.tag_config("warning",  foreground="#cc6600")

        # === Trim (visibile solo con file singolo) ===
        self._frm_trim = ttk.LabelFrame(tab_cod, text="Taglio (solo file singolo)")
        # Non lo pack() subito: appare dinamicamente
        self._frm_trim_inner = ttk.Frame(self._frm_trim)
        self._frm_trim_inner.pack(fill="x", padx=5, pady=6)

        # === Stream (visibile solo con file singolo) ===
        self._frm_streams = ttk.LabelFrame(tab_cod, text="Stream — selezione (solo file singolo)")
        # Non lo pack() subito: appare dinamicamente
        self._frm_streams_inner = ttk.Frame(self._frm_streams)
        self._frm_streams_inner.pack(fill="x", padx=5, pady=4)

        # === Anteprima comando ===
        self._frm_cmd = ttk.LabelFrame(tab_cod, text="Comando generato")
        self._frm_cmd.pack(fill="x", **pad)
        frm_cmd = self._frm_cmd
        self._var_cmd = tk.StringVar()
        frm_cmd_inner = ttk.Frame(frm_cmd)
        frm_cmd_inner.pack(fill="x", padx=5, pady=4)
        cmd_entry = ttk.Entry(frm_cmd_inner, textvariable=self._var_cmd, state="readonly")
        cmd_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(frm_cmd_inner, text="📋", width=3,
                   command=lambda: self.clipboard_clear() or self.clipboard_append(self._var_cmd.get())
                   ).pack(side="left", padx=(4,0))
        self._lbl_cmd_warning = ttk.Label(frm_cmd, text="", foreground="#cc6600")
        # Non lo pack() subito: appare solo quando c'è un avviso da mostrare

        # === Avvio ===
        frm_run = ttk.Frame(tab_cod); frm_run.pack(fill="x", **pad)
        self._btn_start = ttk.Button(frm_run, text="▶  Avvia", command=self._avvia)
        self._btn_start.pack(side="left")
        self._btn_stop = ttk.Button(frm_run, text="⏹  Interrompi", command=self._interrompi, state="disabled")
        self._btn_stop.pack(side="left", padx=8)
        self._lbl_stato = ttk.Label(frm_run, text="")
        self._lbl_stato.pack(side="left", padx=10)

        # =====================================================================
        # TAB MUX: gestore tracce unificato — apri un file, tieni/togli le sue
        # tracce, aggiungi tracce esterne (audio/sottotitoli), genera il nuovo
        # file. Sempre remux puro (-c copy): nessuna ricodifica, solo eventuale
        # forzatura di tag/codec quando il contenitore di destinazione non
        # supporta quello sorgente (stessa logica già usata in Codifica).
        # =====================================================================

        # === File sorgente ===
        frm_mux_src = ttk.LabelFrame(tab_mux, text="File sorgente")
        frm_mux_src.pack(fill="x", **pad)
        self._var_mux_src = tk.StringVar()
        entry_mux_src = ttk.Entry(frm_mux_src, textvariable=self._var_mux_src, state="readonly")
        entry_mux_src.pack(fill="x", padx=5, pady=(5, 2))
        frm_mux_src_btns = ttk.Frame(frm_mux_src)
        frm_mux_src_btns.pack(fill="x", padx=5, pady=(0, 5))
        ttk.Button(frm_mux_src_btns, text="Apri file…", command=self._mux_scegli_file).pack(side="left")
        ttk.Button(frm_mux_src_btns, text="Usa file da Codifica",
                   command=self._mux_usa_da_codifica).pack(side="left", padx=(5, 0))
        if HAS_DND:
            entry_mux_src.drop_target_register(DND_FILES)
            entry_mux_src.dnd_bind("<<Drop>>", self._on_drop_mux_src)

        # === Tracce attuali (popolato dinamicamente da _mux_carica) ===
        self._frm_mux_tracce = ttk.LabelFrame(tab_mux, text="Tracce attuali")
        self._frm_mux_tracce.pack(fill="x", **pad)
        self._frm_mux_tracce_inner = ttk.Frame(self._frm_mux_tracce)
        self._frm_mux_tracce_inner.pack(fill="x", padx=5, pady=4)
        self._lbl_mux_no_file = ttk.Label(self._frm_mux_tracce_inner,
                                          text="Nessun file aperto.", foreground="gray")
        self._lbl_mux_no_file.pack(anchor="w")

        # === Aggiungi tracce esterne ===
        frm_mux_extra = ttk.LabelFrame(tab_mux, text="Aggiungi tracce esterne (audio/sottotitoli)")
        frm_mux_extra.pack(fill="both", **pad)
        self._frm_mux_extra_inner = ttk.Frame(frm_mux_extra)
        self._frm_mux_extra_inner.pack(fill="both", expand=True, padx=5, pady=5)
        ttk.Label(self._frm_mux_extra_inner, text="Nessuna traccia esterna aggiunta.",
                  foreground="gray").pack(anchor="w")
        if HAS_DND:
            frm_mux_extra.drop_target_register(DND_FILES)
            frm_mux_extra.dnd_bind("<<Drop>>", self._on_drop_mux_extra)
        frm_mux_extra_btns = ttk.Frame(tab_mux); frm_mux_extra_btns.pack(fill="x", padx=10)
        ttk.Button(frm_mux_extra_btns, text="➕ Aggiungi file…",
                   command=self._mux_aggiungi_file).pack(side="left")
        if HAS_DND:
            ttk.Label(frm_mux_extra_btns, text="(o trascina qui i file)",
                      foreground="gray").pack(side="left", padx=10)

        # === Output ===
        frm_mux_out = ttk.LabelFrame(tab_mux, text="Output")
        frm_mux_out.pack(fill="x", **pad)
        ttk.Label(frm_mux_out, text="Formato:").pack(side="left", padx=(5,4))
        self._var_mux_ext = tk.StringVar(value="mkv")
        ttk.Combobox(frm_mux_out, textvariable=self._var_mux_ext, values=ESTENSIONI_OUTPUT,
                     width=6, state="readonly").pack(side="left", padx=4)
        self._lbl_mux_dst = ttk.Label(frm_mux_out, text="", foreground="gray")
        self._lbl_mux_dst.pack(side="left", padx=(12, 0))

        # === Anteprima comando ===
        self._frm_mux_cmd = ttk.LabelFrame(tab_mux, text="Comando generato")
        self._frm_mux_cmd.pack(fill="x", **pad)
        self._var_mux_cmd = tk.StringVar()
        frm_mux_cmd_inner = ttk.Frame(self._frm_mux_cmd)
        frm_mux_cmd_inner.pack(fill="x", padx=5, pady=4)
        ttk.Entry(frm_mux_cmd_inner, textvariable=self._var_mux_cmd, state="readonly").pack(
            side="left", fill="x", expand=True)
        ttk.Button(frm_mux_cmd_inner, text="📋", width=3,
                   command=lambda: self.clipboard_clear() or self.clipboard_append(self._var_mux_cmd.get())
                   ).pack(side="left", padx=(4,0))
        self._lbl_mux_cmd_warning = ttk.Label(self._frm_mux_cmd, text="", foreground="#cc6600")
        # Non lo pack() subito: appare solo quando c'è un avviso da mostrare

        # === Avvio ===
        frm_mux_run = ttk.Frame(tab_mux); frm_mux_run.pack(fill="x", **pad)
        self._btn_mux_start = ttk.Button(frm_mux_run, text="▶  Genera file", command=self._mux_genera)
        self._btn_mux_start.pack(side="left")
        self._btn_mux_stop = ttk.Button(frm_mux_run, text="⏹  Interrompi",
                                        command=self._interrompi, state="disabled")
        self._btn_mux_stop.pack(side="left", padx=8)
        self._lbl_mux_stato = ttk.Label(frm_mux_run, text="")
        self._lbl_mux_stato.pack(side="left", padx=10)

        # =====================================================================
        # TAB CAPITOLI: apri un file, modifica/aggiungi/importa capitoli,
        # applica le modifiche AL FILE STESSO (nessun nuovo file separato).
        # Se è un .mkv/.mka e mkvpropedit (MKVToolNix) è disponibile, lo
        # modifica sul posto senza riscriverlo (nessuna copia dei dati
        # audio/video, verificato molto più veloce anche su file grandi).
        # Altrimenti ripiega su un remux ffmpeg (-c copy, nessuna
        # ricodifica) che sostituisce comunque l'originale invece di
        # duplicarlo. Solo singolo file, niente batch.
        # =====================================================================

        # === File sorgente ===
        frm_cap_src = ttk.LabelFrame(tab_cap, text="File sorgente")
        frm_cap_src.pack(fill="x", **pad)
        self._var_cap_src = tk.StringVar()
        entry_cap_src = ttk.Entry(frm_cap_src, textvariable=self._var_cap_src, state="readonly")
        entry_cap_src.pack(side="left", fill="x", expand=True, padx=5, pady=5)
        ttk.Button(frm_cap_src, text="Apri file…", command=self._capitoli_scegli_file).pack(side="left", padx=5)
        ttk.Button(frm_cap_src, text="Usa file da Codifica",
                   command=self._capitoli_usa_da_codifica).pack(side="left", padx=(0, 5))
        if HAS_DND:
            entry_cap_src.drop_target_register(DND_FILES)
            entry_cap_src.dnd_bind("<<Drop>>", self._on_drop_capitoli_src)

        # === Capitoli attuali (popolato dinamicamente da _capitoli_carica) ===
        self._frm_capitoli = ttk.LabelFrame(tab_cap, text="Capitoli")
        self._frm_capitoli.pack(fill="both", **pad)
        self._frm_capitoli_inner = ttk.Frame(self._frm_capitoli)
        self._frm_capitoli_inner.pack(fill="both", expand=True, padx=5, pady=5)
        self._lbl_cap_no_file = ttk.Label(self._frm_capitoli_inner,
                                          text="Nessun file aperto.", foreground="gray")
        self._lbl_cap_no_file.pack(anchor="w")

        frm_cap_btns = ttk.Frame(tab_cap); frm_cap_btns.pack(fill="x", padx=10)
        ttk.Button(frm_cap_btns, text="➕ Aggiungi capitolo",
                   command=self._capitoli_aggiungi).pack(side="left")
        ttk.Button(frm_cap_btns, text="📂 Importa da file…",
                   command=self._capitoli_importa).pack(side="left", padx=8)
        ttk.Button(frm_cap_btns, text="💾 Esporta capitoli…",
                   command=self._capitoli_esporta).pack(side="left")

        # === Come verrà applicato ===
        frm_cap_out = ttk.LabelFrame(tab_cap, text="Applicazione")
        frm_cap_out.pack(fill="x", **pad)
        self._lbl_cap_dst = ttk.Label(frm_cap_out, text="", foreground="gray")
        self._lbl_cap_dst.pack(side="left", padx=5, pady=5)

        # === Anteprima comando ===
        self._frm_cap_cmd = ttk.LabelFrame(tab_cap, text="Comando generato")
        self._frm_cap_cmd.pack(fill="x", **pad)
        self._var_cap_cmd = tk.StringVar()
        frm_cap_cmd_inner = ttk.Frame(self._frm_cap_cmd)
        frm_cap_cmd_inner.pack(fill="x", padx=5, pady=4)
        ttk.Entry(frm_cap_cmd_inner, textvariable=self._var_cap_cmd, state="readonly").pack(
            side="left", fill="x", expand=True)
        ttk.Button(frm_cap_cmd_inner, text="📋", width=3,
                   command=lambda: self.clipboard_clear() or self.clipboard_append(self._var_cap_cmd.get())
                   ).pack(side="left", padx=(4,0))
        self._lbl_cap_cmd_warning = ttk.Label(self._frm_cap_cmd, text="", foreground="#cc6600")
        # Non lo pack() subito: appare solo quando c'è un avviso da mostrare

        # === Avvio ===
        frm_cap_run = ttk.Frame(tab_cap); frm_cap_run.pack(fill="x", **pad)
        self._btn_cap_start = ttk.Button(frm_cap_run, text="▶  Applica capitoli", command=self._capitoli_genera)
        self._btn_cap_start.pack(side="left")
        self._btn_cap_stop = ttk.Button(frm_cap_run, text="⏹  Interrompi",
                                        command=self._interrompi, state="disabled")
        self._btn_cap_stop.pack(side="left", padx=8)
        self._lbl_cap_stato = ttk.Label(frm_cap_run, text="")
        self._lbl_cap_stato.pack(side="left", padx=10)

        # === Log ===
        frm_log = ttk.LabelFrame(root, text="Log")
        frm_log.pack(fill="both", expand=True, **pad)
        mono = font.Font(family="Consolas", size=9)
        self._log = scrolledtext.ScrolledText(
            frm_log, wrap="word", font=mono, height=10, state="disabled",
            background="#1e1e1e", foreground="#d4d4d4")
        self._log.pack(fill="both", expand=True, padx=5, pady=5)
        for tag, fg in [("info","#569cd6"),("ok","#4ec9b0"),("error","#f44747"),
                        ("cmd","#555555"),("detail","#808080"),("warning","#cc9944"),
                        ("progress","#dcdcaa"),("summary","#ce9178")]:
            self._log.tag_config(tag, foreground=fg)

    # -----------------------------------------------------------------------
    # Anteprima comando
    # -----------------------------------------------------------------------

    def _bind_traces(self):
        """Aggiorna l'anteprima ogni volta che cambia un'opzione."""
        for var in (self._var_quality, self._var_lookahead, self._var_limit_fps,
                    self._var_fps, self._var_audio, self._var_subs,
                    self._var_ts, self._var_res, self._var_vcodec,
                    self._var_hflip, self._var_vflip, self._var_ar_ratio):
            var.trace_add("write", lambda *_: self._aggiorna_anteprima())
        # Il formato output influenza anche gli avvisi metadati
        self._var_ext.trace_add("write", lambda *_: (
            self._aggiorna_anteprima(),
            self._mostra_metadati(self._cached_probe) if hasattr(self, "_cached_probe") else None
        ))
        self._listbox.bind("<<ListboxSelect>>", self._on_selezione)
        self._var_mux_ext.trace_add("write", lambda *_: self._mux_aggiorna_anteprima())

    def _aggiorna_anteprima(self):
        mode = self._var_mode.get()
        is_audio = mode == "audio"
        if is_audio:
            ext = AUDIO_ONLY_EXT.get(self._var_audio.get(), "mka")
            self._lbl_audio_ext.configure(text=f"→ estensione output: .{ext} (automatica)")
        else:
            ext = self._var_ext.get()
            self._lbl_audio_ext.configure(text="")

        sel = self._listbox.curselection()
        if not sel or not self._all_files:
            self._var_cmd.set("")
            self._mostra_avviso_cmd(None)
            self._lbl_ar_valore.configure(text="")
            return
        src = self._all_files[sel[0]]

        if mode == "fixtag":
            # Remux in-place: sorgente e destinazione coincidono (vedi fix_hvc1_tag,
            # che scrive su un file temporaneo e poi sostituisce l'originale).
            cmd = build_fixtag_cmd(Path(src.name), Path(src.name))
            self._var_cmd.set(" ".join(cmd))
            self._mostra_avviso_cmd(None)
            self._lbl_ar_valore.configure(text="")
            return

        if mode == "fixbitrate":
            # Come l'esecuzione reale (vedi _avvia): calcola PRIMA dichiarato
            # vs reale e, se il file è già a posto, non mostra un comando di
            # correzione che comunque non verrebbe applicato a questo file
            # in un avvio reale (solo i file con scarto vero vengono toccati).
            data = self._ottieni_probe(src)
            video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            if not video:
                self._var_cmd.set("(nessuno stream video nel file selezionato)")
                self._mostra_avviso_cmd(None)
                self._lbl_ar_valore.configure(text="")
                return
            try:
                durata = float(data.get("format", {}).get("duration") or 0)
            except Exception:
                durata = 0.0
            tags = video.get("tags", {})
            dich_raw = video.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng")
            dichiarato = int(dich_raw) if dich_raw and str(dich_raw).isdigit() else None
            reale = self._ottieni_bitrate_video(src, durata)

            if bitrate_scarto_reale(dichiarato, reale) is False:
                self._var_cmd.set(
                    f"(bitrate già corretto: {dichiarato // 1000}kbps — nessuna correzione necessaria)")
                self._mostra_avviso_cmd(None)
                self._lbl_ar_valore.configure(text="")
                return

            if usa_mkvpropedit_per(src):
                # mkvpropedit ricalcola le statistiche DA SOLO e modifica il
                # file sul posto (vedi fix_bitrate_tag): il valore appena
                # calcolato sopra serviva solo per decidere se mostrare
                # questo comando, non gli viene passato.
                cmd = [Path(trova_mkvpropedit()).name, src.name, "--add-track-statistics-tags"]
                self._var_cmd.set(" ".join(cmd))
                self._mostra_avviso_cmd(None)
            else:
                cmd = build_fixbitrate_cmd(Path(src.name), Path(src.name), reale)
                self._var_cmd.set(" ".join(cmd))
                self._mostra_avviso_cmd(
                    None if reale else ["Bitrate video non calcolabile per questo file: il tag verrà solo svuotato."])
            self._lbl_ar_valore.configure(text="")
            return

        dst = src.parent / f"{src.stem}_enc.{ext}"

        res_val = int(self._var_res.get())
        ss  = (self._entry_ss.get() if hasattr(self, "_entry_ss") and self._var_ss_on and self._var_ss_on.get() else None)
        t   = (self._entry_t.get()  if hasattr(self, "_entry_t")  and self._var_t_on  and self._var_t_on.get()  else None)

        # Dati ffprobe DEL FILE mostrato nell'anteprima (src, il primo della
        # selezione), non un generico "ultimo probato": con selezione multipla
        # self._cached_probe si riferiva ancora all'ultimo file ispezionato in
        # selezione singola, disallineato da src (es. anteprima che mostra il
        # comando per "film2.mkv" ma calcola il rapporto 'Originale' su
        # "film1.mkv"). _ottieni_probe fa cache per file, quindi qui non c'è
        # una nuova chiamata ffprobe ad ogni aggiornamento se src è già noto.
        data = self._ottieni_probe(src)

        # Selezione stream: stessa logica di _avvia(), sui dati cached (niente
        # nuova chiamata ffprobe ad ogni aggiornamento dell'anteprima). Prima
        # mancava, quindi l'anteprima mostrava sempre "-map 0" anche con stream
        # deselezionati, pur essendo la conversione reale corretta.
        stream_map = None
        if len(sel) == 1 and self._stream_vars:
            n_str = len(data.get("streams", []))
            stream_map = [i for i in range(n_str) if i < len(self._stream_vars)
                          and self._stream_vars[i].get()]

        opts = {
            "mode":       self._var_mode.get(),
            "output_ext": ext,
            "vcodec":     self._vcodec_id(),
            "quality":    self._var_quality.get(),
            "look_ahead": self._var_lookahead.get(),
            "limit_fps":  self._var_limit_fps.get(),
            "fps_value":  self._var_fps.get(),
            "audio":      self._var_audio.get(),
            "subs":       self._var_subs.get(),
            "timestamp":  self._var_ts.get(),
            "stream_map": stream_map,
            "limite_res": res_val if res_val > 0 else None,
            "hflip": self._var_hflip.get(),
            "vflip": self._var_vflip.get(),
            "ar_mode":  self._var_ar_mode.get(),
            "ar_ratio": self._var_ar_ratio.get(),
            "ss": ss,
            "t":  t,
        }

        # Valore numerico effettivo del rapporto scelto (soprattutto utile con
        # "Originale", che dipende dal file caricato e non è leggibile a colpo
        # d'occhio come i rapporti fissi già scritti nel campo).
        if opts["ar_mode"] != "nessuna":
            video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            valore_ar = risolvi_rapporto_ar(opts["ar_ratio"], video)
            self._lbl_ar_valore.configure(text=f"= {valore_ar:.2f}:1" if valore_ar else "")
        else:
            self._lbl_ar_valore.configure(text="")

        try:
            warnings = []
            cmd  = build_ffmpeg_cmd(Path(src.name), Path(dst.name), opts, data, warnings)
            self._var_cmd.set(" ".join(cmd))
            self._mostra_avviso_cmd(warnings)
        except Exception as e:
            self._var_cmd.set(f"(errore anteprima: {e})")
            self._mostra_avviso_cmd(None)

    def _mostra_avviso_cmd(self, testi):
        if testi:
            self._lbl_cmd_warning.configure(text="\n".join(f"⚠ {t}" for t in testi))
            self._lbl_cmd_warning.pack(fill="x", padx=5, pady=(0, 4))
        else:
            self._lbl_cmd_warning.pack_forget()

    # -----------------------------------------------------------------------
    # Selezione stream (file singolo)
    # -----------------------------------------------------------------------

    def _on_selezione(self, _event=None):
        sel = self._listbox.curselection()
        # In "Fix tag"/"Correggi tag bitrate" tutti gli stream vanno sempre
        # copiati inalterati: niente selezione stream né taglio, per non
        # rischiare di modificare il file.
        if len(sel) == 1 and self._var_mode.get() not in ("fixtag", "fixbitrate"):
            self._mostra_stream(self._all_files[sel[0]])
        else:
            self._nascondi_stream()
        self._aggiorna_anteprima()

    def _mostra_trim(self, dur_sec: float):
        for w in self._frm_trim_inner.winfo_children():
            w.destroy()

        def fmt_hms(sec):
            sec = int(sec)
            return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"

        ttk.Label(self._frm_trim_inner, text=f"Durata: {fmt_hms(dur_sec)}",
                  foreground="gray").pack(side="left", padx=(0,20))

        self._var_ss_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(self._frm_trim_inner, text="Inizio (hh:mm:ss):",
                        variable=self._var_ss_on, command=self._toggle_trim).pack(side="left")
        self._entry_ss = ttk.Entry(self._frm_trim_inner, width=10)
        self._entry_ss.insert(0, "00:00:00")
        self._entry_ss.pack(side="left", padx=(2,16))

        self._var_t_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(self._frm_trim_inner, text="Durata (hh:mm:ss):",
                        variable=self._var_t_on, command=self._toggle_trim).pack(side="left")
        self._entry_t = ttk.Entry(self._frm_trim_inner, width=10)
        self._entry_t.insert(0, "00:00:00")
        self._entry_t.pack(side="left", padx=2)

        self._toggle_trim()
        self._entry_ss.bind("<KeyRelease>", lambda _: self._aggiorna_anteprima())
        self._entry_t.bind( "<KeyRelease>", lambda _: self._aggiorna_anteprima())

        self._frm_trim.pack(fill="x", padx=10, pady=(4,0), before=self._frm_cmd)

    def _ottieni_probe(self, path: Path) -> dict:
        """ffprobe con cache per file (str(path) -> dati), riusata sia da
        _mostra_stream sia da _aggiorna_anteprima. Prima quest'ultima
        riusava sempre self._cached_probe, che riflette SOLO l'ultimo file
        ispezionato in selezione singola: con più file selezionati (batch)
        l'anteprima comando mostrava il primo file della selezione ma i dati
        (e quindi es. il rapporto 'Originale') di un file diverso, quello
        selezionato singolarmente in precedenza."""
        key = str(path)
        if key not in self._probe_cache:
            self._probe_cache[key] = ffprobe_json(path)
        return self._probe_cache[key]

    def _ottieni_bitrate_video(self, path: Path, durata_sec: float):
        """bitrate_video_esatto con cache per file: l'interrogazione ffprobe
        che somma i pacchetti costa qualche secondo, va rifatta al massimo una
        volta per file (non ad ogni ricalcolo dell'anteprima)."""
        key = str(path)
        if key not in self._bitrate_video_cache:
            self._bitrate_video_cache[key] = bitrate_video_esatto(path, durata_sec)
        return self._bitrate_video_cache[key]

    def _mostra_stream(self, path: Path):
        # Pulisce widget precedenti
        for w in self._frm_streams_inner.winfo_children():
            w.destroy()
        self._stream_vars.clear()

        data    = self._ottieni_probe(path)
        self._cached_probe = data
        self._mostra_metadati(data)
        dur_sec = 0.0
        try:
            dur_sec = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            pass
        self._mostra_trim(dur_sec)
        streams = data.get("streams", [])

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        # Calcolato solo come ultima risorsa (né bit_rate genuino né tag
        # BPS/BPS-eng): l'interrogazione ffprobe dedicata costa qualche
        # secondo, quindi si usa il tag così com'è quando c'è (anche se
        # potenzialmente stantio — per verificarlo c'è il pulsante "🔍
        # Verifica bitrate reale", non un ricalcolo automatico ad ogni file).
        bitrate_video = None
        if video and not video_ha_bitrate_noto(video):
            bitrate_video = self._ottieni_bitrate_video(path, dur_sec)
        avviso_sar = rileva_pixel_non_quadrati(video)
        if avviso_sar:
            self._lbl_sar_avviso.configure(text=avviso_sar)
            self._lbl_sar_avviso.pack(fill="x", padx=5, pady=(0, 3))
        else:
            self._lbl_sar_avviso.pack_forget()

        if not streams:
            return

        ttk.Label(self._frm_streams_inner,
                  text="Stream da includere:", foreground="gray").grid(
            row=0, column=0, sticky="w", pady=(0,4))

        btn_frame = ttk.Frame(self._frm_streams_inner)
        btn_frame.grid(row=1, column=0, sticky="w", pady=(0,4))
        if video is not None:
            btn_verifica = ttk.Button(btn_frame, text="🔍 Verifica bitrate reale",
                       command=lambda: self._verifica_bitrate_reale(path))
            btn_verifica.pack(side="left", padx=(0, 12))
            Tooltip(btn_verifica, "Ricalcola il bitrate video sommando i byte reali dei\n"
                                   "pacchetti (non una stima) e lo confronta con quello\n"
                                   "dichiarato nel file. Se non corrisponde (es. tag stantio\n"
                                   "da una ricodifica precedente) propone di correggerlo,\n"
                                   "senza ricodificare nulla.")
        ttk.Button(btn_frame, text="Seleziona tutti", width=15,
                   command=lambda: self._imposta_tutti_stream(True)).pack(side="left", padx=(0,4))
        ttk.Button(btn_frame, text="Deseleziona tutti", width=17,
                   command=lambda: self._imposta_tutti_stream(False)).pack(side="left")

        for i, s in enumerate(streams):
            label = descrivi_stream(i, s, bitrate_video=bitrate_video)
            var = tk.BooleanVar(value=True)
            self._stream_vars.append(var)
            ttk.Checkbutton(self._frm_streams_inner, text=label, variable=var,
                            command=self._aggiorna_anteprima).grid(
                row=i + 2, column=0, sticky="w", padx=10, pady=1)

        # Mostra il frame
        self._frm_streams.pack(fill="x", padx=10, pady=4,
                               before=self._frm_cmd)

    def _imposta_tutti_stream(self, valore: bool):
        """Seleziona/deseleziona in blocco tutte le checkbox stream (utile con
        molte tracce audio/sottotitoli). var.set() non attiva il command= della
        singola checkbox, quindi l'anteprima va aggiornata esplicitamente."""
        for var in self._stream_vars:
            var.set(valore)
        self._aggiorna_anteprima()

    def _verifica_bitrate_reale(self, path: Path):
        """Ricalcola il bitrate REALE del video sommando i byte veri dei
        pacchetti (bitrate_video_esatto, non una stima) e lo confronta con
        quello dichiarato nel file (campo bit_rate genuino, o tag BPS/BPS-eng
        se è tutto ciò che c'è) — utile per un controllo puntuale "a
        campione" quando si ha il dubbio che un file porti ancora un tag
        stantio da una ricodifica precedente (vedi build_ffmpeg_cmd/
        fix_bitrate_tag). Se lo scarto è oltre una piccola tolleranza (il
        bitrate reale di un encode VBR oscilla comunque un po' attorno al
        target dichiarato), propone di correggerlo subito, senza ricodifica."""
        data = self._ottieni_probe(path)
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            messagebox.showinfo("Verifica bitrate", "Nessuno stream video in questo file.")
            return
        try:
            durata = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            durata = 0.0

        tags = video.get("tags", {})
        dichiarato_raw = video.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng")
        dichiarato = int(dichiarato_raw) if dichiarato_raw and str(dichiarato_raw).isdigit() else None

        self.config(cursor="watch")
        self.update_idletasks()
        try:
            # Sempre ricalcolato "a fresco" (non dalla cache): è proprio un
            # controllo esplicito su richiesta, deve riflettere lo stato
            # ATTUALE del file anche se qualcosa lo ha nel frattempo cambiato.
            reale = bitrate_video_esatto(path, durata)
        finally:
            self.config(cursor="")
        if reale is not None:
            self._bitrate_video_cache[str(path)] = reale

        if reale is None:
            messagebox.showwarning("Verifica bitrate",
                "Impossibile calcolare il bitrate reale per questo file.")
            return

        if dichiarato is None:
            messagebox.showinfo("Verifica bitrate",
                f"Nessun bitrate dichiarato nel file da confrontare.\n"
                f"Bitrate reale: {reale // 1000}kbps.")
            return

        if not bitrate_scarto_reale(dichiarato, reale):
            messagebox.showinfo("Verifica bitrate",
                f"Il bitrate dichiarato è corretto.\n\n"
                f"Dichiarato: {dichiarato // 1000}kbps  —  Reale: {reale // 1000}kbps.")
            return

        if not messagebox.askyesno("Verifica bitrate",
                f"Il bitrate dichiarato ({dichiarato // 1000}kbps) NON corrisponde a "
                f"quello reale ({reale // 1000}kbps) — probabilmente un tag rimasto da "
                "una ricodifica precedente.\n\nCorreggerlo subito su questo file "
                "(nessuna ricodifica, solo il tag)?"):
            return

        log_q = queue.Queue()
        stop_ev = threading.Event()
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            ok = fix_bitrate_tag(path, log_q, stop_ev, bitrate_precalcolato=reale)
        finally:
            self.config(cursor="")
        while not log_q.empty():
            tag, testo = log_q.get()
            self._log_write(tag, testo)

        if ok:
            self._probe_cache.pop(str(path), None)
            self._bitrate_video_cache.pop(str(path), None)
            self._mostra_stream(path)
            messagebox.showinfo("Verifica bitrate", "Tag corretto.")
        else:
            messagebox.showerror("Verifica bitrate",
                "Correzione non riuscita: vedi il log per i dettagli.")

    def _mostra_metadati(self, data: dict):
        """Mostra i tag interni del file nel pannello metadati."""
        # Tag che i dispositivi consumer scrivono tipicamente
        TAG_NOTI = {
            "creation_time":                      ("📅 Data registrazione",   False),
            "com.apple.quicktime.creationdate":   ("📅 Data registrazione (Apple, con TZ)", False),
            "date":                               ("📅 Data",                 False),
            "DATE":                               ("📅 Data",                 False),
            "com.apple.quicktime.location.ISO6709":("📍 Posizione GPS (Apple)", False),
            "location":                           ("📍 Posizione GPS",        False),
            "location-eng":                       ("📍 Posizione GPS",        False),
            "make":                               ("📷 Produttore",           False),
            "model":                              ("📷 Modello",              False),
            "software":                           ("🔧 Software",             False),
            "encoder":                            ("🔧 Encoder",              False),
            "title":                              ("📝 Titolo",               False),
            "comment":                            ("📝 Commento",             False),
        }

        # Tag che rischiano di perdersi su MKV
        RISCHIO_MKV = {
            "com.apple.quicktime.creationdate",
            "com.apple.quicktime.location.ISO6709",
            "com.apple.quicktime.location",
        }

        output_ext = self._var_ext.get()

        self._txt_meta.configure(state="normal")
        self._txt_meta.delete("1.0", "end")

        fmt_tags = data.get("format", {}).get("tags", {})

        # Sezione: tag container
        self._txt_meta.insert("end", "── Tag container ──\n", "section")
        if fmt_tags:
            for k, v in fmt_tags.items():
                desc, _ = TAG_NOTI.get(k, (None, False))
                label   = f"  {desc or k:<45}"
                rischio = k in RISCHIO_MKV and output_ext == "mkv"
                self._txt_meta.insert("end", label, "key")
                self._txt_meta.insert("end", v, "warning" if rischio else "val")
                if rischio:
                    self._txt_meta.insert("end", "  ⚠ potrebbe perdersi in MKV", "warning")
                self._txt_meta.insert("end", "\n")
        else:
            self._txt_meta.insert("end", "  (nessun tag)\n", "section")

        # Sezione: tag per stream, un separatore per ciascuno stream che ne ha
        # (invece di un elenco unico "Tag stream": con più tracce dello
        # stesso tipo e stesso tag — es. due sottotitoli con "title" — era
        # anche ambiguo capire quale valore appartenesse a quale traccia, e
        # rischiava collisioni silenziose nel dizionario usato per raggrupparle).
        for i, s in enumerate(data.get("streams", [])):
            tipo = s.get("codec_type", "?")
            lang = s.get("tags", {}).get("language", "")
            tags_propri = {k: v for k, v in s.get("tags", {}).items() if k not in fmt_tags}
            if not tags_propri:
                continue
            icona = TIPO_ICONA.get(tipo, "❓")
            intestazione = f"{icona} Stream #{i} ({tipo}{'/' + lang if lang else ''})"
            self._txt_meta.insert("end", f"\n── {intestazione} ──\n", "section")
            for k, v in tags_propri.items():
                self._txt_meta.insert("end", f"  {k:<45}", "key")
                self._txt_meta.insert("end", v + "\n", "val")

        self._txt_meta.configure(state="disabled")
        self._frm_meta.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

    def _toggle_trim(self):
        self._entry_ss.configure(state="normal" if self._var_ss_on.get() else "disabled")
        self._entry_t.configure( state="normal" if self._var_t_on.get()  else "disabled")
        self._aggiorna_anteprima()

    def _nascondi_stream(self):
        self._frm_meta.grid_remove()
        self._frm_trim.pack_forget()
        self._frm_streams.pack_forget()
        self._lbl_sar_avviso.pack_forget()
        self._stream_vars.clear()
        self._var_ss_on = None
        self._var_t_on  = None

    # -----------------------------------------------------------------------
    # Azioni cartella / file
    # -----------------------------------------------------------------------

    def _scegli_dir(self):
        start = self._var_dir.get().strip() or str(Path.cwd())
        d = filedialog.askdirectory(initialdir=start)
        if d:
            self._var_dir.set(d)
            self._cerca_file()

    def _dir_corrente(self):
        self._var_dir.set(str(Path.cwd()))
        self._cerca_file()

    def _vcodec_id(self) -> str:
        """Id dell'encoder selezionato ('hevc_amf', 'libx265', 'copy', …)."""
        return self._vcodec_id_by_label.get(self._var_vcodec.get(), "copy")

    def _popola_vcodec(self, funzionanti):
        """Ricostruisce le voci del menu codec + la mappa etichetta→id.
        funzionanti=None → probe non ancora fatto (voci marcate 'verifica in corso');
        lista → gli encoder che superano il probe vanno in cima, gli altri marcati."""
        self._vcodec_id_by_label = {}
        values = []
        if funzionanti is None:
            ordine = list(self._enc_compilati)
            suffix = lambda enc: "  (verifica in corso…)"
        else:
            working = [e for e in self._enc_compilati if e in funzionanti]
            broken  = [e for e in self._enc_compilati if e not in funzionanti]
            ordine  = working + broken
            suffix  = lambda enc: "  ✓ rilevato" if enc in funzionanti else "  ⚠ non rilevato"
        for enc in ordine:
            lbl = VIDEO_ENCODERS[enc][0] + suffix(enc)
            self._vcodec_id_by_label[lbl] = enc
            values.append(lbl)
        self._vcodec_id_by_label[VCODEC_COPY_LABEL] = "copy"
        values.append(VCODEC_COPY_LABEL)
        self._combo_vcodec.configure(values=values)
        return values

    def _probe_encoder(self):
        """Thread di background: verifica quali encoder funzionano davvero e
        registra l'esito nel log (utile per capire perché un encoder non appare)."""
        import shutil
        diag = [f"ffmpeg: {shutil.which('ffmpeg') or 'NON trovato nel PATH!'}",
                "Encoder compilati: " + ", ".join(self._enc_compilati)]
        funzionanti = []
        for enc in self._enc_compilati:
            ok, det = encoder_funziona(enc)
            diag.append(f"  {enc:<12} {'OK ' if ok else 'no '} {det}")
            if ok:
                funzionanti.append(enc)
        self._log_q.put(("detail", "── Rilevamento encoder ──\n" + "\n".join(diag)))
        self._log_q.put(("encoders", funzionanti))

    def _applica_rilevamento(self, funzionanti):
        """Callback nel thread GUI al termine del probe."""
        prev_id = self._vcodec_id()
        self._popola_vcodec(funzionanti)

        def label_di(enc_id):
            return next(l for l, e in self._vcodec_id_by_label.items() if e == enc_id)

        if funzionanti:
            default_lbl = label_di(funzionanti[0])
        else:
            default_lbl = VCODEC_COPY_LABEL
        # mantieni la scelta dell'utente se già valida, altrimenti usa il consigliato
        if prev_id in funzionanti:
            default_lbl = label_di(prev_id)
        self._var_vcodec.set(default_lbl)
        self._toggle_vcodec()

    def _toggle_vcodec(self):
        encode = self._vcodec_id() != "copy"
        state  = "normal" if encode else "disabled"
        self._spin_quality.configure(state=state)
        # look-ahead non ha effetto su libx265/libsvtav1 (interno) né su VideoToolbox
        la_state = ("normal" if encode and self._vcodec_id()
                    not in ("libx265", "libsvtav1", "hevc_videotoolbox") else "disabled")
        self._chk_lookahead.configure(state=la_state)
        self._aggiorna_anteprima()

    def _toggle_fps(self):
        self._spin_fps.configure(state="normal" if self._var_limit_fps.get() else "disabled")

    def _toggle_ar(self):
        attivo = self._var_ar_mode.get() != "nessuna"
        self._combo_ar.configure(state="normal" if attivo else "disabled")
        # "Solo correggi DAR" promette di non ricodificare: seleziona da sola
        # "Copia stream" (altrimenti, lasciando un encoder normale, l'utente
        # deve ricordarsi di cambiarlo a mano) e blocca il menu così non lo si
        # può cambiare per sbaglio finché questa modalità resta attiva.
        if self._var_ar_mode.get() == "dar":
            self._var_vcodec.set(VCODEC_COPY_LABEL)
            self._toggle_vcodec()
            self._combo_vcodec.configure(state="disabled")
        else:
            self._combo_vcodec.configure(state="readonly")
        self._aggiorna_anteprima()

    def _mostra_anteprima_filtro(self):
        """Apre una finestra con un fotogramma del file selezionato, con gli
        stessi filtri (risoluzione/aspect ratio/flip) della conversione
        reale — per vedere l'effetto prima di lanciare tutta la codifica."""
        sel = self._listbox.curselection()
        if len(sel) != 1:
            messagebox.showwarning("Attenzione", "Seleziona un singolo file per l'anteprima.")
            return
        src = self._all_files[sel[0]]

        data = ffprobe_json(src)
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            messagebox.showwarning("Attenzione", "Nessuno stream video nel file selezionato.")
            return
        try:
            durata = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            durata = 0.0

        win = tk.Toplevel(self)
        win.title(f"Anteprima filtri — {src.name}")
        win.geometry("1000x700")
        win.minsize(320, 280)

        if self._var_ar_mode.get() == "dar":
            ttk.Label(win, foreground="#cc6600",
                      text="⚠ 'Solo DAR' non modifica i pixel: qui sotto è simulato lo "
                           "stiramento che un player conforme applicherebbe in riproduzione, "
                           "solo per l'anteprima — il file generato avrà i pixel invariati."
                      ).pack(fill="x", padx=10, pady=(10, 0))

        # La barra dei controlli va impacchettata PRIMA (ancorata in basso con
        # side="bottom"), altrimenti con un'immagine grande la Label (piena
        # priorità con expand=True) le ruba tutto lo spazio verticale e la
        # spinge fuori dalla finestra.
        frm_ctrl = ttk.Frame(win)
        frm_ctrl.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        lbl_tempo = ttk.Label(frm_ctrl, text="00:00:00", width=8)
        lbl_tempo.pack(side="right", padx=(8, 0))

        lbl_img = ttk.Label(win, anchor="center")
        lbl_img.pack(fill="both", expand=True, padx=10, pady=10)

        def fmt_hms(sec):
            sec = max(0, int(sec))
            return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"

        var_pos = tk.DoubleVar(value=durata / 2 if durata > 0 else 0)
        tmp_png = Path(tempfile.gettempdir()) / f"_anteprima_filtro_{os.getpid()}.png"

        def genera(_evt=None):
            ss = fmt_hms(var_pos.get())
            lbl_tempo.config(text=ss)
            lbl_img.configure(text="Generazione anteprima…", image="")
            win.update_idletasks()

            res_val = int(self._var_res.get())
            opts_preview = {
                "limite_res": res_val if res_val > 0 else None,
                "ar_mode":    self._var_ar_mode.get(),
                "ar_ratio":   self._var_ar_ratio.get(),
                "limit_fps":  False,
                "fps_value":  0,
                "hflip":      self._var_hflip.get(),
                "vflip":      self._var_vflip.get(),
            }
            ok, msg = estrai_frame_anteprima(src, ss, opts_preview, video, tmp_png)
            if not win.winfo_exists():
                return  # l'utente ha chiuso la finestra mentre ffmpeg girava
            if not ok:
                lbl_img.configure(text=f"Errore nella generazione: {msg}", image="")
                return
            img = tk.PhotoImage(file=str(tmp_png))
            win._anteprima_img = img  # riferimento tenuto vivo, altrimenti il garbage collector la cancella
            lbl_img.configure(image=img, text="")

        if durata > 0:
            scale = ttk.Scale(frm_ctrl, from_=0, to=durata, variable=var_pos, orient="horizontal")
            scale.pack(side="left", fill="x", expand=True)
            scale.bind("<ButtonRelease-1>", genera)
        else:
            ttk.Label(frm_ctrl, text="Durata sconosciuta: mostro il primo fotogramma.",
                      foreground="gray").pack(side="left")

        ttk.Button(frm_ctrl, text="🔄", width=3, command=genera).pack(side="left", padx=(8, 0))

        def on_close():
            tmp_png.unlink(missing_ok=True)
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

        genera()

    def _toggle_mode(self):
        """'Solo audio': nasconde i controlli video (inutilizzabili senza un flusso
        video di output) e i sottotitoli (un contenitore audio non li supporta).
        'Fix tag'/'Correggi tag bitrate': nascondono anche audio/timestamp, perché
        in quelle modalità tutto viene copiato inalterato — non c'è nulla da
        configurare."""
        mode = self._var_mode.get()
        for w in (self._frm_video_specific, self._frm_audio_row,
                  self._frm_subs, self._frm_timestamp_row):
            w.pack_forget()

        if mode == "video":
            self._frm_video_specific.pack(fill="x")
            self._frm_audio_row.pack(fill="x", padx=5, pady=3)
            self._frm_subs.pack(fill="x", padx=5, pady=3)
            self._frm_timestamp_row.pack(fill="x", padx=5, pady=3)
        elif mode == "audio":
            self._frm_audio_row.pack(fill="x", padx=5, pady=3)
            self._frm_timestamp_row.pack(fill="x", padx=5, pady=3)
        # "fixtag"/"fixbitrate": nessun pannello aggiuntivo

        # Ricerca file: in modalità audio include anche i formati audio puri
        # (mp3/aac/dts/mka/…); in modalità fixtag solo mp4 (unico contenitore
        # dove il tag hvc1 ha senso); in modalità fixbitrate solo mkv (il tag
        # BPS stantio è un problema specifico di Matroska — vedi
        # build_ffmpeg_cmd/bitrate_video_esatto: mp4 usa un campo bit_rate
        # vero, ricalcolato dal muxer, non un tag copiabile alla cieca).
        d = self._var_dir.get().strip()
        if d and Path(d).is_dir():
            # preserva_selezione: cambiare modalità qui rifiltra le
            # estensioni mostrate, ma NON deve silenziosamente sostituire una
            # selezione mirata con "seleziona tutti" (bug reale segnalato:
            # con un solo file selezionato, passare a "Correggi tag bitrate"
            # faceva ripartire l'operazione su TUTTI i file della cartella).
            self._cerca_file(preserva_selezione=True)
        self._aggiorna_anteprima()

    def _cerca_file(self, preserva_selezione: bool = False):
        d = self._var_dir.get().strip()
        if not d or not Path(d).is_dir():
            messagebox.showwarning("Attenzione", "Seleziona prima una cartella valida.")
            return
        mode = self._var_mode.get()
        if mode == "audio":
            ests = ESTENSIONI_INPUT + ESTENSIONI_INPUT_AUDIO
        elif mode == "fixtag":
            ests = ["*.mp4"]
        elif mode == "fixbitrate":
            ests = ["*.mkv"]
        else:
            ests = ESTENSIONI_INPUT
        found = sorted({f for p in ests for f in Path(d).glob(p)})
        self._imposta_file_trovati(found, aggiorna_dir=False, preserva_selezione=preserva_selezione)

    def _imposta_file_trovati(self, paths: list, aggiorna_dir: bool = True,
                               preserva_selezione: bool = False):
        """Popola la lista 'File trovati' con un elenco esplicito di file
        (usato sia dalla ricerca per cartella sia dal drag & drop).
        preserva_selezione=True mantiene selezionati (per nome file) gli
        stessi file già selezionati prima della chiamata, se sono ancora
        presenti nel nuovo elenco — invece di ripiegare su "seleziona tutti".
        Usato quando si cambia modalità (_toggle_mode), che rifiltra le
        estensioni mostrate ma non deve alterare una selezione mirata
        dell'utente. Ripiega comunque su "seleziona tutti" se nessuno dei
        file prima selezionati è ancora presente (o se non richiesto)."""
        nomi_prima = None
        if preserva_selezione:
            nomi_prima = {self._all_files[i].name for i in self._listbox.curselection()
                          if i < len(self._all_files)}

        self._listbox.delete(0, "end")
        self._all_files = paths
        for f in paths:
            self._listbox.insert("end", f.name)

        indici_da_preservare = [i for i, f in enumerate(paths) if nomi_prima and f.name in nomi_prima]
        if indici_da_preservare:
            for i in indici_da_preservare:
                self._listbox.select_set(i)
        else:
            self._listbox.select_set(0, "end")

        self._lbl_files.config(text=f"{len(paths)} file trovati")
        self._nascondi_stream()
        if aggiorna_dir and paths:
            self._var_dir.set(str(paths[0].parent))

    def _on_drop_file_list(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data) if Path(p).is_file()]
        if paths:
            self._imposta_file_trovati(sorted(paths))

    # -----------------------------------------------------------------------
    # Avvio / stop
    # -----------------------------------------------------------------------

    def _avvia(self):
        if self._active_op:
            messagebox.showwarning("Attenzione", "È già in corso un'altra operazione.")
            return
        d = self._var_dir.get().strip()
        if not d or not Path(d).is_dir():
            messagebox.showwarning("Attenzione", "Seleziona una cartella valida.")
            return
        sel_idx = self._listbox.curselection()
        if not sel_idx:
            messagebox.showwarning("Attenzione", "Nessun file selezionato.")
            return

        files = [self._all_files[i] for i in sel_idx]
        mode  = self._var_mode.get()

        if mode == "fixtag":
            if not messagebox.askyesno(
                "Conferma",
                f"Il tag hvc1 verrà applicato a {len(files)} file, sostituendo "
                "l'originale sullo stesso percorso (nessuna ricodifica: stream, "
                "metadati e data del file restano invariati). Continuare?"):
                return

            self._log_clear()
            self._log_write("info", f"Inizio fix tag QuickTime: {len(files)} file\n")

            self._stop_ev.clear()
            self._active_op = "codifica"
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="normal")
            self._lbl_stato.config(text="In corso…")

            threading.Thread(
                target=fixtag_worker,
                args=(files, self._log_q, self._stop_ev),
                daemon=True
            ).start()
            return

        if mode == "fixbitrate":
            # Calcola PRIMA (dichiarato vs reale, come "🔍 Verifica bitrate
            # reale") e mostra un riepilogo, invece di un generico "verrà
            # corretto, continuare?" alla cieca: solo i file con uno scarto
            # reale vengono poi effettivamente toccati.
            self._lbl_stato.config(text="Calcolo bitrate reale…")
            self.config(cursor="watch")
            righe, da_correggere = [], []
            for n, f in enumerate(files, 1):
                self._lbl_stato.config(text=f"Calcolo bitrate reale… ({n}/{len(files)})")
                self.update()
                data = ffprobe_json(f)
                video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
                if not video:
                    righe.append(f"?  {f.name} — nessuno stream video")
                    continue
                try:
                    durata = float(data.get("format", {}).get("duration") or 0)
                except Exception:
                    durata = 0.0
                tags = video.get("tags", {})
                dich_raw = video.get("bit_rate") or tags.get("BPS") or tags.get("BPS-eng")
                dichiarato = int(dich_raw) if dich_raw and str(dich_raw).isdigit() else None
                reale = bitrate_video_esatto(f, durata)
                if reale is None:
                    righe.append(f"?  {f.name} — bitrate reale non calcolabile")
                elif dichiarato is None:
                    righe.append(f"?  {f.name} — nessun valore dichiarato (reale: {reale // 1000}kbps)")
                elif not bitrate_scarto_reale(dichiarato, reale):
                    righe.append(f"✓  {f.name} — già corretto ({dichiarato // 1000}kbps)")
                else:
                    righe.append(f"⚠  {f.name} — dichiarato {dichiarato // 1000}kbps, "
                                  f"reale {reale // 1000}kbps")
                    da_correggere.append((f, reale))
            self.config(cursor="")
            self._lbl_stato.config(text="")

            testo = "\n".join(righe)
            if not da_correggere:
                messagebox.showinfo("Correggi tag bitrate",
                    f"Controllati {len(files)} file: nessuna correzione necessaria.\n\n{testo}")
                return

            nota_esclusi = (f"\n\n({len(files) - len(da_correggere)} file già corretti "
                             "non verranno toccati.)" if len(da_correggere) < len(files) else "")
            if not messagebox.askyesno("Correggi tag bitrate",
                    f"{testo}\n\nCorreggere il tag su {len(da_correggere)} file "
                    f"(nessuna ricodifica, solo il tag)?{nota_esclusi}"):
                return

            self._log_clear()
            self._log_write("info", f"Inizio correzione tag bitrate: {len(da_correggere)} file\n")

            self._stop_ev.clear()
            self._active_op = "codifica"
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="normal")
            self._lbl_stato.config(text="In corso…")

            threading.Thread(
                target=fixbitrate_worker,
                args=(da_correggere, self._log_q, self._stop_ev),
                daemon=True
            ).start()
            return

        # Stream map: solo se selezione singola e stream_vars popolati
        stream_map = None
        if len(sel_idx) == 1 and self._stream_vars:
            data    = ffprobe_json(files[0])
            n_str   = len(data.get("streams", []))
            stream_map = [i for i in range(n_str) if i < len(self._stream_vars)
                          and self._stream_vars[i].get()]
            if not stream_map:
                messagebox.showwarning("Attenzione", "Seleziona almeno uno stream.")
                return

        res_val = int(self._var_res.get())
        output_ext = (AUDIO_ONLY_EXT.get(self._var_audio.get(), "mka") if mode == "audio"
                     else self._var_ext.get())
        opts = {
            "mode":       mode,
            "output_ext": output_ext,
            "vcodec":     self._vcodec_id(),
            "quality":    self._var_quality.get(),
            "look_ahead": self._var_lookahead.get(),
            "hflip":      self._var_hflip.get(),
            "vflip":      self._var_vflip.get(),
            "ar_mode":    self._var_ar_mode.get(),
            "ar_ratio":   self._var_ar_ratio.get(),
            "limit_fps":  self._var_limit_fps.get(),
            "fps_value":  self._var_fps.get(),
            "audio":      self._var_audio.get(),
            "subs":       self._var_subs.get(),
            "timestamp":  self._var_ts.get(),
            "ss": self._entry_ss.get() if (self._var_ss_on and self._var_ss_on.get()) else None,
            "t":  self._entry_t.get()  if (self._var_t_on  and self._var_t_on.get())  else None,
            "stream_map": stream_map,
            "limite_res": res_val if res_val > 0 else None,
        }

        # Come nei tab Mux/Estrazione: avvisa se la destinazione esiste già
        # (es. da un tentativo precedente), invece di sovrascriverla alla
        # cieca con "-y" — altrimenti, se la nuova conversione fallisce, il
        # vecchio file resta lì e sembra il risultato di quella nuova, mentre
        # in realtà non è mai stato ricreato.
        esistenti = [f"{f.stem}_enc.{output_ext}" for f in files
                     if (f.parent / f"{f.stem}_enc.{output_ext}").exists()]
        if esistenti:
            elenco = ", ".join(esistenti[:3]) + ("…" if len(esistenti) > 3 else "")
            if not messagebox.askyesno("Conferma",
                    f"{len(esistenti)} file di destinazione esistono già ({elenco}). Sovrascriverli?"):
                return

        self._log_clear()
        self._log_write("info", f"Inizio conversione: {len(files)} file\n")

        self._stop_ev.clear()
        self._active_op = "codifica"
        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._lbl_stato.config(text="In corso…")

        threading.Thread(
            target=worker,
            args=(files, opts, self._log_q, self._stop_ev),
            daemon=True
        ).start()

    def _interrompi(self):
        self._stop_ev.set()
        if self._active_op == "codifica":
            self._lbl_stato.config(text="Interruzione…")
        elif self._active_op == "mux":
            self._lbl_mux_stato.config(text="Interruzione…")
        elif self._active_op == "capitoli":
            self._lbl_cap_stato.config(text="Interruzione…")

    def _on_tab_changed(self, _event=None):
        """Passando al tab Mux o Capitoli, carica automaticamente il file
        attualmente selezionato in Codifica (se è uno solo) — comodità per
        non dover premere "Usa file da Codifica" ad ogni cambio tab. Non fa
        nulla se in Codifica non c'è una selezione singola, o se il file è
        già quello caricato (evita di riprobarlo inutilmente ad ogni switch
        avanti/indietro tra i tab)."""
        sel = self._listbox.curselection()
        if len(sel) != 1:
            return
        file_codifica = self._all_files[sel[0]]

        tab_attivo = self._nb.select()
        if tab_attivo == str(self._tab_mux):
            if self._mux_src != file_codifica:
                self._mux_carica(file_codifica)
        elif tab_attivo == str(self._tab_cap):
            if self._capitoli_src != file_codifica:
                self._capitoli_carica(file_codifica)

    # -----------------------------------------------------------------------
    # Tab Mux
    # -----------------------------------------------------------------------

    def _mux_scegli_file(self):
        path = filedialog.askopenfilename(
            title="Apri file da modificare",
            filetypes=[("Video/Audio", " ".join(ESTENSIONI_INPUT + ESTENSIONI_INPUT_AUDIO)),
                       ("Tutti i file", "*.*")])
        if path:
            self._mux_carica(Path(path))

    def _mux_usa_da_codifica(self):
        """Carica nel tab Mux il file attualmente selezionato nella lista del
        tab Codifica, senza doverlo ribrowsare da capo."""
        sel = self._listbox.curselection()
        if len(sel) != 1:
            messagebox.showwarning("Attenzione",
                "Seleziona esattamente un file nella lista 'File trovati' del tab Codifica.")
            return
        self._mux_carica(self._all_files[sel[0]])

    def _mux_carica(self, path: Path):
        self._mux_src = path
        self._var_mux_src.set(str(path))
        self._mux_extra_tracks = []
        self._mux_extra_probes = {}
        self._mux_render_extra_tracks()

        data = ffprobe_json(path)
        self._mux_cached_probe = data
        streams = data.get("streams", [])

        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        # Come in _mostra_stream: calcolo esatto solo come ultima risorsa.
        bitrate_video = None
        if video and not video_ha_bitrate_noto(video):
            try:
                dur_sec = float(data.get("format", {}).get("duration") or 0)
            except Exception:
                dur_sec = 0.0
            bitrate_video = self._ottieni_bitrate_video(path, dur_sec)

        for w in self._frm_mux_tracce_inner.winfo_children():
            w.destroy()
        self._mux_stream_vars.clear()
        self._mux_extract_vars.clear()
        self._mux_title_vars.clear()

        if not streams:
            ttk.Label(self._frm_mux_tracce_inner, text="Nessuno stream rilevato.",
                      foreground="gray").pack(anchor="w")
            self._mux_aggiorna_anteprima()
            return

        header = ttk.Frame(self._frm_mux_tracce_inner)
        header.pack(fill="x", pady=(0, 4))
        btns = ttk.Frame(header)
        btns.pack(side="left")
        ttk.Button(btns, text="Seleziona tutti", width=15,
                   command=lambda: self._mux_imposta_tutti_stream(True)).pack(side="left", padx=(0, 4))
        ttk.Button(btns, text="Deseleziona tutti", width=17,
                   command=lambda: self._mux_imposta_tutti_stream(False)).pack(side="left")

        grid = ttk.Frame(self._frm_mux_tracce_inner)
        grid.pack(fill="x")
        grid.columnconfigure(3, weight=1)  # solo il Titolo si allarga con la finestra
        ttk.Label(grid, text="Mantieni", foreground="gray").grid(row=0, column=0, padx=(10, 4))
        ttk.Label(grid, text="Estrai", foreground="gray").grid(row=0, column=1, padx=4)
        ttk.Label(grid, text="", foreground="gray").grid(row=0, column=2)
        ttk.Label(grid, text="Titolo traccia", foreground="gray").grid(row=0, column=3, padx=4, sticky="w")
        for i, s in enumerate(streams):
            # Le tracce non audio/video/sottotitoli (es. "data" residue di
            # certi muxer mp4) non sono accettate da nessun contenitore via
            # semplice -map/-c copy: partono deselezionate (build_mux_cmd le
            # esclude comunque anche se rispuntate, come rete di sicurezza).
            keep_var = tk.BooleanVar(value=s.get("codec_type") in ("video", "audio", "subtitle"))
            extract_var = tk.BooleanVar(value=False)
            tags = s.get("tags", {})
            title_var = tk.StringVar(value=tags.get("title") or tags.get("name") or "")
            self._mux_stream_vars.append(keep_var)
            self._mux_extract_vars.append(extract_var)
            self._mux_title_vars.append(title_var)
            row = i + 1
            ttk.Checkbutton(grid, variable=keep_var,
                            command=self._mux_aggiorna_anteprima).grid(
                row=row, column=0, padx=(10, 4), pady=1)
            ttk.Checkbutton(grid, variable=extract_var).grid(
                row=row, column=1, padx=4, pady=1)
            ttk.Label(grid, text=descrivi_stream(i, s, includi_titolo=False, bitrate_video=bitrate_video)).grid(
                row=row, column=2, sticky="w", padx=(4, 10), pady=1)
            e_title = ttk.Entry(grid, textvariable=title_var, width=48)
            e_title.grid(row=row, column=3, padx=4, pady=1, sticky="ew")
            e_title.bind("<KeyRelease>", lambda _e: self._mux_aggiorna_anteprima())

        ttk.Button(self._frm_mux_tracce_inner, text="⬇  Estrai selezionate",
                   command=self._mux_estrai).pack(anchor="w", pady=(6, 0))

        self._mux_aggiorna_anteprima()

    def _mux_imposta_tutti_stream(self, valore: bool):
        for var in self._mux_stream_vars:
            var.set(valore)
        self._mux_aggiorna_anteprima()

    def _mux_aggiungi_file(self):
        if not self._mux_src:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return
        paths = filedialog.askopenfilenames(
            title="Aggiungi tracce esterne",
            filetypes=[("Sottotitoli", "*.srt *.ass *.ssa *.vtt"),
                       ("Audio", "*.aac *.ac3 *.mp3 *.mka *.wav *.dts *.m4a"),
                       ("Tutti i file", "*.*")])
        if paths:
            self._mux_aggiungi_tracce([Path(p) for p in paths])

    def _mux_aggiungi_tracce(self, paths: list):
        """Aggiunge file esterni come nuove tracce (usato sia dal pulsante
        'Aggiungi file…' sia dal drag & drop). Titolo/predefinita/forzata
        partono vuoti; la lingua viene pre-compilata se riconosciuta dal nome
        del file (es. 'film.eng.srt', coerente col nome che genera l'Estrai
        del tab Mux) — comunque modificabile a mano."""
        già_presenti = {t["path"] for t in self._mux_extra_tracks}
        for pth in paths:
            if pth in già_presenti:
                continue
            self._mux_extra_probes[str(pth)] = ffprobe_json(pth)
            self._mux_extra_tracks.append({
                "path": pth,
                "lang": tk.StringVar(value=rileva_lingua_da_nome(pth)),
                "title": tk.StringVar(value=""),
                "default": tk.BooleanVar(value=False),
                "forced": tk.BooleanVar(value=False),
            })
        self._mux_render_extra_tracks()
        self._mux_aggiorna_anteprima()

    def _mux_render_extra_tracks(self):
        """Ricostruisce l'elenco delle tracce esterne aggiunte, una riga per
        traccia con campi lingua/titolo e le spunte predefinita/forzata."""
        for w in self._frm_mux_extra_inner.winfo_children():
            w.destroy()

        if not self._mux_extra_tracks:
            ttk.Label(self._frm_mux_extra_inner, text="Nessuna traccia esterna aggiunta.",
                      foreground="gray").pack(anchor="w")
            return

        grid = ttk.Frame(self._frm_mux_extra_inner)
        grid.pack(fill="x")
        grid.columnconfigure(2, weight=1)  # solo il Titolo si allarga con la finestra
        for col, testo in enumerate(("File", "Lingua", "Titolo traccia", "Pred.", "Forzata", "")):
            ttk.Label(grid, text=testo, foreground="gray").grid(row=0, column=col, padx=4, sticky="w")

        for row, t in enumerate(self._mux_extra_tracks, start=1):
            # Entry in sola lettura invece di una Label: il testo resta
            # selezionabile/copiabile (es. per incollare il nome nel campo
            # Titolo), ma non modificabile.
            e_nome = ttk.Entry(grid, width=56)
            e_nome.insert(0, t["path"].name)
            e_nome.configure(state="readonly")
            e_nome.grid(row=row, column=0, sticky="w", padx=4, pady=1)

            e_lang = ttk.Entry(grid, textvariable=t["lang"], width=6)
            e_lang.grid(row=row, column=1, padx=4, pady=1)
            e_lang.bind("<KeyRelease>", lambda _e: self._mux_aggiorna_anteprima())

            e_title = ttk.Entry(grid, textvariable=t["title"], width=24)
            e_title.grid(row=row, column=2, padx=4, pady=1, sticky="ew")
            e_title.bind("<KeyRelease>", lambda _e: self._mux_aggiorna_anteprima())

            ttk.Checkbutton(grid, variable=t["default"],
                            command=self._mux_aggiorna_anteprima).grid(row=row, column=3, padx=8)
            ttk.Checkbutton(grid, variable=t["forced"],
                            command=self._mux_aggiorna_anteprima).grid(row=row, column=4, padx=8)
            ttk.Button(grid, text="✕", width=3,
                       command=lambda i=row - 1: self._mux_rimuovi_traccia(i)).grid(
                row=row, column=5, padx=4)

    def _mux_rimuovi_traccia(self, i: int):
        rimosso = self._mux_extra_tracks.pop(i)
        self._mux_extra_probes.pop(str(rimosso["path"]), None)
        self._mux_render_extra_tracks()
        self._mux_aggiorna_anteprima()

    def _mux_extra_tracks_plain(self) -> list:
        """Converte self._mux_extra_tracks (con le tk.Variable) in dizionari
        semplici, per passarli a build_mux_cmd (funzione pura, senza Tkinter)."""
        return [{"path": t["path"], "lang": t["lang"].get().strip(),
                 "title": t["title"].get().strip(),
                 "default": t["default"].get(), "forced": t["forced"].get()}
                for t in self._mux_extra_tracks]

    def _mux_stream_titles(self) -> dict:
        """Titoli editati per le tracce mantenute dal sorgente (indice
        sorgente -> testo), da passare a build_mux_cmd."""
        return {i: v.get().strip() for i, v in enumerate(self._mux_title_vars) if v.get().strip()}

    def _on_drop_mux_extra(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data) if Path(p).is_file()]
        if paths:
            self._mux_aggiungi_tracce(paths)

    def _on_drop_mux_src(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data) if Path(p).is_file()]
        if paths:
            self._mux_carica(paths[0])

    def _mux_aggiorna_anteprima(self):
        if not self._mux_src:
            self._var_mux_cmd.set("")
            self._lbl_mux_dst.configure(text="")
            self._mux_mostra_avviso(None)
            return

        ext = self._var_mux_ext.get()
        dst = self._mux_src.parent / f"{self._mux_src.stem}_mux.{ext}"
        self._lbl_mux_dst.configure(text=f"→ {dst.name}")

        stream_map = [i for i, v in enumerate(self._mux_stream_vars) if v.get()]

        try:
            warnings = []
            cmd = build_mux_cmd(self._mux_src, self._mux_cached_probe, stream_map,
                                self._mux_extra_tracks_plain(), self._mux_extra_probes,
                                ext, dst, warnings, self._mux_stream_titles())
            self._var_mux_cmd.set(" ".join(cmd))
            self._mux_mostra_avviso(warnings)
        except Exception as e:
            self._var_mux_cmd.set(f"(errore anteprima: {e})")
            self._mux_mostra_avviso(None)

    def _mux_mostra_avviso(self, testi):
        if testi:
            self._lbl_mux_cmd_warning.configure(text="\n".join(f"⚠ {t}" for t in testi))
            self._lbl_mux_cmd_warning.pack(fill="x", padx=5, pady=(0, 4))
        else:
            self._lbl_mux_cmd_warning.pack_forget()

    def _mux_genera(self):
        if self._active_op:
            messagebox.showwarning("Attenzione", "È già in corso un'altra operazione.")
            return
        if not self._mux_src:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return

        stream_map = [i for i, v in enumerate(self._mux_stream_vars) if v.get()]
        if not stream_map:
            messagebox.showwarning("Attenzione", "Seleziona almeno uno stream da mantenere.")
            return

        ext = self._var_mux_ext.get()
        dst = self._mux_src.parent / f"{self._mux_src.stem}_mux.{ext}"

        if dst.exists():
            if not messagebox.askyesno("Conferma", f"Il file {dst.name} esiste già. Sovrascriverlo?"):
                return

        warnings = []
        cmd = build_mux_cmd(self._mux_src, self._mux_cached_probe, stream_map,
                            self._mux_extra_tracks_plain(), self._mux_extra_probes,
                            ext, dst, warnings, self._mux_stream_titles())

        self._log_clear()
        self._log_write("info", f"Inizio mux: {self._mux_src.name}\n")
        for w in warnings:
            self._log_write("warning", f"  ⚠ {w}")

        self._stop_ev.clear()
        self._active_op = "mux"
        self._btn_mux_start.configure(state="disabled")
        self._btn_mux_stop.configure(state="normal")
        self._lbl_mux_stato.config(text="In corso…")

        threading.Thread(
            target=mux_worker,
            args=(self._mux_src, cmd, dst, self._log_q, self._stop_ev),
            daemon=True
        ).start()

    def _mux_estrai(self):
        if self._active_op:
            messagebox.showwarning("Attenzione", "È già in corso un'altra operazione.")
            return
        if not self._mux_src:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return

        indici = [i for i, v in enumerate(self._mux_extract_vars) if v.get()]
        if not indici:
            messagebox.showwarning("Attenzione", "Seleziona almeno una traccia da estrarre.")
            return

        streams = self._mux_cached_probe.get("streams", [])
        estrazioni = []
        for idx in indici:
            s = streams[idx] if idx < len(streams) else {}
            tags = s.get("tags", {})
            ext = estensione_nativa(s.get("codec_type", ""), s.get("codec_name", ""))
            lang = tags.get("language", "")
            # Come descrivi_stream: "title" (mkv) o "name" (mp4) — utile per
            # distinguere a colpo d'occhio più tracce della stessa lingua
            # (es. "SDH", "Forced", "Non udenti").
            titolo = sanitizza_nome_file(tags.get("title") or tags.get("name") or "")
            suffisso_lang   = f".{lang}" if lang else ""
            suffisso_titolo = f".{titolo}" if titolo else ""
            dst = self._mux_src.parent / f"{self._mux_src.stem}_track{idx}{suffisso_lang}{suffisso_titolo}.{ext}"
            estrazioni.append((build_extract_cmd(self._mux_src, idx, dst), dst))

        esistenti = [dst.name for _, dst in estrazioni if dst.exists()]
        if esistenti:
            elenco = ", ".join(esistenti[:3]) + ("…" if len(esistenti) > 3 else "")
            if not messagebox.askyesno("Conferma",
                    f"{len(esistenti)} file di destinazione esistono già ({elenco}). Sovrascriverli?"):
                return

        self._log_clear()
        self._log_write("info", f"Inizio estrazione: {len(estrazioni)} traccia/e\n")

        self._stop_ev.clear()
        self._active_op = "mux"
        self._btn_mux_start.configure(state="disabled")
        self._btn_mux_stop.configure(state="normal")
        self._lbl_mux_stato.config(text="In corso…")

        threading.Thread(
            target=extract_worker,
            args=(estrazioni, self._log_q, self._stop_ev),
            daemon=True
        ).start()

    # -----------------------------------------------------------------------
    # Tab Capitoli
    # -----------------------------------------------------------------------

    def _capitoli_scegli_file(self):
        path = filedialog.askopenfilename(
            title="Apri file da modificare",
            filetypes=[("Video", " ".join(ESTENSIONI_INPUT)),
                       ("Tutti i file", "*.*")])
        if path:
            self._capitoli_carica(Path(path))

    def _capitoli_usa_da_codifica(self):
        """Carica nel tab Capitoli il file attualmente selezionato nella
        lista del tab Codifica, senza doverlo ribrowsare da capo."""
        sel = self._listbox.curselection()
        if len(sel) != 1:
            messagebox.showwarning("Attenzione",
                "Seleziona esattamente un file nella lista 'File trovati' del tab Codifica.")
            return
        self._capitoli_carica(self._all_files[sel[0]])

    def _on_drop_capitoli_src(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data) if Path(p).is_file()]
        if paths:
            self._capitoli_carica(paths[0])

    def _capitoli_carica(self, path: Path):
        self._capitoli_src = path
        self._var_cap_src.set(str(path))

        data = ffprobe_json(path)
        try:
            self._capitoli_durata = float(data.get("format", {}).get("duration") or 0)
        except Exception:
            self._capitoli_durata = 0.0

        esistenti = leggi_capitoli(path)
        self._capitoli = [
            {"start": tk.StringVar(value=formatta_timestamp_capitolo(c["start"])),
             "title": tk.StringVar(value=c["title"])}
            for c in esistenti
        ]
        self._capitoli_render()

    def _capitoli_render(self):
        """Ricostruisce la lista di righe modificabili da self._capitoli
        (stessa logica di _mux_render_extra_tracks: distrugge e ricrea tutto
        ad ogni modifica, più semplice che tenere sincronizzati indici e
        widget separatamente)."""
        for w in self._frm_capitoli_inner.winfo_children():
            w.destroy()

        if self._capitoli_src is None:
            ttk.Label(self._frm_capitoli_inner, text="Nessun file aperto.",
                      foreground="gray").pack(anchor="w")
            self._capitoli_aggiorna_anteprima()
            return

        if not self._capitoli:
            ttk.Label(self._frm_capitoli_inner,
                      text="Nessun capitolo. Usa \"➕ Aggiungi capitolo\" o \"📂 Importa da file…\".",
                      foreground="gray").pack(anchor="w")
            self._capitoli_aggiorna_anteprima()
            return

        grid = ttk.Frame(self._frm_capitoli_inner)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)  # il titolo si allarga con la finestra
        ttk.Label(grid, text="Inizio", foreground="gray").grid(row=0, column=0, padx=(0, 8))
        ttk.Label(grid, text="Titolo", foreground="gray").grid(row=0, column=1, sticky="w")

        for i, c in enumerate(self._capitoli):
            row = i + 1
            e_start = ttk.Entry(grid, textvariable=c["start"], width=13)
            e_start.grid(row=row, column=0, padx=(0, 8), pady=1, sticky="w")
            e_start.bind("<KeyRelease>", lambda _e: self._capitoli_aggiorna_anteprima())
            e_title = ttk.Entry(grid, textvariable=c["title"])
            e_title.grid(row=row, column=1, padx=(0, 8), pady=1, sticky="ew")
            e_title.bind("<KeyRelease>", lambda _e: self._capitoli_aggiorna_anteprima())
            ttk.Button(grid, text="✕", width=3,
                       command=lambda i=i: self._capitoli_rimuovi(i)).grid(row=row, column=2, pady=1)

        self._capitoli_aggiorna_anteprima()

    def _capitoli_aggiungi(self):
        if self._capitoli_src is None:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return
        # Punto di partenza comodo: 1 minuto dopo l'ultimo capitolo esistente
        # (o l'inizio file, se è il primo) — l'utente lo corregge comunque,
        # è solo per non partire sempre da 00:00:00 quando ce ne sono già.
        if self._capitoli:
            ultimo = max(parsa_timestamp_capitolo(c["start"].get()) or 0.0 for c in self._capitoli)
            inizio = min(ultimo + 60.0, self._capitoli_durata) if self._capitoli_durata else ultimo + 60.0
        else:
            inizio = 0.0
        self._capitoli.append({
            "start": tk.StringVar(value=formatta_timestamp_capitolo(inizio)),
            "title": tk.StringVar(value=f"Capitolo {len(self._capitoli) + 1}"),
        })
        self._capitoli_render()

    def _capitoli_rimuovi(self, i: int):
        if 0 <= i < len(self._capitoli):
            del self._capitoli[i]
        self._capitoli_render()

    def _capitoli_importa(self):
        if self._capitoli_src is None:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return
        path = filedialog.askopenfilename(
            title="Importa capitoli (formato OGM/SimpleChapters)",
            initialdir=str(self._capitoli_src.parent),
            filetypes=[("File di testo", "*.txt"), ("Tutti i file", "*.*")])
        if not path:
            return
        try:
            testo = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile leggere il file:\n{e}")
            return

        importati = parsa_capitoli_ogm(testo)
        if not importati:
            messagebox.showwarning("Attenzione",
                "Nessun capitolo riconosciuto nel file (atteso formato OGM/SimpleChapters: "
                "CHAPTER01=00:00:00.000 / CHAPTER01NAME=Titolo).")
            return

        if self._capitoli and not messagebox.askyesno("Importa capitoli",
                f"Trovati {len(importati)} capitoli nel file. Sostituire i {len(self._capitoli)} "
                "capitoli attuali?"):
            return

        self._capitoli = [
            {"start": tk.StringVar(value=formatta_timestamp_capitolo(c["start"])),
             "title": tk.StringVar(value=c["title"])}
            for c in importati
        ]
        self._capitoli_render()

    def _capitoli_esporta(self):
        """Esporta l'elenco capitoli ATTUALMENTE mostrato (con le modifiche
        non ancora salvate nel file, se ce ne sono) in un file di testo
        OGM/SimpleChapters — indipendente dal file video, così è
        riutilizzabile/condivisibile o reimportabile in futuro."""
        if not self._capitoli:
            messagebox.showwarning("Attenzione", "Nessun capitolo da esportare.")
            return
        capitoli, non_validi = self._capitoli_raccogli()
        if non_validi:
            messagebox.showwarning("Attenzione",
                f"Orario non valido: {', '.join(non_validi)}. Correggilo prima di esportare.")
            return
        nome_default = (f"{self._capitoli_src.stem}_capitoli.txt"
                        if self._capitoli_src else "capitoli.txt")
        # initialdir esplicito: senza, il dialogo riparte dall'ultima cartella
        # visitata da Tk (anche di un'altra sessione), non da quella del file
        # video caricato — verificato che è quello che ci si aspetta qui.
        cartella_iniziale = str(self._capitoli_src.parent) if self._capitoli_src else str(Path.cwd())
        path = filedialog.asksaveasfilename(
            title="Esporta capitoli", defaultextension=".txt", initialfile=nome_default,
            initialdir=cartella_iniziale,
            filetypes=[("File di testo", "*.txt"), ("Tutti i file", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(formatta_capitoli_ogm(capitoli), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Errore", f"Impossibile scrivere il file:\n{e}")
            return
        messagebox.showinfo("Esporta capitoli", f"Capitoli esportati in:\n{path}")

    def _capitoli_raccogli(self):
        """Legge lo stato attuale dei campi in una lista semplice di
        {"start": float, "title": str}, scartando righe con un orario non
        interpretabile (segnalate come avviso, non bloccano le altre)."""
        capitoli, non_validi = [], []
        for c in self._capitoli:
            val = parsa_timestamp_capitolo(c["start"].get())
            if val is None:
                non_validi.append(c["start"].get())
                continue
            capitoli.append({"start": val, "title": c["title"].get()})
        return capitoli, non_validi

    def _capitoli_aggiorna_anteprima(self):
        if self._capitoli_src is None:
            self._var_cap_cmd.set("")
            self._lbl_cap_dst.config(text="")
            self._mostra_avviso_cap(None)
            return

        nome = self._capitoli_src.name
        # Il file temporaneo (ffmetadata o lista OGM) viene scritto solo alla
        # generazione (_capitoli_genera): qui si mostra solo la STRUTTURA del
        # comando che verrà davvero eseguito, coerente con genera_capitoli.
        mkvpropedit = trova_mkvpropedit() if self._capitoli_src.suffix.lower() in (".mkv", ".mka") else None
        if mkvpropedit:
            self._lbl_cap_dst.config(
                text=f"✓ mkvpropedit: modifica \"{nome}\" sul posto, senza riscrivere il file.")
            chap_tmp = Path(tempfile.gettempdir()) / f"_capitoli_ogm_{os.getpid()}.txt"
            cmd = [Path(mkvpropedit).name, nome, "--chapters", chap_tmp.name]
        else:
            motivo = "" if self._capitoli_src.suffix.lower() in (".mkv", ".mka") \
                     else " (mkvpropedit gestisce solo mkv/mka)"
            self._lbl_cap_dst.config(
                text=f"⚠ mkvpropedit non disponibile{motivo}: ffmpeg riscriverà \"{nome}\" per intero.")
            meta_tmp = Path(tempfile.gettempdir()) / f"_capitoli_meta_{os.getpid()}.txt"
            cmd = build_capitoli_cmd(Path(nome), meta_tmp, Path(f".{Path(nome).stem}.capitolifix{self._capitoli_src.suffix}"))
        self._var_cap_cmd.set(" ".join(str(c) for c in cmd))

        _capitoli, non_validi = self._capitoli_raccogli()
        avvisi = []
        if non_validi:
            avvisi.append(f"Orario non valido, ignorato: {', '.join(non_validi)}")
        self._mostra_avviso_cap(avvisi)

    def _mostra_avviso_cap(self, testi):
        if testi:
            self._lbl_cap_cmd_warning.configure(text="\n".join(f"⚠ {t}" for t in testi))
            self._lbl_cap_cmd_warning.pack(fill="x", padx=5, pady=(0, 4))
        else:
            self._lbl_cap_cmd_warning.pack_forget()

    def _capitoli_genera(self):
        if self._active_op:
            messagebox.showwarning("Attenzione", "È già in corso un'altra operazione.")
            return
        if self._capitoli_src is None:
            messagebox.showwarning("Attenzione", "Apri prima un file sorgente.")
            return
        if not self._capitoli:
            messagebox.showwarning("Attenzione", "Aggiungi almeno un capitolo (o importane un elenco).")
            return

        capitoli, non_validi = self._capitoli_raccogli()
        if non_validi:
            messagebox.showwarning("Attenzione",
                f"Orario non valido: {', '.join(non_validi)}. Correggilo prima di continuare.")
            return

        veloce = usa_mkvpropedit_per(self._capitoli_src)
        msg = (f"I capitoli di \"{self._capitoli_src.name}\" verranno modificati SUL POSTO "
               "(mkvpropedit, nessuna riscrittura del file). Continuare?" if veloce else
               f"\"{self._capitoli_src.name}\" verrà riscritto per intero (nessun mkvpropedit "
               "disponibile per questo file) per applicare i nuovi capitoli, sostituendo "
               "l'originale sullo stesso percorso. Su file molto grandi può richiedere qualche "
               "istante. Continuare?")
        if not messagebox.askyesno("Applica capitoli", msg):
            return

        self._log_clear()
        self._log_write("info", f"Inizio applicazione capitoli: {self._capitoli_src.name}\n")

        self._stop_ev.clear()
        self._active_op = "capitoli"
        self._btn_cap_start.configure(state="disabled")
        self._btn_cap_stop.configure(state="normal")
        self._lbl_cap_stato.config(text="In corso…")

        threading.Thread(
            target=capitoli_worker,
            args=(self._capitoli_src, capitoli, self._capitoli_durata, self._log_q, self._stop_ev),
            daemon=True
        ).start()

    # -----------------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------------

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _log_write(self, tag: str, text: str):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _poll_log(self):
        try:
            while True:
                tag, msg = self._log_q.get_nowait()
                if tag == "encoders":
                    self._applica_rilevamento(msg)
                elif tag == "done":
                    if self._active_op == "mux":
                        self._btn_mux_start.configure(state="normal")
                        self._btn_mux_stop.configure(state="disabled")
                        self._lbl_mux_stato.config(text="Completato")
                    elif self._active_op == "capitoli":
                        self._btn_cap_start.configure(state="normal")
                        self._btn_cap_stop.configure(state="disabled")
                        self._lbl_cap_stato.config(text="Completato")
                    else:
                        self._btn_start.configure(state="normal")
                        self._btn_stop.configure(state="disabled")
                        self._lbl_stato.config(text="Completato")
                    self._active_op = None
                    # tre campanelle di sistema distanziate (cross-platform via Tk)
                    for i in range(3):
                        self.after(i * 250, self.bell)
                elif tag == "progress":
                    self._log.configure(state="normal")
                    self._log.delete("end-2l", "end-1l")
                    self._log.insert("end", msg + "\n", tag)
                    self._log.see("end")
                    self._log.configure(state="disabled")
                else:
                    self._log_write(tag, msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    App().mainloop()
