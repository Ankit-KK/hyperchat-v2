// HyperChat OBS audio player (Phase 10). No third-party code.
//
// Flow: read the OBS token from the URL #fragment (never sent to a server), remove it from the address bar,
// GET /api/v1/obs/session, subscribe to private-{slug}-audio through Pusher (protocol 7 over WebSocket,
// authorized by /api/v1/realtime/pusher/auth with the token header), then claim items one at a time:
//   on connect, on `audio-ready`, after an item ends or fails, on a Retry-After hint, and every poll_seconds
//   while idle (the safety net for missed events; also the only trigger when realtime is off).
// Claims are single-flight and never happen while an item plays. One reused <audio> element plays every item
// (no decoding into memory, no AudioContext). A claimed item already counts as played on the server.
(function () {
  "use strict";
  var statusEl = document.getElementById("status");
  var startBtn = document.getElementById("start");
  var audio = document.getElementById("audio");
  var token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  history.replaceState(null, "", location.pathname);  // the token leaves the address bar at once

  function status(text) { statusEl.textContent = text; }
  if (!/^obs_[0-9a-f]{64}$/.test(token) && !/^[A-Za-z0-9_-]{16,200}$/.test(token)) {
    status("Missing or invalid OBS token: copy the player URL from your dashboard again.");
    return;
  }

  var session = null, busy = false, again = false, playing = false, timer = null, ready = false;

  function api(path, options) {
    options = options || {};
    var headers = options.headers || {};
    headers["x-obs-token"] = token;
    return fetch(path, { method: options.method || "GET", headers: headers, body: options.body,
                         cache: "no-store", credentials: "omit", redirect: "error" });
  }

  function schedule(seconds) {
    clearTimeout(timer);
    timer = setTimeout(claim, Math.max(0.2, seconds) * 1000);
  }

  function claim() {
    if (!ready || playing) return;
    if (busy) { again = true; return; }
    busy = true;
    clearTimeout(timer);
    api("/api/v1/obs/audio/claim", { method: "POST" }).then(function (r) {
      if (r.status === 200) return r.json().then(play);
      if (r.status === 204) {
        var hint = Number(r.headers.get("Retry-After"));
        schedule(hint > 0 ? Math.min(hint, session.poll_seconds) : session.poll_seconds);
        status("Waiting for alerts");
        return;
      }
      if (r.status === 401) { status("OBS token rejected: create a new player URL in your dashboard."); ready = false; return; }
      schedule(r.status === 429 ? 30 : 10);
    }).catch(function () { schedule(10); }).then(function () {
      busy = false;
      if (again && !playing) { again = false; claim(); }
    });
  }

  function play(item) {
    playing = true;
    clearTimeout(timer);
    status("Playing (" + item.kind + ")");
    audio.src = item.audio_url;
    var p = audio.play();
    if (p && p.catch) p.catch(function () { finished(3); });
  }

  function finished(delay) {
    playing = false;
    audio.removeAttribute("src");
    audio.load();  // releases the previous file
    schedule(delay);
  }
  audio.addEventListener("ended", function () { finished(0.3); });
  audio.addEventListener("error", function () { if (playing) finished(3); });

  // ── Pusher (protocol 7): audio-ready only triggers a claim, it never carries what to play ──
  function realtime(cfg) {
    var ws = null, socketId = null, retry = 0;
    function open() {
      ws = new WebSocket("wss://ws-" + cfg.cluster + ".pusher.com/app/" + encodeURIComponent(cfg.key) +
                         "?protocol=7&client=hyperchat-obs&version=1.0");
      ws.onmessage = function (ev) {
        var m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.event === "pusher:connection_established") {
          socketId = JSON.parse(m.data).socket_id;
          retry = 0;
          subscribe();
          claim();  // anything that became ready while disconnected
        } else if (m.event === "pusher:ping") {
          ws.send(JSON.stringify({ event: "pusher:pong", data: {} }));
        } else if (m.event === "audio-ready" && m.channel === cfg.audio_channel) {
          claim();
        }
      };
      ws.onclose = function () { setTimeout(open, Math.min(60, Math.pow(2, retry++)) * 1000); };
    }
    function subscribe() {
      var body = new URLSearchParams({ socket_id: socketId, channel_name: cfg.audio_channel }).toString();
      api(cfg.auth_endpoint, { method: "POST", body: body,
                               headers: { "content-type": "application/x-www-form-urlencoded" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (a) {
          if (a && a.auth) ws.send(JSON.stringify({ event: "pusher:subscribe",
                                                     data: { channel: cfg.audio_channel, auth: a.auth } }));
        }).catch(function () {});
    }
    open();
  }

  function begin() {
    ready = true;
    document.body.classList.add("quiet");
    if (session.realtime) realtime(session.realtime);
    claim();
  }

  function start() {
    api("/api/v1/obs/session").then(function (r) {
      if (r.status === 401) throw new Error("token");
      if (!r.ok) throw new Error("session");
      return r.json();
    }).then(function (s) {
      session = s;
      status("Connected: " + (s.streamer.name || s.streamer.slug));
      if (!s.silence_url) { begin(); return; }
      // Autoplay check with the silent file: OBS allows autoplay; a normal browser tab needs one click first,
      // and nothing is claimed before audio can actually play.
      audio.src = s.silence_url;
      var p = audio.play();
      (p && p.then ? p : Promise.resolve()).then(function () { audio.removeAttribute("src"); audio.load(); begin(); },
        function () {
          audio.removeAttribute("src");
          startBtn.hidden = false;
          startBtn.addEventListener("click", function () {
            audio.src = s.silence_url;
            audio.play().then(function () { startBtn.hidden = true; audio.removeAttribute("src"); audio.load(); begin(); },
                              function () { status("Audio is blocked in this browser."); });
          }, { once: true });
        });
    }).catch(function (e) {
      status(e.message === "token" ? "OBS token rejected: create a new player URL in your dashboard."
                                   : "Cannot reach HyperChat; retrying…");
      if (e.message !== "token") setTimeout(start, 30000);  // never reload: the token is no longer in the URL
    });
  }
  start();
})();
