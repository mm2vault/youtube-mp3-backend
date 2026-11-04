# Flask API for converting user-authorized media into temporary MP3 files.
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_BYTES + 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": "*"}, r"/health": {"origins": "*"}})

logging.basicConfig(level=logging.INFO)
TEMP_DIR = Path("/tmp/mp3convert")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_AGE_SECONDS = 600
CONVERSION_TIMEOUT_SECONDS = 150
MAX_VIDEO_DURATION_SECONDS = 3600
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
URL_PATTERN = re.compile(
    r"(?:youtube\.com/(?:embed/|shorts/|watch\?v=)|youtu\.be/|[?&]v=)([a-zA-Z0-9_-]{11})"
)
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}
_cleanup_lock = threading.Lock()


def validate_youtube_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            return None
        match = URL_PATTERN.search(value.strip())
        return match.group(1) if match else None
    except (TypeError, ValueError):
        return None


def cleanup_old_files():
    now = time.time()
    with _cleanup_lock:
        for path in TEMP_DIR.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > MAX_AGE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                app.logger.warning("Could not clean temporary file: %s", path)


def cleanup_worker():
    while True:
        time.sleep(60)
        cleanup_old_files()


def run_ffmpeg(source: Path, destination: Path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if probe.returncode != 0:
        raise ValueError("Dosya okunamadı veya desteklenmeyen bir medya dosyası.")
    try:
        duration = float(probe.stdout.strip())
    except (TypeError, ValueError):
        raise ValueError("Medya süresi okunamadı.")
    if duration <= 0 or duration > MAX_VIDEO_DURATION_SECONDS:
        raise ValueError("Dosya boş veya 60 dakikadan uzun.")
    converted = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(source), "-vn",
         "-map", "0:a:0", "-codec:a", "libmp3lame", "-q:a", "0",
         "-map_metadata", "-1", str(destination)],
        capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_SECONDS, check=False,
    )
    if converted.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise ValueError("Dosyadan ses çıkarılamadı. Dosyanın ses içerdiğinden emin olun.")
    if destination.stat().st_size > MAX_FILE_SIZE_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError("Oluşturulan MP3 dosyası 100 MB sınırını aşıyor.")


@app.get("/")
def index():
    return jsonify({
        "service": "WaveConvert API", "status": "running", "health": "/health",
        "youtube_convert": "POST /api/convert", "file_convert": "POST /api/convert-file",
        "download": "GET /api/download/<token>"
    }), 200


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.post("/api/convert")
def convert():
    cleanup_old_files()
    payload = request.get_json(silent=True) or {}
    video_id = validate_youtube_url(payload.get("url"))
    if not video_id:
        return jsonify({"error": "Geçersiz YouTube bağlantısı. HTTPS bağlantısı kullanın."}), 400

    token = secrets.token_urlsafe(32)
    expected_file = TEMP_DIR / f"{token}.mp3"
    command = [
        "yt-dlp", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
        "--no-playlist", "--no-warnings", "--no-progress", "--socket-timeout", "20",
        "--max-filesize", str(MAX_FILE_SIZE_BYTES),
        "--match-filter", f"duration <= {MAX_VIDEO_DURATION_SECONDS}",
        "--output", str(TEMP_DIR / f"{token}.%(ext)s"),
        "--print", "title", "--print", "after_move:filepath",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=CONVERSION_TIMEOUT_SECONDS, check=False)
        if completed.returncode != 0:
            details = completed.stderr[-2500:]
            app.logger.warning("yt-dlp failed: %s", details)
            expected_file.unlink(missing_ok=True)
            if "Sign in to confirm" in details or "not a bot" in details.lower():
                return jsonify({"error": "YouTube bu sunucudan gelen isteği bot korumasıyla engelledi. Bu bağlantı şu anda dönüştürülemiyor; dosya yükleme seçeneğini kullanabilirsiniz."}), 422
            if "Private video" in details or "Video unavailable" in details:
                return jsonify({"error": "Video özel, kaldırılmış veya bu bölgede erişilemiyor."}), 422
            return jsonify({"error": "YouTube içeriğine erişilemedi veya içerik desteklenmiyor. İzinli medya dosyanızı yükleyerek deneyebilirsiniz."}), 422

        output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        title = output_lines[-2][:250] if len(output_lines) >= 2 and output_lines[-1] == str(expected_file) else "YouTube Audio"
        if not expected_file.is_file():
            return jsonify({"error": "Dönüştürülen dosya bulunamadı."}), 422
        if expected_file.stat().st_size > MAX_FILE_SIZE_BYTES:
            expected_file.unlink(missing_ok=True)
            return jsonify({"error": "Dosya izin verilen boyutu aşıyor."}), 413
        return jsonify({"token": token, "title": title}), 200
    except subprocess.TimeoutExpired:
        expected_file.unlink(missing_ok=True)
        return jsonify({"error": "İşlem zaman aşımına uğradı. Daha kısa bir içerik deneyin."}), 408
    except FileNotFoundError:
        expected_file.unlink(missing_ok=True)
        app.logger.exception("yt-dlp or FFmpeg executable missing")
        return jsonify({"error": "Sunucuda gerekli dönüştürme aracı bulunamadı."}), 500
    except Exception:
        expected_file.unlink(missing_ok=True)
        app.logger.exception("Unexpected YouTube conversion error")
        return jsonify({"error": "Sunucu tarafında beklenmeyen bir hata oluştu."}), 500


