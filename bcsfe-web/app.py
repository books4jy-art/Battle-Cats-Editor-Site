"""Web front-end for BCSFE (Battle Cats Save File Editor).

Run it with:  python app.py   then open http://localhost:8000

Routes:
  GET  /           – the editor page (static/index.html)
  POST /api/edit   – run an edit; form fields described in static/index.html
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------- config ----
HERE = Path(__file__).resolve().parent
DATA_DIR = os.path.abspath(os.environ.get("BCSFE_DATA_DIR", str(HERE / "bcsfe-data")))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "300"))
# Set to 1 when running behind a reverse proxy (Render, Railway, nginx…) so the
# real visitor IP is used for rate limiting.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
WORKER = str(HERE / "worker.py")

COUNTRIES = {"en", "jp", "kr", "tw"}
NUMERIC = [
    "catfood", "xp", "np", "leadership", "normal_tickets", "rare_tickets",
    "platinum_tickets", "legend_tickets", "platinum_shards",
]
TOGGLES = [
    "unlock_cats", "true_form_cats", "max_battle_items", "max_catseyes",
    "max_catamins", "max_treasure_chests",
]
I32_MAX = 2_147_483_647

log = logging.getLogger("bcsfe-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # save files are ~100 KB–1 MB

job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
state_lock = threading.Lock()
active_ips: set[str] = set()
last_run: dict[str, float] = {}


def client_ip() -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    job_dir = tempfile.mkdtemp(prefix="bcsfe-job-")
    job = {**job, "data_dir": DATA_DIR, "job_dir": job_dir}
    try:
        if not job_slots.acquire(timeout=120):
            return {"ok": False, "error": "The editor is busy right now. Try again in a minute."}
        try:
            proc = subprocess.run(
                [sys.executable, WORKER],
                input=json.dumps(job).encode(),
                capture_output=True,
                timeout=JOB_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "The editor took too long and was stopped. Try again later."}
        finally:
            job_slots.release()
        if proc.returncode != 0 or not proc.stdout:
            log.error("worker failed (rc=%s): %s", proc.returncode, proc.stderr.decode(errors="replace")[-2000:])
            return {"ok": False, "error": "The editor crashed. Check the server logs."}
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def parse_ids(raw: str, limit: int = 5000) -> list[int] | None:
    """Parse "1, 5, 10-12" into [1, 5, 10, 11, 12]; None if malformed."""
    ids: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        lo, sep, hi = part.partition("-")
        if not lo.isdigit() or (sep and not hi.isdigit()):
            return None
        a, b = int(lo), int(hi) if sep else int(lo)
        if a > b or b >= limit:
            return None
        ids.update(range(a, b + 1))
    return sorted(ids)


def parse_edits(form) -> dict[str, Any] | str:
    edits: dict[str, Any] = {}
    for key in NUMERIC:
        raw = (form.get(key) or "").strip().replace(",", "")
        if not raw:
            continue
        if not raw.isdigit():
            return f"{key.replace('_', ' ').title()} must be a whole number."
        edits[key] = min(int(raw), I32_MAX)
    for key in TOGGLES:
        if form.get(key) in ("1", "true", "on"):
            edits[key] = True

    # Story stages: which chapters (0-8), whether to clear them, and a treasure level.
    raw = (form.get("story_chapters") or "").strip()
    chapters: list[int] = []
    if raw:
        parts = raw.split(",")
        if not all(p.strip().isdigit() and int(p) <= 8 for p in parts):
            return 'Unknown story chapter.'
        chapters = sorted({int(p) for p in parts})
    treasure = (form.get("treasure_level") or "").strip()
    if treasure and treasure not in ("0", "1", "2", "3"):
        return 'Unknown treasure level.'
    clear_story = form.get("clear_story") in ("1", "true", "on")
    if (clear_story or treasure) and not chapters:
        return 'Choose at least one chapter for the story stage edits.'
    if clear_story:
        edits["clear_story"] = True
    if treasure:
        edits["treasure_level"] = int(treasure)
    if clear_story or treasure:
        edits["story_chapters"] = chapters

    # Individual characters: "25, 100-110" style ID lists; rarity groups 0-5.
    for key in ("add_cats", "remove_cats"):
        ids = parse_ids(form.get(key) or "")
        if ids is None:
            return 'Character IDs must be numbers like 25 or 100-110.'
        if ids:
            edits[key] = ids
    rarities = parse_ids(form.get("add_rarities") or "")
    if rarities is None or any(r > 5 for r in rarities):
        return 'Unknown rarity.'
    if rarities:
        edits["add_rarities"] = rarities

    # Legend/event maps: which groups to clear and how many crowns (0 = all).
    maps = [k for k in ['legend', 'uncanny', 'zero', 'event', 'collab'] if form.get(f"clear_{k}") in ("1", "true", "on")]
    crowns = (form.get("map_crowns") or "0").strip()
    if crowns not in ("0", "1", "2", "3", "4"):
        return 'Unknown crown count.'
    if maps:
        edits["clear_maps"] = maps
        edits["map_crowns"] = int(crowns)
    return edits


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


catalog_lock = threading.Lock()
catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CATALOG_MAX_AGE = 6 * 3600


@app.get("/api/cats")
def cats():
    """Character list (id, name, rarity, obtainable) for the search box, cached per region."""
    cc = (request.args.get("cc") or 'en').lower()
    if cc not in COUNTRIES:
        return fail("Unknown country.")
    cached = catalog_cache.get(cc)
    if cached and time.time() - cached[0] < CATALOG_MAX_AGE:
        return jsonify(cached[1])
    if not catalog_lock.acquire(timeout=90):
        return fail('The character list is busy. Try again in a moment.', 503)
    try:
        cached = catalog_cache.get(cc)
        if cached and time.time() - cached[0] < CATALOG_MAX_AGE:
            return jsonify(cached[1])
        result = run_job({"mode": "catalog", "cc": cc})
        if result.get("ok"):
            catalog_cache[cc] = (time.time(), result)
        return jsonify(result), (200 if result.get("ok") else 502)
    finally:
        catalog_lock.release()


@app.post("/api/edit")
def edit():
    form = request.form
    mode = form.get("mode")
    if mode not in ("codes", "file"):
        return fail("Unknown mode.")

    country = (form.get("country") or "").strip().lower() or None
    if country is not None and country not in COUNTRIES:
        return fail("Unknown country.")

    edits = parse_edits(form)
    if isinstance(edits, str):
        return fail(edits)

    job: dict[str, Any] = {"mode": mode, "cc": country, "edits": edits}

    if mode == "codes":
        tc = (form.get("transfer_code") or "").strip()
        pin = (form.get("confirmation_code") or "").strip()
        if not tc or not pin:
            return fail("Enter both the transfer code and the confirmation code.")
        if len(tc) > 64 or len(pin) > 16 or not tc.isalnum() or not pin.isalnum():
            return fail("Those codes don't look right. Copy them exactly as the game shows them.")
        if country is None:
            return fail("Choose your game's country.")
        if not edits:
            return fail("Choose at least one thing to edit. Transfer codes only work once, "
                        "so the save has to be re-uploaded with changes.")
        job.update(transfer_code=tc, confirmation_code=pin,
                   new_account=form.get("new_account") in ("1", "true", "on"))
    else:
        upload = request.files.get("save_file")
        if upload is None or not upload.filename:
            return fail("Choose a SAVE_DATA file.")
        data = upload.read()
        if not data:
            return fail("That file is empty.")
        job["file_b64"] = base64.b64encode(data).decode()

    ip = client_ip()
    with state_lock:
        if ip in active_ips:
            return fail("You already have an edit running. Wait for it to finish.", 429)
        wait = COOLDOWN_SECONDS - (time.monotonic() - last_run.get(ip, -1e9))
        if wait > 0:
            return fail(f"Slow down — you can run another edit in {int(wait) + 1}s.", 429)
        active_ips.add(ip)
    try:
        result = run_job(job)
    finally:
        with state_lock:
            active_ips.discard(ip)
            last_run[ip] = time.monotonic()

    if mode == "file" and not edits:
        result.pop("edited_b64", None)  # nothing changed; just show info
    return jsonify(result)


@app.errorhandler(413)
def too_large(_):
    return fail("That file is too big to be a save file.", 413)


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        from waitress import serve
    except ImportError:
        log.warning("waitress not installed; using Flask's development server")
        app.run(host=HOST, port=PORT)
        return
    log.info("BCSFE web editor running on http://localhost:%s", PORT)
    serve(app, host=HOST, port=PORT, threads=8)


if __name__ == "__main__":
    main()
