"""Runs one BCSFE job in an isolated process.

The web server (or Discord bot) starts this script once per request, sends the job as JSON on stdin and
reads the result as JSON from stdout. Running each job in its own process keeps
BCSFE's global state (config, cached game data, country code) from leaking
between users, and means one crashed or hung edit can't take the bot down.

Job format (stdin):
{
  "mode": "codes" | "file",
  "transfer_code": "...", "confirmation_code": "...",   # mode == codes
  "file_b64": "...",                                     # mode == file
  "cc": "en" | "jp" | "kr" | "tw" | null,
  "edits": {"catfood": 45000, "xp": 99999999, "unlock_cats": true, ...},
  "new_account": false,
  "data_dir": "/path/to/shared/bcsfe-data",
  "job_dir": "/path/to/private/tmp/dir"
}
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import traceback
from typing import Any, Callable

# BCSFE prints to stdout (progress messages, colour codes). Keep the real
# stdout for our JSON result and send everything else to stderr.
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from importlib import resources  # noqa: E402

import bcsfe  # noqa: E402
from bcsfe import core  # noqa: E402

# Numeric fields: (save attribute, max-value key, managed item type or None).
# Managed items are how the game server tracks premium currency; BCSFE reports
# changes to them so the server's records match the save (ban prevention).
NUMERIC_FIELDS: dict[str, tuple[str, str, Any]] = {
    "catfood": ("catfood", "catfood", core.ManagedItemType.CATFOOD),
    "xp": ("xp", "xp", None),
    "np": ("np", "np", None),
    "leadership": ("leadership", "leadership", None),
    "normal_tickets": ("normal_tickets", "normal_tickets", None),
    "rare_tickets": ("rare_tickets", "rare_tickets", core.ManagedItemType.RARE_TICKET),
    "platinum_tickets": (
        "platinum_tickets",
        "platinum_tickets",
        core.ManagedItemType.PLATINUM_TICKET,
    ),
    "legend_tickets": (
        "legend_tickets",
        "legend_tickets",
        core.ManagedItemType.LEGEND_TICKET,
    ),
    "platinum_shards": ("platinum_shards", "platinum_tickets", None),
}

LABELS = {
    "catfood": "Cat Food",
    "xp": "XP",
    "np": "NP",
    "leadership": "Leadership",
    "normal_tickets": "Cat Tickets",
    "rare_tickets": "Rare Tickets",
    "platinum_tickets": "Platinum Tickets",
    "legend_tickets": "Legend Tickets",
    "platinum_shards": "Platinum Shards",
}


def migrate_data(data_dir: str) -> None:
    """Copy BCSFE's bundled files (locales, themes, max values) into data_dir."""
    core.set_data_dir_path(core.Path(data_dir))
    version_path = core.Path.get_data_folder().add("version.txt")
    if version_path.exists() and version_path.read().to_str().strip() == bcsfe.__version__:
        return
    src = resources.files(bcsfe.__app_name__).joinpath("files")
    bcsfe.copy_to_data_dir(src, src)
    version_path.write(core.Data(bcsfe.__version__))