@app.post("/api/convert-file")
def convert_file():
    cleanup_old_files()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Lütfen bir medya dosyası seçin."}), 400

    safe_name = secure_filename(uploaded.filename)
    extension = Path(safe_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Desteklenmeyen dosya türü. MP4, MOV, MKV, WebM, MP3, WAV, M4A, AAC, OGG veya FLAC yükleyin."}), 415

    token = secrets.token_urlsafe(32)
    source = TEMP_DIR / f"{token}.upload{extension}"
    output = TEMP_DIR / f"{token}.mp3"
    try:
        uploaded.save(source)
        if not source.is_file() or source.stat().st_size == 0:
            return jsonify({"error": "Yüklenen dosya boş."}), 400
        if source.stat().st_size > MAX_FILE_SIZE_BYTES:
            return jsonify({"error": "Dosya 100 MB sınırını aşıyor."}), 413
        run_ffmpeg(source, output)
        title = Path(safe_name).stem[:180] or "Audio"
        return jsonify({"token": token, "title": title}), 200
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        return jsonify({"error": "Dönüştürme zaman aşımına uğradı. Daha küçük bir dosya deneyin."}), 408
    except ValueError as error:
        output.unlink(missing_ok=True)
        return jsonify({"error": str(error)}), 422
    except FileNotFoundError:
        output.unlink(missing_ok=True)
        app.logger.exception("FFmpeg executable missing")
        return jsonify({"error": "Sunucuda FFmpeg bulunamadı."}), 500
    except Exception:
        output.unlink(missing_ok=True)
        app.logger.exception("Unexpected file conversion error")
        return jsonify({"error": "Dosya dönüştürülürken sunucu hatası oluştu."}), 500
    finally:
        source.unlink(missing_ok=True)



@app.post("/api/convert-file")
def convert_file():
    cleanup_old_files()
    from werkzeug.utils import secure_filename
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Lütfen bir medya dosyası seçin."}), 400
    filename = secure_filename(uploaded.filename)
    extension = Path(filename).suffix.lower()
    allowed = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    if extension not in allowed:
        return jsonify({"error": "Desteklenmeyen medya dosyası."}), 415

    token = secrets.token_urlsafe(32)
    source = TEMP_DIR / (token + ".source" + extension)
    output = TEMP_DIR / (token + ".mp3")
    try:
        uploaded.save(source)
        if not source.is_file() or source.stat().st_size == 0:
            return jsonify({"error": "Dosya boş."}), 400
        if source.stat().st_size > MAX_FILE_SIZE_BYTES:
            return jsonify({"error": "Dosya 100 MB sınırını aşıyor."}), 413
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(source)], capture_output=True, text=True, timeout=20)
        duration = float(probe.stdout.strip()) if probe.returncode == 0 else 0
        if duration <= 0 or duration > MAX_VIDEO_DURATION_SECONDS:
            return jsonify({"error": "Dosya okunamadı veya 60 dakikadan uzun."}), 422
        converted = subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(source), "-vn", "-map", "0:a:0", "-codec:a", "libmp3lame", "-q:a", "0", str(output)], capture_output=True, text=True, timeout=CONVERSION_TIMEOUT_SECONDS)
        if converted.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            return jsonify({"error": "Dosyadan ses çıkarılamadı."}), 422
        return jsonify({"token": token, "title": Path(filename).stem[:180]}), 200
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        return jsonify({"error": "Dönüştürme zaman aşımına uğradı."}), 408
    except Exception:
        output.unlink(missing_ok=True)
        app.logger.exception("File conversion failed")
        return jsonify({"error": "Dosya dönüştürülürken hata oluştu."}), 500
    finally:
        source.unlink(missing_ok=True)


@app.get("/api/download/<token>")
def download(token):
    cleanup_old_files()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return jsonify({"error": "Geçersiz indirme token'ı."}), 400
    file_path = TEMP_DIR / f"{token}.mp3"
    if not file_path.is_file():
        return jsonify({"error": "Dosya bulunamadı veya bağlantının süresi doldu."}), 404
    response = send_file(file_path, mimetype="audio/mpeg", as_attachment=True,
                         download_name="audio.mp3", conditional=False, max_age=0)
    response.call_on_close(lambda: file_path.unlink(missing_ok=True))
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Dosya 100 MB yükleme sınırını aşıyor."}), 413


_cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True, name="mp3-cleanup")
_cleanup_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
