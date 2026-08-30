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

$("gateBtn").onclick = function() {
	api("/api/gate", {json: {password: $("gatePassword").value}}).then(function() {
		unlock();
	}).catch(function(e) {
		$("gateMsg").textContent = e.message;
	});
};
$("gatePassword").onkeydown = function(e) {
	if (e.key === "Enter") $("gateBtn").click();
};

function unlock() {
	$("gate").hidden = true;
	$("main").hidden = false;
	checkSpotify();
}

api("/api/spotify/status").then(function() {
	unlock();
}).catch(function() {});

$("tabDownloadBtn").onclick = function() {
	$("tabDownload").hidden = false;
	$("tabRetag").hidden = true;
};
$("tabRetagBtn").onclick = function() {
	$("tabDownload").hidden = true;
	$("tabRetag").hidden = false;
};

function checkSpotify() {
	api("/api/spotify/status").then(function(d) {
		if (d.logged_in) {
			$("spotifyLoginBtn").hidden = true;
			$("spotifyUser").textContent = "logged in as " + (d.name || "?");
			loadLibrary();
		}
	}).catch(function() {});
}

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

function discardAll() {
	var tokens = rows.filter(function(r) { return r.token; }).map(function(r) { return r.token; });
	if (tokens.length) api("/api/discard", {json: {tokens: tokens}}).catch(function() {});
}

window.addEventListener("pagehide", function() {
	var tokens = rows.filter(function(r) { return r.token; }).map(function(r) { return r.token; });
	if (tokens.length) {
		navigator.sendBeacon("/api/discard", new Blob([JSON.stringify({tokens: tokens})], {type: "application/json"}));
	}
});

var collectionName = "tracks";

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
		d.tracks.forEach(function(track, i) { addRow(track, i); });
		if (!rows.length) {
			$("overallProgress").textContent = "no tracks found";
			return;
		}
		updateOverall();
		rows.forEach(function(row) { queueFind(row); });
	}).catch(function(e) {
		$("overallProgress").textContent = "failed: " + e.message;
	});
}

function addRow(track, i) {
	var tr = document.createElement("tr");
	tr.innerHTML = "<td>" + track.track_number + "</td><td></td><td></td><td>searching</td><td></td><td></td>";
	tr.children[1].textContent = track.artist_name + " - " + track.track_name;
	$("trackRows").appendChild(tr);
	var row = {
		track: track, tr: tr, url: null, tried: [], token: null,
		status: "searching", diff: null, source: null,
		sourceCell: tr.children[2], statusCell: tr.children[3],
		progressCell: tr.children[4], actionCell: tr.children[5]
	};
	rows.push(row);
}

function queueFind(row) {
	findQueue.push(row);
	pumpFind();
}

