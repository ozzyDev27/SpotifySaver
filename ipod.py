import os
import platform
import shutil
import subprocess
import sys

DATA = os.path.join(os.path.expanduser("~"), ".ipod-uploader")
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
VENV = os.path.join(DATA, "venv")
VPY = os.path.join(VENV, "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")
PORT = 5510
PACKAGES = ["flask", "requests", "eyed3", "mutagen", "Pillow", "yt-dlp"]
sys.stdout.reconfigure(line_buffering=True)


def pkg_install(name):
	if IS_MAC:
		if not shutil.which("brew"):
			sys.exit(f"{name} missing and Homebrew not found. Install Homebrew from https://brew.sh then rerun.")
		subprocess.run(["brew", "install", name], check=True)
	elif IS_WIN:
		if not shutil.which("winget"):
			sys.exit(f"{name} missing and winget not found. Install {name} manually then rerun.")
		ids = {"ffmpeg": "Gyan.FFmpeg", "deno": "DenoLand.Deno"}
		subprocess.run(["winget", "install", "-e", "--id", ids[name], "--accept-source-agreements", "--accept-package-agreements"], check=True)
	elif shutil.which("apt-get"):
		if name == "deno":
			subprocess.run(["sh", "-c", "curl -fsSL https://deno.land/install.sh | sh"], check=True)
			os.environ["PATH"] = os.path.expanduser("~/.deno/bin") + os.pathsep + os.environ["PATH"]
		else:
			subprocess.run(["sudo", "apt-get", "install", "-y", name], check=True)
	elif shutil.which("dnf"):
		subprocess.run(["sudo", "dnf", "install", "-y", name], check=True)
	elif shutil.which("pacman"):
		subprocess.run(["sudo", "pacman", "-S", "--noconfirm", name], check=True)
	else:
		sys.exit(f"{name} missing and no supported package manager found. Install it manually then rerun.")


def ensure_tool(name):
	if shutil.which(name):
		print(f"{name} ok")
		return
	print(f"installing {name}...")
	pkg_install(name)
	if not shutil.which(name):
		sys.exit(f"{name} still not found after install. Restart your terminal and rerun.")


