# app.py
# Flask API for converting content you are authorized to download into temporary MP3 files.

import logging
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, after_this_request, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
CORS(app, resources={r"/api/*": {"origins": "*"}, r"/health": {"origins": "*"}})

logging.basicConfig(level=logging.INFO)
TEMP_DIR = Path("/tmp/mp3convert")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_AGE_SECONDS = 600
CONVERSION_TIMEOUT_SECONDS = 120
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 3600

URL_PATTERN = re.compile(
    r"(?:youtube\.com/(?:embed/|shorts/|watch\?v=)|"
    r"youtu\.be/|[?&]v=)([a-zA-Z0-9_-]{11})"
)
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be", "www.youtu.be",
}

_cleanup_lock = threading.Lock()


def validate_youtube_url(value):
    """Return a video ID only for HTTPS URLs on an allowed YouTube hostname."""
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
    """Remove temporary files older than ten minutes."""
    now = time.time()
    with _cleanup_lock:
        for path in TEMP_DIR.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > MAX_AGE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                app.logger.warning("Could not clean temporary file: %s", path)


def cleanup_worker():
    """Run cleanup periodically, including when the API receives no requests."""
    while True:
        time.sleep(60)
        cleanup_old_files()


@app.get("/")
def index():
    return jsonify({
        "service": "YouTube MP3 API",
        "status": "running",
        "health": "/health",
        "convert": "POST /api/convert",
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
        return jsonify({"error": "Geçersiz YouTube bağlantısı."}), 400

    token = secrets.token_urlsafe(32)
    output_template = str(TEMP_DIR / f"{token}.%(ext)s")
    expected_file = TEMP_DIR / f"{token}.mp3"

    command = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout", "20",
        "--max-filesize", str(MAX_FILE_SIZE_BYTES),
        "--match-filter", f"duration <= {MAX_VIDEO_DURATION_SECONDS}",
        "--output", output_template,
        "--print", "title",
        "--print", "after_move:filepath",
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
        )

        if completed.returncode != 0:
            app.logger.warning("yt-dlp failed: %s", completed.stderr[-1500:])
            expected_file.unlink(missing_ok=True)
            return jsonify({"error": "Dönüştürme başarısız. İçerik erişilemiyor veya desteklenmiyor."}), 422

        # yt-dlp prints the title and final filepath; trust only the expected output path.
        output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        title = "YouTube Audio"
        if len(output_lines) >= 2 and output_lines[-1] == str(expected_file):
            title = output_lines[-2][:250] or title

        if not expected_file.is_file():
            app.logger.error("Expected output file was not created: %s", expected_file)
            return jsonify({"error": "Dönüştürülen dosya bulunamadı."}), 422

        if expected_file.stat().st_size > MAX_FILE_SIZE_BYTES:
            expected_file.unlink(missing_ok=True)
            return jsonify({"error": "Dosya izin verilen boyutu aşıyor."}), 413

        return jsonify({"token": token, "title": title}), 200

    except subprocess.TimeoutExpired:
        expected_file.unlink(missing_ok=True)
        return jsonify({"error": "İşlem 120 saniyede tamamlanamadı. Daha sonra tekrar deneyin."}), 408
    except FileNotFoundError:
        expected_file.unlink(missing_ok=True)
        app.logger.exception("yt-dlp executable was not found")
        return jsonify({"error": "yt-dlp veya FFmpeg bulunamadı."}), 500
    except Exception:
        expected_file.unlink(missing_ok=True)
        app.logger.exception("Unexpected conversion error")
        return jsonify({"error": "Sunucu tarafında beklenmeyen bir hata oluştu."}), 500


@app.get("/api/download/<token>")
def download(token):
    cleanup_old_files()

    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return jsonify({"error": "Geçersiz indirme token'ı."}), 400

    file_path = TEMP_DIR / f"{token}.mp3"
    if not file_path.is_file():
        return jsonify({"error": "Dosya bulunamadı veya bağlantının süresi doldu."}), 404

    response = send_file(
        file_path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name="audio.mp3",
        conditional=False,
        max_age=0,
    )

    # Flask/Werkzeug closes the response after the file has been sent.
    response.call_on_close(lambda: file_path.unlink(missing_ok=True))
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "İstek boyutu çok büyük."}), 413


# Start one periodic cleanup thread per process.
_cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True, name="mp3-cleanup")
_cleanup_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