function pumpFind() {
	while (findActive < 3 && findQueue.length) {
		var row = findQueue.shift();
		findActive++;
		runFind(row);
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
			row.statusCell.textContent = "✗ " + d.diff.toFixed(1) + "s off";
		}
		renderActions(row);
		updateOverall();
		prepActive--;
		pumpPrepare();
	}
	function fail(msg) {
		row.status = "missing";
		row.statusCell.textContent = "failed: " + msg;
		renderActions(row);
		updateOverall();
		prepActive--;
		pumpPrepare();
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
	if (row.token) {
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
}

function updateOverall() {
	var done = rows.filter(function(r) { return r.token || r.status === "missing"; }).length;
	var ready = rows.filter(function(r) { return r.token; }).length;
	if (rows.length) {
		$("overallProgress").textContent = Math.round(done / rows.length * 100) + "% (" + ready + "/" + rows.length + " ready)";
	}
}

$("downloadAllBtn").onclick = function() {
	var tokens = rows.filter(function(r) { return r.token; }).map(function(r) { return r.token; });
	if (!tokens.length) return;
	api("/api/zip", {json: {tokens: tokens, name: collectionName}}).then(function(d) {
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

function saveBlob(blob, name) {
	var a = document.createElement("a");
	a.href = URL.createObjectURL(blob);
	a.download = name;
	a.click();
	setTimeout(function() { URL.revokeObjectURL(a.href); }, 10000);
}

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

canvas.onmousedown = function(e) {
	if (!cutter.buffer) return;
	var rect = canvas.getBoundingClientRect();
	var x = (e.clientX - rect.left) * canvas.width / rect.width;
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
	var rect = canvas.getBoundingClientRect();
	var x = (e.clientX - rect.left) * canvas.width / rect.width;
	var t = Math.max(0, Math.min(cutter.buffer.duration, xToTime(x)));
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
	var rect = canvas.getBoundingClientRect();
	var x = (e.clientX - rect.left) * canvas.width / rect.width;
	var anchor = xToTime(x);
	var factor = 1.25;
	if (e.deltaY < 0) factor = 0.8;
	zoomAround(anchor, factor);
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
	api("/api/restore/" + cutter.row.token, {method: "POST"}).then(function() {
		loadCutterAudio();
	}).catch(function(e) {
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
			var moved = retagFiles.splice(i, 1)[0];
			retagFiles.splice(target, 0, moved);
			renderRetagRows();
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
		grip.textContent = "⇅ drag";
		tr.appendChild(num);
		tr.appendChild(grip);
		tr.appendChild(name);
		tr.appendChild(title);
		tr.ondragstart = function(e) {
			e.dataTransfer.setData("text/plain", i);
		};
		tr.ondragover = function(e) { e.preventDefault(); };
		tr.ondrop = function(e) {
			e.preventDefault();
			var from = parseInt(e.dataTransfer.getData("text/plain"), 10);
			if (isNaN(from) || from === i) return;
			var moved = retagFiles.splice(from, 1)[0];
			retagFiles.splice(i, 0, moved);
			renderRetagRows();
		};
		tbody.appendChild(tr);
	});
}

function guessTitle(name) {
	var stem = name.replace(/\.mp3$/i, "");
	var m = stem.match(/^(\d+)[.\s\-]+(.+)$/);
	if (m) return m[2].trim();
	return stem;
}

$("incArtist").onchange = function() { $("fArtist").disabled = !this.checked; };
$("incAlbumArtist").onchange = function() { $("fAlbumArtist").disabled = !this.checked; };
$("incAlbum").onchange = function() { $("fAlbum").disabled = !this.checked; };
$("incYear").onchange = function() { $("fYear").disabled = !this.checked; };
$("incGenre").onchange = function() { $("fGenre").disabled = !this.checked; };
$("incCover").onchange = function() {
	$("fCoverFile").disabled = !this.checked;
	$("fCoverUrl").disabled = !this.checked;
};
$("fYear").disabled = true;
$("fGenre").disabled = true;

$("retagGoBtn").onclick = function() {
	if (!retagFiles.length) {
		$("retagMsg").textContent = "no files";
		return;
	}
	var fields = {};
	if ($("incArtist").checked) fields.artist = $("fArtist").value;
	if ($("incAlbumArtist").checked) {
		fields.album_artist = $("fAlbumArtist").value || $("fArtist").value;
	}
	if ($("incAlbum").checked) fields.album = $("fAlbum").value;
	if ($("incYear").checked && $("fYear").value) fields.year = $("fYear").value;
	if ($("incGenre").checked && $("fGenre").value) fields.genre = $("fGenre").value;
	var fd = new FormData();
	var items = retagFiles.map(function(f, i) {
		fd.append("files", f);
		return {file_index: i, title: f.retagTitle || guessTitle(f.name)};
	});
	if ($("incCover").checked) {
		if ($("fCoverFile").files[0]) {
			fd.append("cover", $("fCoverFile").files[0]);
		} else if ($("fCoverUrl").value) {
			fields.cover_url = $("fCoverUrl").value;
		}
	}
	fd.append("meta", JSON.stringify({fields: fields, items: items, number: $("incNumber").checked}));
	var xhr = new XMLHttpRequest();
	xhr.open("POST", "/api/retag");
	xhr.responseType = "blob";
	xhr.upload.onprogress = function(e) {
		if (e.lengthComputable) {
			$("retagMsg").textContent = "uploading " + Math.round(e.loaded / e.total * 100) + "%";
		}
	};
	xhr.onload = function() {
		if (xhr.status === 200) {
			$("retagMsg").textContent = "done";
			saveBlob(xhr.response, ($("fAlbum").value || "retagged") + ".zip");
		} else {
			$("retagMsg").textContent = "failed (" + xhr.status + ")";
		}
	};
	xhr.onerror = function() {
		$("retagMsg").textContent = "upload error";
	};
	$("retagMsg").textContent = "uploading...";
	xhr.send(fd);
};