def bootstrap():
	if sys.version_info < (3, 10):
		sys.exit("python 3.10 or newer is required")
	print(f"python {sys.version.split()[0]} ok")
	ensure_tool("ffmpeg")
	ensure_tool("deno")
	os.makedirs(DATA, exist_ok=True)
	if not os.path.exists(VPY):
		print("creating virtual environment...")
		subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
	print("checking python packages...")
	subprocess.run([VPY, "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
	subprocess.run([VPY, "-m", "pip", "install", "-q", "--upgrade", "--pre"] + PACKAGES, check=True)
	print("python packages ok")
	os.execv(VPY, [VPY, os.path.abspath(__file__), "--run"])


if __name__ == "__main__" and "--run" not in sys.argv:
	bootstrap()

import io
import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from urllib import request as rq

import eyed3
import requests
from PIL import Image
from flask import Flask, Response, jsonify, redirect, request, send_file
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, APIC
from yt_dlp import YoutubeDL

CONFIG_PATH = os.path.join(DATA, "config.json")
TOKEN_PATH = os.path.join(DATA, "spotify.json")
WORK = os.path.join(DATA, "work")
MIN_FREE = 700 * 1024 * 1024
REDIRECT_URI = f"http://127.0.0.1:{PORT}/callback"


def load_config():
	if os.path.exists(CONFIG_PATH):
		with open(CONFIG_PATH) as f:
			cfg = json.load(f)
		if cfg.get("client_id") and cfg.get("client_secret"):
			return cfg
	print("\nSpotify credentials needed. Create an app at https://developer.spotify.com/dashboard")
	print(f"Set the redirect URI to {REDIRECT_URI}")
	cfg = {"client_id": input("Client ID: ").strip(), "client_secret": input("Client Secret: ").strip()}
	with open(CONFIG_PATH, "w") as f:
		json.dump(cfg, f)
	return cfg


CFG = load_config()
CLIENT_ID = CFG["client_id"]
CLIENT_SECRET = CFG["client_secret"]

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2048 * 1024 * 1024
logging.getLogger("werkzeug").setLevel(logging.ERROR)

PROG = {}
PROG_LOCK = threading.Lock()
SP = {}
if os.path.exists(TOKEN_PATH):
	with open(TOKEN_PATH) as f:
		SP = json.load(f)


def save_sp():
	with open(TOKEN_PATH, "w") as f:
		json.dump(SP, f)


def ensure_space():
	if shutil.disk_usage(WORK).free < MIN_FREE:
		raise RuntimeError("out of disk space")


def normalize(s):
	s = s.translate(str.maketrans('\\/:*?"<>|', "__       "))
	s = re.sub(r'[!@#$%^&+=\[\]{};\'`,~]', '', s)
	return s.strip()


@app.get("/api/spotify/login")
def spotify_login():
	params = urllib.parse.urlencode({
		"client_id": CLIENT_ID,
		"response_type": "code",
		"redirect_uri": REDIRECT_URI,
		"scope": "playlist-read-private playlist-read-collaborative user-library-read",
	})
	return jsonify({"url": "https://accounts.spotify.com/authorize?" + params})


@app.get("/callback")
def callback():
	code = request.args.get("code")
	if not code:
		return redirect("/")
	r = requests.post("https://accounts.spotify.com/api/token", data={
		"grant_type": "authorization_code",
		"code": code,
		"redirect_uri": REDIRECT_URI,
		"client_id": CLIENT_ID,
		"client_secret": CLIENT_SECRET,
	}, timeout=15)
	tok = r.json()
	if "access_token" in tok:
		SP["token"] = tok["access_token"]
		SP["refresh"] = tok.get("refresh_token")
		SP["exp"] = time.time() + tok.get("expires_in", 3600) - 60
		save_sp()
	return redirect("/")


def sp_token():
	if not SP.get("token"):
		return None
	if time.time() > SP.get("exp", 0) and SP.get("refresh"):
		r = requests.post("https://accounts.spotify.com/api/token", data={
			"grant_type": "refresh_token",
			"refresh_token": SP["refresh"],
			"client_id": CLIENT_ID,
			"client_secret": CLIENT_SECRET,
		}, timeout=15)
		tok = r.json()
		if "access_token" in tok:
			SP["token"] = tok["access_token"]
			SP["exp"] = time.time() + tok.get("expires_in", 3600) - 60
			save_sp()
	return SP["token"]


def sp_get(path, **params):
	tok = sp_token()
	if not tok:
		raise PermissionError("not logged in to spotify")
	url = path if path.startswith("http") else "https://api.spotify.com/v1/" + path
	r = requests.get(url, params=params, headers={"Authorization": "Bearer " + tok}, timeout=15)
	r.raise_for_status()
	return r.json()


def sp_paged(path, **params):
	items = []
	offset = 0
	while True:
		batch = sp_get(path, limit=50, offset=offset, **params).get("items", [])
		items.extend(batch)
		offset += len(batch)
		if len(batch) < 50:
			return items


def playlist_items(sid):
	page = sp_get("playlists/" + sid).get("items")
	if isinstance(page, list):
		return page
	items = []
	while isinstance(page, dict):
		items.extend(page.get("items") or [])
		nxt = page.get("next")
		if not nxt:
			break
		page = sp_get(nxt)
	return items


@app.get("/api/spotify/status")
def spotify_status():
	if not SP.get("token"):
		return jsonify({"logged_in": False})
	try:
		me = sp_get("me")
		return jsonify({"logged_in": True, "name": me.get("display_name")})
	except Exception:
		return jsonify({"logged_in": False})


@app.get("/api/library")
def library():
	playlists = []
	for p in sp_paged("me/playlists"):
		if not p:
			continue
		count = (p.get("tracks") or {}).get("total")
		playlists.append({"id": p["id"], "name": p["name"], "count": count, "type": "playlist"})
	albums = [{"id": a["album"]["id"], "name": a["album"]["name"],
		"artist": a["album"]["artists"][0]["name"], "count": a["album"]["total_tracks"], "type": "album"}
		for a in sp_paged("me/albums") if a and a.get("album")]
	return jsonify({"playlists": playlists, "albums": albums})


@app.get("/api/tracks")
def tracks():
	kind = request.args.get("type")
	sid = request.args.get("id")
	out = []
	if kind == "album":
		album = sp_get("albums/" + sid)
		album_name = normalize(album["name"])
		album_date = album["release_date"]
		album_art = album["images"][0]["url"] if album["images"] else ""
		total = album["total_tracks"]
		for item in sp_paged("albums/" + sid + "/tracks"):
			tn = normalize(item["name"])
			out.append({
				"track_name": tn, "artist_name": normalize(item["artists"][0]["name"]),
				"album_name": album_name, "album_date": album_date, "album_art": album_art,
				"track_number": item["track_number"], "total_tracks": total,
				"duration_ms": item.get("duration_ms", 0), "file_name": tn,
			})
	else:
		for item in playlist_items(sid):
			if not item:
				continue
			t = item.get("item") or item.get("track") or item
			if not t.get("name") or t.get("type") not in (None, "track"):
				continue
			album = t.get("album") or {}
			tn = normalize(t["name"])
			out.append({
				"track_name": tn, "artist_name": normalize(t["artists"][0]["name"]),
				"album_name": normalize(album.get("name", "")),
				"album_date": album.get("release_date", ""),
				"album_art": album["images"][0]["url"] if album.get("images") else "",
				"track_number": 0, "total_tracks": None,
				"duration_ms": t.get("duration_ms", 0), "file_name": tn,
			})
		for i, t in enumerate(out):
			t["track_number"] = i + 1
			t["total_tracks"] = len(out)
	return jsonify({"tracks": out})


def find_archive_url(track, exclude):
	query = f"{track['artist_name']} {track['track_name']}"
	search = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode([
		("q", f"{query} AND mediatype:audio"),
		("fl[]", "identifier"), ("output", "json"), ("rows", "3"), ("columns", "1"),
	])
	try:
		with rq.urlopen(search, timeout=8) as resp:
			items = json.loads(resp.read()).get("response", {}).get("docs", [])
		name_lower = track["track_name"].lower()
		spotify_secs = track.get("duration_ms", 0) / 1000
		for item in items:
			identifier = item.get("identifier", "")
			with rq.urlopen(f"https://archive.org/metadata/{identifier}/files", timeout=8) as resp:
				files = json.loads(resp.read()).get("result", [])
			for f in files:
				fname = f.get("name", "")
				if name_lower in fname.lower() and fname.lower().endswith((".mp3", ".flac", ".ogg")):
					length = float(f.get("length") or 0)
					if spotify_secs and length and abs(length - spotify_secs) > 5:
						continue
					url = f"https://archive.org/download/{identifier}/{fname}"
					if url not in exclude:
						return url
	except Exception:
		pass
	return None


def find_youtube_url(track, exclude):
	query = f"{track['artist_name']} {track['album_name']} {track['track_name']}"
	skip_ids = {u.split("v=")[-1] for u in exclude if "v=" in u}
	with YoutubeDL({"quiet": True, "noprogress": True, "skip_download": True, "extract_flat": True, "remote_components": ["ejs:github"]}) as ydl:
		result = ydl.extract_info(f"ytsearch5:{query}", download=False)
		if not result or not result.get("entries"):
			return None
		entries = [e for e in result["entries"] if e and e.get("id") not in skip_ids]
		if not entries:
			return None
		for entry in entries:
			uploader = entry.get("uploader") or entry.get("channel") or ""
			if "- Topic" in uploader:
				return f"https://www.youtube.com/watch?v={entry['id']}"
		return f"https://www.youtube.com/watch?v={entries[0]['id']}"


@app.post("/api/find")
def api_find():
	data = request.json
	track = data["track"]
	exclude = data.get("exclude", [])
	url = None if exclude else find_archive_url(track, exclude)
	source = "archive" if url else "youtube"
	if not url:
		url = find_youtube_url(track, exclude)
	if not url:
		return jsonify({"url": None})
	return jsonify({"url": url, "source": source})


def set_progress(token, pct, stage):
	with PROG_LOCK:
		PROG[token] = {"pct": pct, "stage": stage}


@app.get("/api/progress/<token>")
def api_progress(token):
	with PROG_LOCK:
		p = PROG.get(token, {"pct": 0, "stage": "queued"})
		if p.get("stage") in ("done", "failed"):
			PROG.pop(token, None)
		return jsonify(p)


def tag_mp3(path, track):
	audiofile = eyed3.load(path)
	if not audiofile:
		return
	audiofile.initTag(version=eyed3.id3.ID3_V2_3)
	audiofile.tag.title = track["track_name"]
	audiofile.tag.artist = track["artist_name"]
	audiofile.tag.album = track["album_name"]
	audiofile.tag.album_artist = track["artist_name"]
	audiofile.tag.track_num = (track["track_number"], track["total_tracks"])
	try:
		audiofile.tag.recording_date = eyed3.core.Date(int(str(track["album_date"])[:4]))
	except (ValueError, TypeError):
		pass
	try:
		raw = rq.urlopen(track["album_art"]).read()
		buf = io.BytesIO()
		Image.open(io.BytesIO(raw)).convert("RGB").save(buf, "PNG", compress_level=0)
		audiofile.tag.images.set(eyed3.id3.frames.ImageFrame.FRONT_COVER, buf.getvalue(), "image/png")
	except Exception:
		pass
	audiofile.tag.save(version=eyed3.id3.ID3_V2_3)


def reencode(path, filters, bitrate):
	tmp = path + ".tmp.mp3"
	try:
		subprocess.run(
			["ffmpeg", "-i", path, "-af", ",".join(filters),
			 "-map_metadata", "0", "-id3v2_version", "3",
			 "-c:a", "libmp3lame", "-b:a", bitrate + "k", tmp, "-y"],
			check=True, capture_output=True, timeout=300)
		os.replace(tmp, path)
		return None
	except subprocess.CalledProcessError as e:
		if os.path.exists(tmp):
			os.remove(tmp)
		return e.stderr.decode()[-300:]
	except Exception as e:
		if os.path.exists(tmp):
			os.remove(tmp)
		return str(e)


def trim_silence(path, trim_start, trim_end, bitrate):
	filters = []
	if trim_start:
		filters.append("silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB")
	if trim_end:
		filters += ["areverse", "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB", "areverse"]
	if filters:
		reencode(path, filters, bitrate)


def mp3_duration(path):
	af = eyed3.load(path)
	if af and af.info:
		return af.info.time_secs
	return 0


@app.post("/api/prepare")
def api_prepare():
	data = request.json
	bitrate = str(data.get("bitrate", 128))
	if bitrate not in ("96", "128", "192", "256", "320"):
		bitrate = "128"
	try:
		ensure_space()
	except RuntimeError as e:
		return jsonify({"error": str(e)}), 507
	token = uuid.uuid4().hex
	pid = data.get("pid") or token
	if not re.fullmatch(r"[0-9a-f]{8,64}", pid):
		pid = token
	wdir = os.path.join(WORK, token)
	os.makedirs(wdir)
	set_progress(pid, 0, "downloading")
	threading.Thread(target=run_prepare, args=(data["track"], data["url"], token, pid, wdir, bitrate), daemon=True).start()
	return jsonify({"pid": pid})


def run_prepare(track, url, token, pid, wdir, bitrate):
	def hook(d):
		if d.get("status") == "downloading":
			total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
			if total:
				set_progress(pid, min(89, int(d.get("downloaded_bytes", 0) / total * 90)), "downloading")
		elif d.get("status") == "finished":
			set_progress(pid, 90, "converting")

	def pphook(d):
		if d.get("status") == "finished":
			set_progress(pid, 91, "converted")

	opts = {
		"format": "bestaudio/best",
		"outtmpl": f"{wdir}/raw.%(ext)s",
		"ignoreerrors": True,
		"progress_hooks": [hook],
		"postprocessor_hooks": [pphook],
		"quiet": True,
		"noprogress": True,
		"no_warnings": True,
		"socket_timeout": 30,
		"remote_components": ["ejs:github"],
		"postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate}],
	}
	try:
		with YoutubeDL(opts) as ydl:
			info = ydl.extract_info(url, download=True)
	except Exception:
		info = None
	mp3 = os.path.join(wdir, "raw.mp3")
	if not info or not os.path.exists(mp3):
		shutil.rmtree(wdir, ignore_errors=True)
		with PROG_LOCK:
			PROG[pid] = {"pct": 0, "stage": "failed", "error": "download failed"}
		return
	set_progress(pid, 92, "tagging")
	tag_mp3(mp3, track)
	cur = os.path.join(wdir, "cur.mp3")
	shutil.copy(mp3, os.path.join(wdir, "orig.mp3"))
	os.rename(mp3, cur)
	set_progress(pid, 95, "trimming")
	idx = track["track_number"]
	total = track["total_tracks"] or 0
	trim_silence(cur, idx != 1, idx != total, bitrate)
	track["bitrate"] = bitrate
	with open(os.path.join(wdir, "meta.json"), "w") as f:
		json.dump(track, f)
	dur = mp3_duration(cur)
	with PROG_LOCK:
		PROG[pid] = {"pct": 100, "stage": "done", "token": token, "duration": dur,
			"diff": abs(dur - track.get("duration_ms", 0) / 1000)}


def wdir_of(token):
	if not re.fullmatch(r"[0-9a-f]{32}", token):
		return None
	p = os.path.join(WORK, token)
	return p if os.path.isdir(p) else None


def read_meta(wdir):
	with open(os.path.join(wdir, "meta.json")) as f:
		return json.load(f)


@app.get("/api/audio/<token>")
def api_audio(token):
	wdir = wdir_of(token)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	return send_file(os.path.join(wdir, "cur.mp3"), mimetype="audio/mpeg", conditional=True)


@app.post("/api/restore/<token>")
def api_restore(token):
	wdir = wdir_of(token)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	shutil.copy(os.path.join(wdir, "orig.mp3"), os.path.join(wdir, "cur.mp3"))
	return jsonify({"duration": mp3_duration(os.path.join(wdir, "cur.mp3"))})


@app.post("/api/cut/<token>")
def api_cut(token):
	wdir = wdir_of(token)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	data = request.json
	start = max(0.0, float(data.get("start", 0)))
	end = float(data.get("end", 0))
	fadein = max(0.0, float(data.get("fadein", 0)))
	fadeout = max(0.0, float(data.get("fadeout", 0)))
	length = end - start
	if length <= 0:
		return jsonify({"error": "bad range"}), 400
	filters = [f"atrim=start={start}:end={end}", "asetpts=PTS-STARTPTS"]
	if fadein > 0:
		filters.append(f"afade=t=in:st=0:d={fadein}")
	if fadeout > 0:
		filters.append(f"afade=t=out:st={max(0, length - fadeout)}:d={fadeout}")
	cur = os.path.join(wdir, "cur.mp3")
	err = reencode(cur, filters, read_meta(wdir).get("bitrate", "128"))
	if err:
		return jsonify({"error": err}), 500
	return jsonify({"duration": mp3_duration(cur)})


def cleanup_response(resp, paths):
	def done():
		for p in paths:
			if os.path.isdir(p):
				shutil.rmtree(p, ignore_errors=True)
			elif os.path.exists(p):
				try:
					os.remove(p)
				except OSError:
					pass
	resp.call_on_close(done)
	threading.Timer(90, done).start()
	return resp


@app.post("/api/discard")
def api_discard():
	for token in (request.json or {}).get("tokens", []):
		wdir = wdir_of(token)
		if wdir:
			shutil.rmtree(wdir, ignore_errors=True)
	return jsonify({"ok": True})


@app.get("/api/download/<token>")
def api_download(token):
	wdir = wdir_of(token)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	name = normalize(read_meta(wdir)["file_name"]) + ".mp3"
	resp = send_file(os.path.join(wdir, "cur.mp3"), mimetype="audio/mpeg", as_attachment=True, download_name=name)
	return cleanup_response(resp, [wdir])


@app.post("/api/zip")
def api_zip():
	body = request.json or {}
	zname = normalize(body.get("name") or "tracks")
	try:
		ensure_space()
	except RuntimeError as e:
		return jsonify({"error": str(e)}), 507
	zid = uuid.uuid4().hex
	zpath = os.path.join(WORK, zid + ".zip")
	dirs = []
	used = set()
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
		for token in body.get("tokens", []):
			wdir = wdir_of(token)
			if not wdir:
				continue
			base = normalize(read_meta(wdir)["file_name"])
			name = base + ".mp3"
			n = 2
			while name in used:
				name = f"{base} ({n}).mp3"
				n += 1
			used.add(name)
			z.write(os.path.join(wdir, "cur.mp3"), name)
			dirs.append(wdir)
	for d in dirs:
		shutil.rmtree(d, ignore_errors=True)
	with open(os.path.join(WORK, zid + ".name"), "w") as f:
		f.write(zname)
	return jsonify({"zid": zid})


@app.get("/api/zipfile/<zid>")
def api_zipfile(zid):
	if not re.fullmatch(r"[0-9a-f]{32}", zid):
		return jsonify({"error": "bad id"}), 400
	zpath = os.path.join(WORK, zid + ".zip")
	npath = os.path.join(WORK, zid + ".name")
	if not os.path.exists(zpath):
		return jsonify({"error": "gone"}), 404
	zname = "tracks"
	if os.path.exists(npath):
		with open(npath) as f:
			zname = f.read().strip() or "tracks"
	resp = send_file(zpath, mimetype="application/zip", as_attachment=True, download_name=zname + ".zip")
	return cleanup_response(resp, [zpath, npath])


TEXT_FRAMES = {"artist": TPE1, "album_artist": TPE2, "album": TALB, "year": TDRC, "genre": TCON}


@app.post("/api/retag/start")
def retag_start():
	try:
		ensure_space()
	except RuntimeError as e:
		return jsonify({"error": str(e)}), 507
	body = request.json or {}
	count = body.get("count")
	if not isinstance(count, int) or count < 1 or count > 500:
		return jsonify({"error": "bad count"}), 400
	fields = body.get("fields") or {}
	meta = {"count": count, "number": bool(body.get("number", True)),
		"fields": {k: str(v) for k, v in fields.items() if k in TEXT_FRAMES and v is not None}}
	sid = uuid.uuid4().hex
	os.makedirs(os.path.join(WORK, sid))
	with open(os.path.join(WORK, sid, "meta.json"), "w") as f:
		json.dump(meta, f)
	return jsonify({"sid": sid})


@app.post("/api/retag/cover/<sid>")
def retag_cover(sid):
	wdir = wdir_of(sid)
	if not wdir or "cover" not in request.files:
		return jsonify({"error": "gone"}), 404
	try:
		Image.open(request.files["cover"].stream).convert("RGB").save(os.path.join(wdir, "cover.png"), "PNG", compress_level=0)
	except Exception:
		return jsonify({"error": "bad cover image"}), 400
	return jsonify({"ok": True})


@app.post("/api/retag/file/<sid>/<int:i>")
def retag_file(sid, i):
	wdir = wdir_of(sid)
	if not wdir or "file" not in request.files:
		return jsonify({"error": "gone"}), 404
	meta = read_meta(wdir)
	if i < 0 or i >= meta["count"]:
		return jsonify({"error": "bad index"}), 400
	f = request.files["file"]
	fp = os.path.join(wdir, f"{i}.mp3")
	f.save(fp)
	title = request.form.get("title") or os.path.splitext(f.filename or "")[0] or f"track {i + 1}"
	try:
		try:
			tags = ID3(fp)
		except ID3NoHeaderError:
			tags = ID3()
		tags.delete(fp)
		tags.clear()
		tags["TIT2"] = TIT2(encoding=3, text=title)
		for key, frame in TEXT_FRAMES.items():
			if key in meta["fields"]:
				tags[frame.__name__] = frame(encoding=3, text=meta["fields"][key])
		if meta["number"]:
			tags["TRCK"] = TRCK(encoding=3, text=f"{i + 1}/{meta['count']}")
		cover = os.path.join(wdir, "cover.png")
		if os.path.exists(cover):
			with open(cover, "rb") as c:
				tags["APIC"] = APIC(encoding=3, mime="image/png", type=3, desc="", data=c.read())
		tags.save(fp, v2_version=3, v1=0)
	except Exception as e:
		os.remove(fp)
		return jsonify({"error": str(e) or "tagging failed"}), 400
	with open(fp + ".name", "w") as n:
		n.write(title)
	return jsonify({"ok": True})


@app.get("/api/retag/zip/<sid>")
def retag_zip(sid):
	wdir = wdir_of(sid)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	meta = read_meta(wdir)
	width = len(str(meta["count"]))
	zpath = wdir + ".zip"
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
		for i in range(meta["count"]):
			fp = os.path.join(wdir, f"{i}.mp3")
			if not os.path.exists(fp):
				continue
			with open(fp + ".name") as n:
				title = n.read()
			z.write(fp, f"{str(i + 1).zfill(width)} {normalize(title)}.mp3")
	zname = normalize(meta["fields"].get("album") or "retagged") + ".zip"
	resp = send_file(zpath, mimetype="application/zip", as_attachment=True, download_name=zname)
	return cleanup_response(resp, [wdir, zpath])


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ipod uploader</title>
</head>
<body>
<h1>ipod uploader</h1>
<div id="main">
	<button id="tabDownloadBtn">download</button>
	<button id="tabRetagBtn">retag</button>
	<hr>
	<div id="tabDownload">
		<div id="spotifyAuth">
			<button id="spotifyLoginBtn">log in to spotify</button>
			<span id="spotifyUser"></span>
		</div>
		<table width="100%"><tr>
		<td width="30%" valign="top">
		<div id="library" hidden>
			<h3>playlists</h3>
			<ul id="playlistList"></ul>
			<h3>liked albums</h3>
			<ul id="albumList"></ul>
		</div>
		</td>
		<td valign="top">
		<div id="collection" hidden>
			<h2 id="collectionName"></h2>
			<button id="downloadAllBtn">download all</button>
			<label>bitrate <select id="bitrate">
				<option value="96">96 kbps</option>
				<option value="128" selected>128 kbps</option>
				<option value="192">192 kbps</option>
				<option value="256">256 kbps</option>
				<option value="320">320 kbps</option>
			</select></label>
			<span id="overallProgress"></span>
			<table border="1" cellpadding="4">
				<thead>
					<tr><th>#</th><th>track</th><th>source</th><th>status</th><th>progress</th><th>actions</th></tr>
				</thead>
				<tbody id="trackRows"></tbody>
			</table>
		</div>
		<div id="cutter" hidden>
			<h3 id="cutterTitle"></h3>
			<canvas id="wave" width="1200" height="180"></canvas>
			<br>
			<button id="playBtn">play</button>
			<button id="zoomInBtn">zoom in</button>
			<button id="zoomOutBtn">zoom out</button>
			<button id="zoomFitBtn">fit</button>
			<span id="cursorTime"></span>
			<br>
			start <input type="number" id="cutStart" step="0.01" min="0" size="8">
			end <input type="number" id="cutEnd" step="0.01" min="0" size="8">
			fade in <input type="number" id="fadeIn" step="0.1" min="0" value="0" size="5">
			fade out <input type="number" id="fadeOut" step="0.1" min="0" value="0" size="5">
			<br>
			<button id="setStartBtn">set start to cursor</button>
			<button id="setEndBtn">set end to cursor</button>
			<button id="applyCutBtn">apply cut</button>
			<button id="restoreBtn">restore original (undo trim/cuts)</button>
			<button id="closeCutterBtn">close</button>
			<span id="cutMsg"></span>
		</div>
		</td>
		</tr></table>
	</div>
	<div id="tabRetag" hidden>
		<p><input type="file" id="retagFiles" accept=".mp3" multiple></p>
		<table border="1" cellpadding="4">
			<tbody id="retagRows"></tbody>
		</table>
		<p>
			<label><input type="checkbox" id="incArtist" checked> artist</label>
			<input type="text" id="fArtist">
		</p>
		<p>
			<label><input type="checkbox" id="incAlbumArtist" checked> album artist</label>
			<input type="text" id="fAlbumArtist" placeholder="same as artist if blank">
		</p>
		<p>
			<label><input type="checkbox" id="incAlbum" checked> album</label>
			<input type="text" id="fAlbum">
		</p>
		<p>
			<label><input type="checkbox" id="incYear"> year</label>
			<input type="number" id="fYear" size="6">
		</p>
		<p>
			<label><input type="checkbox" id="incGenre"> genre</label>
			<input type="text" id="fGenre">
		</p>
		<p>
			<label><input type="checkbox" id="incNumber" checked> track numbers</label>
		</p>
		<p>
			<label><input type="checkbox" id="incCover" checked> cover art</label>
			file <input type="file" id="fCoverFile" accept="image/*">
		</p>
		<p>
			<button id="retagGoBtn">retag &amp; download zip</button>
			<span id="retagMsg"></span>
		</p>
	</div>
</div>
<script>
var $ = function(id) { return document.getElementById(id); };

function api(path, opts) {
	opts = opts || {};
	if (opts.json) {
		opts.method = opts.method || "POST";
		opts.headers = {"Content-Type": "application/json"};
		opts.body = JSON.stringify(opts.json);
	}
	return fetch(path, opts).then(function(r) {
		return r.json().then(function(data) {
			if (!r.ok) throw new Error(data.error || r.status);
			return data;
		});
	});
}

$("tabDownloadBtn").onclick = function() {
	$("tabDownload").hidden = false;
	$("tabRetag").hidden = true;
};
$("tabRetagBtn").onclick = function() {
	$("tabDownload").hidden = true;
	$("tabRetag").hidden = false;
};

api("/api/spotify/status").then(function(d) {
	if (d.logged_in) {
		$("spotifyLoginBtn").hidden = true;
		$("spotifyUser").textContent = "logged in as " + (d.name || "?");
		loadLibrary();
	}
}).catch(function() {});

$("spotifyLoginBtn").onclick = function() {
	api("/api/spotify/login").then(function(d) {
		location.href = d.url;
	});
};

function loadLibrary() {
	api("/api/library").then(function(d) {
		$("library").hidden = false;
		fillList($("playlistList"), d.playlists);
		fillList($("albumList"), d.albums);
	}).catch(function(e) {
		$("spotifyUser").textContent = "library failed: " + e.message;
	});
}

function fillList(ul, items) {
	ul.innerHTML = "";
	items.forEach(function(item) {
		var li = document.createElement("li");
		var btn = document.createElement("button");
		var label = item.name;
		if (item.count !== null && item.count !== undefined) label += " (" + item.count + ")";
		if (item.artist) label = item.artist + " - " + label;
		btn.textContent = label;
		btn.onclick = function() { openCollection(item); };
		li.appendChild(btn);
		ul.appendChild(li);
	});
}

var rows = [];
var findQueue = [];
var findActive = 0;
var prepQueue = [];
var prepActive = 0;
var collectionName = "tracks";

function tokens() {
	return rows.filter(function(r) { return r.token; }).map(function(r) { return r.token; });
}

function discardAll() {
	var t = tokens();
	if (t.length) api("/api/discard", {json: {tokens: t}}).catch(function() {});
}

window.addEventListener("pagehide", function() {
	var t = tokens();
	if (t.length) navigator.sendBeacon("/api/discard", new Blob([JSON.stringify({tokens: t})], {type: "application/json"}));
});

function openCollection(item) {
	discardAll();
	collectionName = item.name;
	$("collection").hidden = false;
	$("cutter").hidden = true;
	$("collectionName").textContent = item.name;
	$("trackRows").innerHTML = "";
	$("overallProgress").textContent = "loading tracks...";
	rows = [];
	findQueue = [];
	prepQueue = [];
	api("/api/tracks?type=" + item.type + "&id=" + item.id).then(function(d) {
		d.tracks.forEach(addRow);
		if (!rows.length) {
			$("overallProgress").textContent = "no tracks found";
			return;
		}
		updateOverall();
		rows.forEach(queueFind);
	}).catch(function(e) {
		$("overallProgress").textContent = "failed: " + e.message;
	});
}

function addRow(track) {
	var tr = document.createElement("tr");
	tr.innerHTML = "<td>" + track.track_number + "</td><td></td><td></td><td>searching</td><td></td><td></td>";
	tr.children[1].textContent = track.artist_name + " - " + track.track_name;
	$("trackRows").appendChild(tr);
	rows.push({
		track: track, tr: tr, url: null, tried: [], token: null,
		status: "searching", diff: null,
		sourceCell: tr.children[2], statusCell: tr.children[3],
		progressCell: tr.children[4], actionCell: tr.children[5]
	});
}

function queueFind(row) {
	findQueue.push(row);
	pumpFind();
}

function pumpFind() {
	while (findActive < 3 && findQueue.length) {
		findActive++;
		runFind(findQueue.shift());
	}
}

function runFind(row) {
	row.status = "searching";
	row.statusCell.textContent = "searching";
	api("/api/find", {json: {track: row.track, exclude: row.tried}}).then(function(d) {
		findActive--;
		if (!d.url) {
			row.status = "missing";
			row.statusCell.textContent = "no result";
			renderActions(row);
			updateOverall();
			pumpFind();
			return;
		}
		row.url = d.url;
		row.tried.push(d.url);
		var a = document.createElement("a");
		a.href = d.url;
		a.target = "_blank";
		a.textContent = d.source;
		row.sourceCell.innerHTML = "";
		row.sourceCell.appendChild(a);
		pumpFind();
		row.status = "queued";
		row.statusCell.textContent = "queued";
		prepQueue.push(row);
		pumpPrepare();
	}).catch(function(e) {
		findActive--;
		row.status = "missing";
		row.statusCell.textContent = "error: " + e.message;
		renderActions(row);
		updateOverall();
		pumpFind();
	});
}

function pumpPrepare() {
	while (prepActive < 2 && prepQueue.length) {
		prepActive++;
		prepare(prepQueue.shift());
	}
}

function prepare(row) {
	row.status = "downloading";
	row.statusCell.textContent = "downloading";
	var pid = "";
	for (var i = 0; i < 32; i++) pid += "0123456789abcdef"[Math.floor(Math.random() * 16)];
	function settle() {
		renderActions(row);
		updateOverall();
		prepActive--;
		pumpPrepare();
	}
	function finish(d) {
		row.token = d.token;
		row.diff = d.diff;
		row.progressCell.textContent = "100%";
		if (d.diff <= 1) {
			row.status = "ok";
			row.statusCell.textContent = "ok";
		} else if (d.diff <= 2) {
			row.status = "warn";
			row.statusCell.textContent = "? " + d.diff.toFixed(1) + "s off";
		} else {
			row.status = "bad";
			row.statusCell.textContent = "\u2717 " + d.diff.toFixed(1) + "s off";
		}
		settle();
	}
	function fail(msg) {
		row.status = "missing";
		row.statusCell.textContent = "failed: " + msg;
		settle();
	}
	var stalls = 0;
	var poll = setInterval(function() {
		api("/api/progress/" + pid).then(function(p) {
			if (p.stage === "done") {
				clearInterval(poll);
				finish(p);
				return;
			}
			if (p.stage === "failed") {
				clearInterval(poll);
				fail(p.error || "download failed");
				return;
			}
			if (p.stage === "queued") {
				stalls++;
				if (stalls > 300) {
					clearInterval(poll);
					fail("timed out");
					return;
				}
			} else {
				stalls = 0;
			}
			row.progressCell.textContent = p.pct + "% " + p.stage;
		}).catch(function() {});
	}, 1000);
	api("/api/prepare", {json: {track: row.track, url: row.url, pid: pid, bitrate: $("bitrate").value}}).catch(function(e) {
		clearInterval(poll);
		fail(e.message);
	});
}

function renderActions(row) {
	row.actionCell.innerHTML = "";
	var retry = document.createElement("button");
	retry.textContent = "retry";
	retry.onclick = function() {
		if (row.token) api("/api/discard", {json: {tokens: [row.token]}}).catch(function() {});
		row.token = null;
		row.progressCell.textContent = "";
		queueFind(row);
	};
	row.actionCell.appendChild(retry);
	if (!row.token) return;
	var cut = document.createElement("button");
	cut.textContent = "cut";
	cut.onclick = function() { openCutter(row); };
	row.actionCell.appendChild(cut);
	var dl = document.createElement("button");
	dl.textContent = "download";
	dl.onclick = function() {
		location.href = "/api/download/" + row.token;
		row.token = null;
		row.statusCell.textContent = "downloaded";
		row.actionCell.innerHTML = "";
		updateOverall();
	};
	row.actionCell.appendChild(dl);
}

function updateOverall() {
	var done = rows.filter(function(r) { return r.token || r.status === "missing"; }).length;
	var ready = tokens().length;
	if (rows.length) {
		$("overallProgress").textContent = Math.round(done / rows.length * 100) + "% (" + ready + "/" + rows.length + " ready)";
	}
}

$("downloadAllBtn").onclick = function() {
	var t = tokens();
	if (!t.length) return;
	api("/api/zip", {json: {tokens: t, name: collectionName}}).then(function(d) {
		location.href = "/api/zipfile/" + d.zid;
		rows.forEach(function(r) {
			if (r.token) {
				r.token = null;
				r.statusCell.textContent = "downloaded";
				r.actionCell.innerHTML = "";
			}
		});
	}).catch(function(e) {
		$("overallProgress").textContent = e.message;
	});
};

var cutter = {
	row: null, buffer: null, ctx: null, source: null,
	viewStart: 0, viewLen: 0, cursor: 0, playing: false,
	playStartedAt: 0, playOffset: 0, drag: null
};
var canvas = $("wave");
var cctx = canvas.getContext("2d");

function openCutter(row) {
	stopPlayback();
	cutter.row = row;
	$("cutter").hidden = false;
	$("cutterTitle").textContent = row.track.artist_name + " - " + row.track.track_name;
	$("cutMsg").textContent = "loading audio...";
	loadCutterAudio();
}

function loadCutterAudio() {
	fetch("/api/audio/" + cutter.row.token).then(function(r) {
		if (!r.ok) throw new Error("audio fetch failed");
		return r.arrayBuffer();
	}).then(function(buf) {
		if (!cutter.ctx) cutter.ctx = new AudioContext();
		return cutter.ctx.decodeAudioData(buf);
	}).then(function(audio) {
		cutter.buffer = audio;
		cutter.viewStart = 0;
		cutter.viewLen = audio.duration;
		cutter.cursor = 0;
		$("cutStart").value = 0;
		$("cutEnd").value = audio.duration.toFixed(2);
		$("cutMsg").textContent = "";
		drawWave();
	}).catch(function(e) {
		$("cutMsg").textContent = e.message;
	});
}

function timeToX(t) {
	return (t - cutter.viewStart) / cutter.viewLen * canvas.width;
}

function xToTime(x) {
	return cutter.viewStart + x / canvas.width * cutter.viewLen;
}

function drawWave() {
	var b = cutter.buffer;
	if (!b) return;
	cctx.fillStyle = "#fff";
	cctx.fillRect(0, 0, canvas.width, canvas.height);
	var data = b.getChannelData(0);
	var rate = b.sampleRate;
	var mid = canvas.height / 2;
	cctx.fillStyle = "#888";
	for (var x = 0; x < canvas.width; x++) {
		var s0 = Math.floor((cutter.viewStart + x / canvas.width * cutter.viewLen) * rate);
		var s1 = Math.floor((cutter.viewStart + (x + 1) / canvas.width * cutter.viewLen) * rate);
		if (s0 >= data.length) break;
		if (s1 > data.length) s1 = data.length;
		var min = 1, max = -1;
		var step = Math.max(1, Math.floor((s1 - s0) / 50));
		for (var s = s0; s < s1; s += step) {
			var v = data[s];
			if (v < min) min = v;
			if (v > max) max = v;
		}
		cctx.fillRect(x, mid - max * mid, 1, Math.max(1, (max - min) * mid));
	}
	var cs = parseFloat($("cutStart").value) || 0;
	var ce = parseFloat($("cutEnd").value) || b.duration;
	cctx.fillStyle = "rgba(255,0,0,0.15)";
	var xs = timeToX(cs);
	var xe = timeToX(ce);
	if (xs > 0) cctx.fillRect(0, 0, Math.min(xs, canvas.width), canvas.height);
	if (xe < canvas.width) cctx.fillRect(Math.max(xe, 0), 0, canvas.width - xe, canvas.height);
	cctx.fillStyle = "#f00";
	cctx.fillRect(xs - 1, 0, 3, canvas.height);
	cctx.fillRect(xe - 1, 0, 3, canvas.height);
	var fi = parseFloat($("fadeIn").value) || 0;
	var fo = parseFloat($("fadeOut").value) || 0;
	cctx.strokeStyle = "#00f";
	if (fi > 0) {
		cctx.beginPath();
		cctx.moveTo(xs, canvas.height);
		cctx.lineTo(timeToX(cs + fi), 0);
		cctx.stroke();
	}
	if (fo > 0) {
		cctx.beginPath();
		cctx.moveTo(timeToX(ce - fo), 0);
		cctx.lineTo(xe, canvas.height);
		cctx.stroke();
	}
	var pos = cutter.playing ? playPosition() : cutter.cursor;
	cctx.fillStyle = "#000";
	cctx.fillRect(timeToX(pos), 0, 1, canvas.height);
	$("cursorTime").textContent = pos.toFixed(2) + "s / " + b.duration.toFixed(2) + "s";
}

function playPosition() {
	return cutter.playOffset + (cutter.ctx.currentTime - cutter.playStartedAt);
}

function canvasX(e) {
	var rect = canvas.getBoundingClientRect();
	return (e.clientX - rect.left) * canvas.width / rect.width;
}

canvas.onmousedown = function(e) {
	if (!cutter.buffer) return;
	var x = canvasX(e);
	var cs = timeToX(parseFloat($("cutStart").value) || 0);
	var ce = timeToX(parseFloat($("cutEnd").value) || cutter.buffer.duration);
	if (Math.abs(x - cs) < 8) {
		cutter.drag = "start";
	} else if (Math.abs(x - ce) < 8) {
		cutter.drag = "end";
	} else {
		cutter.drag = "cursor";
		cutter.cursor = Math.max(0, Math.min(cutter.buffer.duration, xToTime(x)));
		if (cutter.playing) {
			stopPlayback();
			startPlayback();
		}
	}
	drawWave();
};

window.onmousemove = function(e) {
	if (!cutter.drag || !cutter.buffer) return;
	var t = Math.max(0, Math.min(cutter.buffer.duration, xToTime(canvasX(e))));
	if (cutter.drag === "start") {
		$("cutStart").value = t.toFixed(2);
	} else if (cutter.drag === "end") {
		$("cutEnd").value = t.toFixed(2);
	} else {
		cutter.cursor = t;
	}
	drawWave();
};

window.onmouseup = function() {
	cutter.drag = null;
};

canvas.onwheel = function(e) {
	if (!cutter.buffer) return;
	e.preventDefault();
	zoomAround(xToTime(canvasX(e)), e.deltaY < 0 ? 0.8 : 1.25);
};

function zoomAround(anchor, factor) {
	var newLen = Math.min(cutter.buffer.duration, Math.max(0.2, cutter.viewLen * factor));
	var frac = (anchor - cutter.viewStart) / cutter.viewLen;
	cutter.viewStart = Math.max(0, Math.min(cutter.buffer.duration - newLen, anchor - frac * newLen));
	cutter.viewLen = newLen;
	drawWave();
}

$("zoomInBtn").onclick = function() {
	if (cutter.buffer) zoomAround(cutter.viewStart + cutter.viewLen / 2, 0.5);
};
$("zoomOutBtn").onclick = function() {
	if (cutter.buffer) zoomAround(cutter.viewStart + cutter.viewLen / 2, 2);
};
$("zoomFitBtn").onclick = function() {
	if (!cutter.buffer) return;
	cutter.viewStart = 0;
	cutter.viewLen = cutter.buffer.duration;
	drawWave();
};

$("cutStart").oninput = drawWave;
$("cutEnd").oninput = drawWave;
$("fadeIn").oninput = drawWave;
$("fadeOut").oninput = drawWave;

$("setStartBtn").onclick = function() {
	$("cutStart").value = cutter.cursor.toFixed(2);
	drawWave();
};
$("setEndBtn").onclick = function() {
	$("cutEnd").value = cutter.cursor.toFixed(2);
	drawWave();
};

function startPlayback() {
	var src = cutter.ctx.createBufferSource();
	src.buffer = cutter.buffer;
	src.connect(cutter.ctx.destination);
	src.start(0, cutter.cursor);
	cutter.source = src;
	cutter.playing = true;
	cutter.playOffset = cutter.cursor;
	cutter.playStartedAt = cutter.ctx.currentTime;
	$("playBtn").textContent = "pause";
	src.onended = function() {
		if (cutter.source === src) stopPlayback();
	};
	animatePlayhead();
}

function stopPlayback() {
	if (cutter.source) {
		cutter.source.onended = null;
		cutter.source.stop();
		cutter.source = null;
	}
	if (cutter.playing) cutter.cursor = Math.min(cutter.buffer.duration, playPosition());
	cutter.playing = false;
	$("playBtn").textContent = "play";
	drawWave();
}

function animatePlayhead() {
	if (!cutter.playing) return;
	drawWave();
	requestAnimationFrame(animatePlayhead);
}

$("playBtn").onclick = function() {
	if (!cutter.buffer) return;
	if (cutter.playing) {
		stopPlayback();
	} else {
		if (cutter.ctx.state === "suspended") cutter.ctx.resume();
		startPlayback();
	}
};

$("applyCutBtn").onclick = function() {
	if (!cutter.row) return;
	stopPlayback();
	$("cutMsg").textContent = "cutting...";
	api("/api/cut/" + cutter.row.token, {json: {
		start: parseFloat($("cutStart").value) || 0,
		end: parseFloat($("cutEnd").value) || cutter.buffer.duration,
		fadein: parseFloat($("fadeIn").value) || 0,
		fadeout: parseFloat($("fadeOut").value) || 0
	}}).then(function() {
		$("fadeIn").value = 0;
		$("fadeOut").value = 0;
		loadCutterAudio();
	}).catch(function(e) {
		$("cutMsg").textContent = e.message;
	});
};

$("restoreBtn").onclick = function() {
	if (!cutter.row) return;
	stopPlayback();
	$("cutMsg").textContent = "restoring...";
	api("/api/restore/" + cutter.row.token, {method: "POST"}).then(loadCutterAudio).catch(function(e) {
		$("cutMsg").textContent = e.message;
	});
};

$("closeCutterBtn").onclick = function() {
	stopPlayback();
	$("cutter").hidden = true;
	cutter.row = null;
	cutter.buffer = null;
};

var retagFiles = [];

$("retagFiles").onchange = function() {
	retagFiles = Array.from(this.files);
	renderRetagRows();
};

function moveFile(from, to) {
	var moved = retagFiles.splice(from, 1)[0];
	retagFiles.splice(to, 0, moved);
	renderRetagRows();
}

function renderRetagRows() {
	var tbody = $("retagRows");
	tbody.innerHTML = "";
	retagFiles.forEach(function(f, i) {
		var tr = document.createElement("tr");
		tr.draggable = true;
		var num = document.createElement("td");
		var numInput = document.createElement("input");
		numInput.type = "number";
		numInput.min = 1;
		numInput.max = retagFiles.length;
		numInput.value = i + 1;
		numInput.size = 3;
		numInput.onchange = function() {
			var target = parseInt(numInput.value, 10) - 1;
			if (isNaN(target) || target < 0 || target >= retagFiles.length) {
				renderRetagRows();
				return;
			}
			moveFile(i, target);
		};
		num.appendChild(numInput);
		var name = document.createElement("td");
		name.textContent = f.name;
		var title = document.createElement("td");
		var titleInput = document.createElement("input");
		titleInput.type = "text";
		titleInput.size = 40;
		titleInput.value = f.retagTitle !== undefined ? f.retagTitle : guessTitle(f.name);
		titleInput.oninput = function() { f.retagTitle = titleInput.value; };
		f.retagTitle = titleInput.value;
		title.appendChild(titleInput);
		var grip = document.createElement("td");
		grip.textContent = "\u21c5 drag";
		tr.appendChild(num);
		tr.appendChild(grip);
		tr.appendChild(name);
		tr.appendChild(title);
		tr.ondragstart = function(e) { e.dataTransfer.setData("text/plain", i); };
		tr.ondragover = function(e) { e.preventDefault(); };
		tr.ondrop = function(e) {
			e.preventDefault();
			var from = parseInt(e.dataTransfer.getData("text/plain"), 10);
			if (!isNaN(from) && from !== i) moveFile(from, i);
		};
		tbody.appendChild(tr);
	});
}

function guessTitle(name) {
	var stem = name.replace(/\.mp3$/i, "");
	var m = stem.match(/^(\d+)[.\s\-]+(.+)$/);
	return m ? m[2].trim() : stem;
}

[["incArtist", "fArtist"], ["incAlbumArtist", "fAlbumArtist"], ["incAlbum", "fAlbum"], ["incYear", "fYear"], ["incGenre", "fGenre"]].forEach(function(pair) {
	$(pair[0]).onchange = function() { $(pair[1]).disabled = !this.checked; };
});
$("incCover").onchange = function() {
	$("fCoverFile").disabled = !this.checked;
};
$("fYear").disabled = true;
$("fGenre").disabled = true;

function upload(path, fd) {
	return fetch(path, {method: "POST", body: fd}).then(function(r) {
		return r.json().then(function(d) {
			if (!r.ok) throw new Error(d.error || r.status);
			return d;
		});
	});
}

$("retagGoBtn").onclick = function() {
	if (!retagFiles.length) {
		$("retagMsg").textContent = "no files";
		return;
	}
	var fields = {};
	if ($("incArtist").checked) fields.artist = $("fArtist").value;
	if ($("incAlbumArtist").checked) fields.album_artist = $("fAlbumArtist").value || $("fArtist").value;
	if ($("incAlbum").checked) fields.album = $("fAlbum").value;
	if ($("incYear").checked && $("fYear").value) fields.year = $("fYear").value;
	if ($("incGenre").checked && $("fGenre").value) fields.genre = $("fGenre").value;
	var cover = $("incCover").checked ? $("fCoverFile").files[0] : null;
	var btn = this;
	btn.disabled = true;
	$("retagMsg").textContent = "starting...";
	var sid;
	api("/api/retag/start", {json: {fields: fields, number: $("incNumber").checked, count: retagFiles.length}}).then(function(d) {
		sid = d.sid;
		if (!cover) return;
		var fd = new FormData();
		fd.append("cover", cover);
		return upload("/api/retag/cover/" + sid, fd);
	}).then(function() {
		return retagFiles.reduce(function(chain, f, i) {
			return chain.then(function() {
				$("retagMsg").textContent = "tagging " + (i + 1) + "/" + retagFiles.length;
				var fd = new FormData();
				fd.append("file", f);
				fd.append("title", f.retagTitle || guessTitle(f.name));
				return upload("/api/retag/file/" + sid + "/" + i, fd);
			});
		}, Promise.resolve());
	}).then(function() {
		$("retagMsg").textContent = "done";
		location.href = "/api/retag/zip/" + sid;
	}).catch(function(e) {
		$("retagMsg").textContent = "failed: " + e.message;
		if (sid) api("/api/discard", {json: {tokens: [sid]}}).catch(function() {});
	}).then(function() {
		btn.disabled = false;
	});
};
</script>
</body>
</html>
"""


@app.get("/")
def root():
	return Response(HTML, mimetype="text/html")


if __name__ == "__main__":
	url = f"http://127.0.0.1:{PORT}"
	print(f"\nServer running! Go to {url}")
	print("Press Ctrl-C to stop.")
	threading.Timer(1, lambda: webbrowser.open(url)).start()
	import flask.cli
	flask.cli.show_server_banner = lambda *a, **k: None
	try:
		app.run(host="127.0.0.1", port=PORT, threaded=True)
	except KeyboardInterrupt:
		pass
	shutil.rmtree(WORK, ignore_errors=True)
