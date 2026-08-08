import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
import zipfile
from urllib import request as rq

import eyed3
import requests
from PIL import Image
from flask import Flask, jsonify, redirect, request, send_file, session
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TCON, APIC
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
GATE_PASSWORD = "yippee"
WORK = os.path.join(os.path.dirname(__file__), "work")
MIN_FREE = 700 * 1024 * 1024
MAX_AGE = 3600

os.makedirs(WORK, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-" + CLIENT_ID if CLIENT_ID else "dev")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

PROG = {}
PROG_LOCK = threading.Lock()


def sweep():
	while True:
		now = time.time()
		try:
			for name in os.listdir(WORK):
				p = os.path.join(WORK, name)
				if now - os.path.getmtime(p) > MAX_AGE:
					shutil.rmtree(p, ignore_errors=True)
		except OSError:
			pass
		time.sleep(600)


threading.Thread(target=sweep, daemon=True).start()


def ensure_space():
	if shutil.disk_usage(WORK).free < MIN_FREE:
		for name in sorted(os.listdir(WORK), key=lambda n: os.path.getmtime(os.path.join(WORK, n))):
			shutil.rmtree(os.path.join(WORK, name), ignore_errors=True)
			if shutil.disk_usage(WORK).free >= MIN_FREE:
				return
		raise RuntimeError("server out of disk space")


def normalize(s):
	s = s.translate(str.maketrans('\\/:*?"<>|', "__       "))
	s = re.sub(r'[!@#$%^&+=\[\]{};\'`,~]', '', s)
	return s.strip()


@app.before_request
def gate():
	path = request.path
	if path.startswith("/api") and path != "/api/gate" and not session.get("ok"):
		return jsonify({"error": "locked"}), 401


@app.post("/api/gate")
def api_gate():
	if (request.json or {}).get("password") == GATE_PASSWORD:
		session["ok"] = True
		session.permanent = True
		return jsonify({"ok": True})
	return jsonify({"error": "wrong password"}), 403


def redirect_uri():
	root = request.host_url
	if "127.0.0.1" not in root and "localhost" not in root:
		root = root.replace("http://", "https://")
	return root + "callback"


@app.get("/api/spotify/login")
def spotify_login():
	params = urllib.parse.urlencode({
		"client_id": CLIENT_ID,
		"response_type": "code",
		"redirect_uri": redirect_uri(),
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
		"redirect_uri": redirect_uri(),
		"client_id": CLIENT_ID,
		"client_secret": CLIENT_SECRET,
	}, timeout=15)
	tok = r.json()
	if "access_token" in tok:
		session["sp_token"] = tok["access_token"]
		session["sp_refresh"] = tok.get("refresh_token")
		session["sp_exp"] = time.time() + tok.get("expires_in", 3600) - 60
	return redirect("/")


def sp_token():
	if not session.get("sp_token"):
		return None
	if time.time() > session.get("sp_exp", 0) and session.get("sp_refresh"):
		r = requests.post("https://accounts.spotify.com/api/token", data={
			"grant_type": "refresh_token",
			"refresh_token": session["sp_refresh"],
			"client_id": CLIENT_ID,
			"client_secret": CLIENT_SECRET,
		}, timeout=15)
		tok = r.json()
		if "access_token" in tok:
			session["sp_token"] = tok["access_token"]
			session["sp_exp"] = time.time() + tok.get("expires_in", 3600) - 60
	return session["sp_token"]


def sp_get(path, **params):
	tok = sp_token()
	if not tok:
		raise PermissionError("not logged in to spotify")
	r = requests.get("https://api.spotify.com/v1/" + path, params=params,
		headers={"Authorization": "Bearer " + tok}, timeout=15)
	r.raise_for_status()
	return r.json()


def sp_paged(path, key=None, **params):
	items = []
	offset = 0
	while True:
		data = sp_get(path, limit=50, offset=offset, **params)
		if key:
			data = data[key]
		batch = data.get("items", [])
		items.extend(batch)
		offset += len(batch)
		if len(batch) < 50:
			return items


@app.get("/api/spotify/status")
def spotify_status():
	if not session.get("sp_token"):
		return jsonify({"logged_in": False})
	try:
		me = sp_get("me")
		return jsonify({"logged_in": True, "name": me.get("display_name")})
	except Exception:
		return jsonify({"logged_in": False})


@app.get("/api/debug/<pid>")
def api_debug(pid):
	out = {}
	for label, path, params in [
		("detail", "playlists/" + pid, {}),
		("detail_fields", "playlists/" + pid, {"fields": "tracks.total"}),
		("tracks", "playlists/" + pid + "/tracks", {"limit": 1}),
	]:
		try:
			d = sp_get(path, **params)
			out[label] = {"keys": sorted(d.keys()), "total": d.get("total"), "tracks": d.get("tracks")}
		except Exception as e:
			out[label] = str(e)
	return jsonify(out)


@app.get("/api/library")
def library():
	playlists = []
	for p in sp_paged("me/playlists"):
		if not p:
			continue
		count = (p.get("tracks") or {}).get("total")
		if count is None:
			try:
				count = sp_get(f"playlists/{p['id']}", fields="tracks.total")["tracks"]["total"]
			except Exception as e:
				print(f"count fail {p['name']}: {e}", flush=True)
				count = None
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
		for item in sp_paged("playlists/" + sid + "/tracks", additional_types="track"):
			t = item.get("track")
			if not t or t.get("type") != "track":
				continue
			tn = normalize(t["name"])
			out.append({
				"track_name": tn, "artist_name": normalize(t["artists"][0]["name"]),
				"album_name": normalize(t["album"]["name"]),
				"album_date": t["album"]["release_date"],
				"album_art": t["album"]["images"][0]["url"] if t["album"]["images"] else "",
				"track_number": t["track_number"], "total_tracks": None,
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
	skip_ids = set()
	for u in exclude:
		if "v=" in u:
			skip_ids.add(u.split("v=")[-1])
	with YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": True}) as ydl:
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
	url = None
	if not exclude:
		url = find_archive_url(track, exclude)
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
		return jsonify(PROG.get(token, {"pct": 0, "stage": "queued"}))


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
		year = int(str(track["album_date"])[:4])
		audiofile.tag.recording_date = eyed3.core.Date(year)
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


def trim_silence(file_path, trim_start, trim_end):
	if not trim_start and not trim_end:
		return
	filters = []
	if trim_start:
		filters.append("silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB")
	if trim_end:
		filters += ["areverse", "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-91dB", "areverse"]
	tmp = file_path + ".tmp.mp3"
	try:
		subprocess.run(
			["ffmpeg", "-i", file_path, "-af", ",".join(filters),
			 "-map_metadata", "0", "-id3v2_version", "3",
			 "-c:a", "libmp3lame", "-b:a", "320k", tmp, "-y"],
			check=True, capture_output=True)
		os.replace(tmp, file_path)
	except Exception:
		if os.path.exists(tmp):
			os.remove(tmp)


def mp3_duration(path):
	af = eyed3.load(path)
	if af and af.info:
		return af.info.time_secs
	return 0


@app.post("/api/prepare")
def api_prepare():
	data = request.json
	track = data["track"]
	url = data["url"]
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

	def hook(d):
		if d.get("status") == "downloading":
			total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
			if total:
				set_progress(pid, min(89, int(d.get("downloaded_bytes", 0) / total * 90)), "downloading")
		elif d.get("status") == "finished":
			set_progress(pid, 90, "converting")

	opts = {
		"format": "bestaudio/best",
		"outtmpl": f"{wdir}/raw.%(ext)s",
		"ignoreerrors": True,
		"progress_hooks": [hook],
		"quiet": True,
		"postprocessors": [{
			"key": "FFmpegExtractAudio",
			"preferredcodec": "mp3",
			"preferredquality": "320",
		}],
	}
	with YoutubeDL(opts) as ydl:
		info = ydl.extract_info(url, download=True)
	mp3 = os.path.join(wdir, "raw.mp3")
	if not info or not os.path.exists(mp3):
		shutil.rmtree(wdir, ignore_errors=True)
		set_progress(pid, 0, "failed")
		return jsonify({"error": "download failed"}), 502
	set_progress(pid, 92, "tagging")
	tag_mp3(mp3, track)
	cur = os.path.join(wdir, "cur.mp3")
	shutil.copy(mp3, os.path.join(wdir, "orig.mp3"))
	os.rename(mp3, cur)
	set_progress(pid, 95, "trimming")
	idx = track["track_number"]
	total = track["total_tracks"] or 0
	trim_silence(cur, idx != 1, idx != total)
	with open(os.path.join(wdir, "meta.json"), "w") as f:
		json.dump(track, f)
	dur = mp3_duration(cur)
	with PROG_LOCK:
		PROG.pop(pid, None)
	diff = abs(dur - track.get("duration_ms", 0) / 1000)
	return jsonify({"token": token, "duration": dur, "diff": diff})


def wdir_of(token):
	if not re.fullmatch(r"[0-9a-f]{32}", token):
		return None
	p = os.path.join(WORK, token)
	if os.path.isdir(p):
		return p
	return None


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
	cur = os.path.join(wdir, "cur.mp3")
	length = end - start
	if length <= 0:
		return jsonify({"error": "bad range"}), 400
	filters = [f"atrim=start={start}:end={end}", "asetpts=PTS-STARTPTS"]
	if fadein > 0:
		filters.append(f"afade=t=in:st=0:d={fadein}")
	if fadeout > 0:
		filters.append(f"afade=t=out:st={max(0, length - fadeout)}:d={fadeout}")
	tmp = cur + ".tmp.mp3"
	try:
		subprocess.run(
			["ffmpeg", "-i", cur, "-af", ",".join(filters),
			 "-map_metadata", "0", "-id3v2_version", "3",
			 "-c:a", "libmp3lame", "-b:a", "320k", tmp, "-y"],
			check=True, capture_output=True)
		os.replace(tmp, cur)
	except subprocess.CalledProcessError as e:
		if os.path.exists(tmp):
			os.remove(tmp)
		return jsonify({"error": e.stderr.decode()[-300:]}), 500
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


@app.get("/api/download/<token>")
def api_download(token):
	wdir = wdir_of(token)
	if not wdir:
		return jsonify({"error": "gone"}), 404
	with open(os.path.join(wdir, "meta.json")) as f:
		track = json.load(f)
	name = normalize(track["file_name"]) + ".mp3"
	keep = request.args.get("keep") == "1"
	resp = send_file(os.path.join(wdir, "cur.mp3"), mimetype="audio/mpeg",
		as_attachment=True, download_name=name)
	if not keep:
		resp = cleanup_response(resp, [wdir])
	return resp


@app.post("/api/zip")
def api_zip():
	tokens = (request.json or {}).get("tokens", [])
	try:
		ensure_space()
	except RuntimeError as e:
		return jsonify({"error": str(e)}), 507
	zpath = os.path.join(WORK, uuid.uuid4().hex + ".zip")
	dirs = []
	used = set()
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
		for token in tokens:
			wdir = wdir_of(token)
			if not wdir:
				continue
			with open(os.path.join(wdir, "meta.json")) as f:
				track = json.load(f)
			name = normalize(track["file_name"]) + ".mp3"
			n = 2
			while name in used:
				name = normalize(track["file_name"]) + f" ({n}).mp3"
				n += 1
			used.add(name)
			z.write(os.path.join(wdir, "cur.mp3"), name)
			dirs.append(wdir)
	resp = send_file(zpath, mimetype="application/zip", as_attachment=True, download_name="tracks.zip")
	return cleanup_response(resp, dirs + [zpath])


@app.post("/api/retag")
def api_retag():
	try:
		ensure_space()
	except RuntimeError as e:
		return jsonify({"error": str(e)}), 507
	meta = json.loads(request.form["meta"])
	fields = meta.get("fields", {})
	items = meta.get("items", [])
	cover_png = None
	if "cover" in request.files:
		data = request.files["cover"].read()
	elif fields.get("cover_url"):
		try:
			data = rq.urlopen(fields["cover_url"]).read()
		except Exception:
			data = None
	else:
		data = None
	if data:
		buf = io.BytesIO()
		Image.open(io.BytesIO(data)).convert("RGB").save(buf, "PNG", compress_level=0)
		cover_png = buf.getvalue()
	wdir = os.path.join(WORK, uuid.uuid4().hex)
	os.makedirs(wdir)
	zpath = wdir + ".zip"
	files = request.files.getlist("files")
	total_tracks = str(len(items))
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
		for i, item in enumerate(items):
			f = files[item["file_index"]]
			fp = os.path.join(wdir, f"{i}.mp3")
			f.save(fp)
			try:
				tags = ID3(fp)
			except ID3NoHeaderError:
				tags = ID3()
			tags.clear()
			title = item.get("title") or os.path.splitext(f.filename)[0]
			tags["TIT2"] = TIT2(encoding=3, text=title)
			if fields.get("artist") is not None:
				tags["TPE1"] = TPE1(encoding=3, text=fields["artist"])
			if fields.get("album_artist") is not None:
				tags["TPE2"] = TPE2(encoding=3, text=fields["album_artist"])
			if fields.get("album") is not None:
				tags["TALB"] = TALB(encoding=3, text=fields["album"])
			if fields.get("year") is not None:
				tags["TDRC"] = TDRC(encoding=3, text=str(fields["year"]))
			if fields.get("genre") is not None:
				tags["TCON"] = TCON(encoding=3, text=fields["genre"])
			if meta.get("number", True):
				tags["TRCK"] = TRCK(encoding=3, text=f"{i + 1}/{total_tracks}")
			if cover_png:
				tags["APIC"] = APIC(encoding=3, mime="image/png", type=3, desc="", data=cover_png)
			tags.save(fp, v2_version=3, v1=0)
			num = str(i + 1).zfill(len(total_tracks))
			z.write(fp, f"{num} {normalize(title)}.mp3")
			os.remove(fp)
	resp = send_file(zpath, mimetype="application/zip", as_attachment=True, download_name="retagged.zip")
	return cleanup_response(resp, [wdir, zpath])


@app.get("/")
def root():
	return app.send_static_file("index.html")


if __name__ == "__main__":
	app.run(host="127.0.0.1", port=5510, threaded=True)