@contextlib.contextmanager
def game_data_lock(data_dir: str):
    """Serialise game-data downloads so concurrent jobs don't corrupt the cache."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, ".game_data.lock"), "a+") as fh:
        if os.name == "nt":  # Windows
            import msvcrt
            import time

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def snapshot(save: core.SaveFile) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, (attr, _, _) in NUMERIC_FIELDS.items():
        try:
            out[key] = int(getattr(save, attr))
        except Exception:
            pass
    try:
        out["cats_unlocked"] = len(save.cats.get_unlocked_cats())
    except Exception:
        pass
    return out


def apply_edits(save: core.SaveFile, edits: dict[str, Any], data_dir: str) -> tuple[list[str], list[str]]:
    done: list[str] = []
    failed: list[str] = []
    maxes = core.core_data.max_value_manager

    def attempt(label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            done.append(label)
        except EOFError:
            # BCSFE asked an interactive question (usually: game data repo unreachable)
            failed.append(f"{label}: couldn't download game data — try again later")
            traceback.print_exc()
        except Exception as e:  # keep going; report what failed
            failed.append(f"{label}: {e}")
            traceback.print_exc()

    for key, (attr, max_key, managed_type) in NUMERIC_FIELDS.items():
        if edits.get(key) is None:
            continue
        cap = int(getattr(maxes, max_key))
        if key == "platinum_shards":
            cap *= 10
        value = max(0, min(int(edits[key]), cap))

        def set_value(attr=attr, value=value, managed_type=managed_type):
            before = int(getattr(save, attr))
            setattr(save, attr, value)
            if managed_type is not None and value != before:
                core.BackupMetaData(save).add_managed_item(
                    core.ManagedItem.from_change(value - before, managed_type)
                )

        label = LABELS[key] + (f" (capped at {cap:,})" if value != int(edits[key]) else "")
        attempt(label, set_value)

    if edits.get("max_battle_items"):
        def f():
            for item in save.battle_items.items:
                item.amount = maxes.battle_items
        attempt("Battle items maxed", f)

    if edits.get("max_catseyes"):
        def f():
            for i in range(len(save.catseyes)):
                save.catseyes[i] = maxes.catseyes
        attempt("Catseyes maxed", f)

    if edits.get("max_catamins"):
        def f():
            for i in range(len(save.catamins)):
                save.catamins[i] = maxes.catamins
        attempt("Catamins maxed", f)

    if edits.get("max_treasure_chests"):
        def f():
            for i in range(len(save.treasure_chests)):
                save.treasure_chests[i] = maxes.treasure_chests
        attempt("Treasure chests maxed", f)

    # Cat edits need game data (downloaded and cached in data_dir).
    if edits.get("unlock_cats") or edits.get("true_form_cats"):
        with game_data_lock(data_dir):
            if edits.get("unlock_cats"):
                def f():
                    cats = save.cats.get_cats_obtainable(save)
                    if cats is None:
                        raise RuntimeError("couldn't download game data to find obtainable cats")
                    for cat in cats:
                        cat.unlock(save)
                attempt("All obtainable cats unlocked", f)

            if edits.get("true_form_cats"):
                def f():
                    cats = save.cats.get_unlocked_cats()
                    set_forms = core.core_data.config.get_bool(core.ConfigKey.SET_CAT_CURRENT_FORMS)
                    save.cats.true_form_cats(save, cats, False, set_forms)
                attempt("True forms for unlocked cats", f)

    return done, failed


def run(job: dict[str, Any]) -> dict[str, Any]:
    data_dir = job["data_dir"]
    job_dir = job["job_dir"]
    os.makedirs(job_dir, exist_ok=True)

    migrate_data(data_dir)
    # Per-job config/log so nothing user-specific lands in the shared folder.
    core.set_log_path(core.Path(os.path.join(job_dir, "bcsfe.log")))
    core.set_transfer_backup_path(core.Path(os.path.join(job_dir, "original_SAVE_DATA")))
    core.core_data.init_data()

    cc = core.CountryCode.from_code(job["cc"]) if job.get("cc") else None
    result: dict[str, Any] = {"ok": False}

    # ---- load the save ----------------------------------------------------
    if job["mode"] == "codes":
        if cc is None:
            return {"ok": False, "error": "A country code is required."}
        handler, req = core.ServerHandler.from_codes(
            job["transfer_code"].strip(),
            job["confirmation_code"].strip(),
            cc,
            core.GameVersion(120200),
            print=False,
            save_backup=True,
        )
        if handler is None:
            if req is None:
                return {"ok": False, "error": "Couldn't reach the game servers. Try again later."}
            hint = " (JP and TW codes are easy to mix up — check the country.)" if job["cc"] in ("jp", "tw") else ""
            return {"ok": False, "error": "Invalid transfer code, confirmation code or country." + hint}
        save = handler.save_file
        backup = os.path.join(job_dir, "original_SAVE_DATA")
        if os.path.exists(backup):
            with open(backup, "rb") as fh:
                result["original_b64"] = base64.b64encode(fh.read()).decode()
    else:
        raw = core.Data(base64.b64decode(job["file_b64"]))
        try:
            save = core.SaveFile(raw, cc)
        except core.CantDetectSaveCCError:
            return {"ok": False, "error": "Couldn't detect the save's country. Choose your game's country and try again."}
        except Exception as e:
            return {"ok": False, "error": f"Couldn't read that save file: {e}"}

    result["country"] = save.cc.get_code()
    result["game_version"] = save.game_version.to_string()
    result["before"] = snapshot(save)

    # ---- edit ---------------------------------------------------------------
    done, failed = apply_edits(save, job.get("edits") or {}, data_dir)
    result["done"] = done
    result["failed"] = failed
    result["after"] = snapshot(save)

    # ---- output -------------------------------------------------------------
    if job["mode"] == "codes":
        if job.get("new_account"):
            if not core.ServerHandler(save, print=False).create_new_account():
                result["failed"].append("New account: server refused; uploaded to the existing account")
            else:
                result["done"].append("Moved to a new account (inquiry code)")
        codes = core.ServerHandler(save, print=False).get_codes()
        if codes is None:
            result["error"] = (
                "Edits were applied but the upload failed. Download your original save below — "
                "you can restore it with a save manager or try again."
            )
            result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()
            return result
        result["transfer_code"], result["confirmation_code"] = codes
    else:
        result["edited_b64"] = base64.b64encode(save.to_data().to_bytes()).decode()

    result["ok"] = True
    return result


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        out = run(job)
    except Exception as e:
        traceback.print_exc()
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _REAL_STDOUT.write(json.dumps(out))
    _REAL_STDOUT.flush()


if __name__ == "__main__":
    main()
