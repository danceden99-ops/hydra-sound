const $ = (id) => document.getElementById(id);

function showToast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 3000);
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return res.json();
}

// Tabs
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab, .tab-panel").forEach((el) => el.classList.remove("active"));
    btn.classList.add("active");
    $(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

function formatStream(rate, bits, fmt) {
  if (!rate && !bits && !fmt) return "—";
  const r = rate ? `${(rate / 1000).toFixed(1)} kHz` : "";
  const b = bits ? `${bits}-bit` : "";
  return [b, r, fmt].filter(Boolean).join(" / ");
}

function updateLiveUI(data) {
  const { stream, volume, system, dsp_active } = data;
  $("cpu-load").textContent = `${system.cpu_percent}%`;
  $("cpu-temp").textContent = system.temperature_c !== null ? `${system.temperature_c}°C` : "N/A";

  const badge = $("pipeline-badge");
  badge.textContent = dsp_active ? "DSP (32-BIT+)" : "BIT-PERFECT";

  const srcBadge = $("stream-source");
  if (stream.active_source) {
    const pauseInfo = stream.state === "paused" && stream.pause_remaining_seconds !== null
      ? ` (PAUSED ${stream.pause_remaining_seconds}s)`
      : ` (${stream.state.toUpperCase()})`;
    srcBadge.textContent = `${stream.active_source.toUpperCase()}${pauseInfo}`;
    $("btn-release-lock").hidden = false;
  } else {
    srcBadge.textContent = "IDLE";
    $("btn-release-lock").hidden = true;
  }

  $("track-title").textContent = stream.title || (stream.active_source ? "Streaming Active" : "Waiting for stream…");
  $("track-artist").textContent = [stream.artist, stream.album].filter(Boolean).join(" — ") || "Discoverable on LAN";

  $("incoming-format").textContent = formatStream(stream.incoming_rate, stream.incoming_bit_depth, stream.incoming_format);
  $("outgoing-format").textContent = formatStream(stream.outgoing_rate, stream.outgoing_bit_depth, stream.outgoing_format);

  $("vol-mode-badge").textContent = volume.mode.toUpperCase();
  const disabled = volume.mode === "disabled";
  $("vol-slider").disabled = disabled;
  $("btn-mute").disabled = disabled;
  $("vol-slider").max = volume.max_volume;
  $("vol-slider").value = volume.current_volume;
  $("vol-value").textContent = volume.muted ? "MUTED" : `${volume.current_volume}%`;
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/status`);
  ws.onmessage = (ev) => updateLiveUI(JSON.parse(ev.data));
  ws.onclose = () => setTimeout(connectWebSocket, 2000);
}

let latestNetStatus = null;

function updateStaticIpFieldsState() {
  const isStatic = $("net-mode").value === "static";
  ["net-ip", "net-mask", "net-gw", "net-dns"].forEach((id) => {
    $(id).disabled = !isStatic;
  });
  if (isStatic && latestNetStatus) {
    if (!$("net-ip").value) $("net-ip").value = latestNetStatus.ip_address || "";
    if (!$("net-mask").value) $("net-mask").value = latestNetStatus.cidr || "24";
    if (!$("net-gw").value) $("net-gw").value = latestNetStatus.gateway || "";
    if (!$("net-dns").value) $("net-dns").value = latestNetStatus.dns || "";
  }
}

async function loadInitialConfig() {
  const [devices, cfg, irStatus, verInfo, liveStatus] = await Promise.all([
    api("/api/audio/devices"),
    api("/api/config"),
    api("/api/ir/status"),
    api("/api/system/version"),
    api("/api/status"),
  ]);

  const devSel = $("audio-device");
  devSel.innerHTML = "";
  devices.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = d.description;
    devSel.appendChild(opt);
  });
  devSel.value = cfg.audio.device_id;

  $("resample-enabled").checked = cfg.audio.resample_enabled;
  $("resample-rate").value = cfg.audio.resample_rate;
  $("resample-bits").value = cfg.audio.resample_bit_depth;
  $("resample-algo").value = cfg.audio.resample_algorithm;

  $("vol-mode").value = cfg.volume.mode;
  $("vol-curve").value = cfg.volume.curve;
  $("vol-power-on").value = cfg.volume.power_on_volume;
  $("vol-max").value = cfg.volume.max_volume;
  $("vol-step").value = cfg.volume.step_size;
  $("vol-hw-mixer").value = cfg.volume.hardware_mixer_name;

  $("renderer-device-name").value = cfg.renderers.device_name || "Hydra Sound";
  $("pause-timeout").value = cfg.renderers.pause_timeout_seconds;
  $("sp-enabled").checked = cfg.renderers.spotify.enabled;
  $("sp-auth").value = cfg.renderers.spotify.auth_mode;
  $("sp-user").value = cfg.renderers.spotify.username;
  $("sp-pass").value = cfg.renderers.spotify.password;

  $("qb-enabled").checked = cfg.renderers.qobuz.enabled;
  $("qb-email").value = cfg.renderers.qobuz.email;
  $("qb-pass").value = cfg.renderers.qobuz.password;

  $("ap-enabled").checked = cfg.renderers.airplay.enabled;
  $("dlna-enabled").checked = cfg.renderers.dlna.enabled;
  $("gc-enabled").checked = cfg.renderers.googlecast.enabled;

  $("ir-mode").value = cfg.ir.mode;
  $("ir-gpio-pin").value = cfg.ir.gpio_pin;
  $("flirc-status").textContent = `FLIRC Connected: ${irStatus.flirc.connected ? "Yes" : "No"} (${irStatus.flirc.raw})`;

  latestNetStatus = liveStatus.network || {};
  const netModeLabel = (latestNetStatus.mode || cfg.network.mode || "dhcp").toUpperCase();
  const netIpDisplay = latestNetStatus.ip_address
    ? `${latestNetStatus.ip_address}/${latestNetStatus.cidr || "24"}`
    : "Unknown";
  $("net-current-summary").textContent =
    `Interface: ${latestNetStatus.interface || cfg.network.interface} | Mode: ${netModeLabel} | IP: ${netIpDisplay} | Gateway: ${latestNetStatus.gateway || "—"} | DNS: ${latestNetStatus.dns || "—"} | mDNS: ${cfg.network.hostname}.local`;

  $("net-hostname").value = cfg.network.hostname;
  $("net-iface").value = latestNetStatus.interface || cfg.network.interface;
  $("net-mode").value = latestNetStatus.mode || cfg.network.mode;
  $("net-ip").value = cfg.network.static_ip || (latestNetStatus.mode === "static" ? latestNetStatus.ip_address : "");
  $("net-mask").value = cfg.network.subnet_mask || latestNetStatus.cidr || "24";
  $("net-gw").value = cfg.network.gateway || latestNetStatus.gateway || "";
  $("net-dns").value = cfg.network.dns || latestNetStatus.dns || "";
  $("net-host").textContent = `${cfg.network.hostname}.local (${latestNetStatus.ip_address || "—"})`;
  $("wifi-country").value = cfg.network.wifi_country || "US";
  $("wifi-ssid").value = cfg.network.wifi_ssid || "";
  $("wifi-pass").value = cfg.network.wifi_password || "";
  updateStaticIpFieldsState();

  $("sys-github-repo").value = cfg.system?.github_repo || verInfo.github_repo || "";
  const latestTag = verInfo.latest_github_tag ? ` | Latest GitHub Release: ${verInfo.latest_github_tag}` : "";
  $("sys-version-info").textContent =
    `App v${verInfo.app_version} (${verInfo.git_tag} / ${verInfo.git_commit})${latestTag} — Python ${verInfo.python_version}`;
}

// Event bindings
$("vol-slider").addEventListener("change", async (e) => {
  await api("/api/volume/set", "POST", { volume: Number(e.target.value) });
});
$("btn-mute").addEventListener("click", () => api("/api/volume/mute", "POST"));
$("btn-release-lock").addEventListener("click", () => api("/api/stream/release", "POST"));

$("save-audio").addEventListener("click", async () => {
  const devSel = $("audio-device");
  await api("/api/config/audio", "PUT", {
    device_id: devSel.value,
    device_name: devSel.options[devSel.selectedIndex]?.textContent || "DAC",
    bit_perfect: !$("resample-enabled").checked,
    resample_enabled: $("resample-enabled").checked,
    resample_rate: Number($("resample-rate").value),
    resample_bit_depth: Number($("resample-bits").value),
    resample_algorithm: $("resample-algo").value,
  });
  showToast("Audio settings applied");
});

$("btn-audio-caps").addEventListener("click", async () => {
  const devId = $("audio-device").value;
  const out = $("audio-caps-output");
  out.hidden = false;
  out.textContent = `Probing capabilities for ${devId}…`;
  const caps = await api(`/api/audio/capabilities?device_id=${encodeURIComponent(devId)}`);
  let text = `Device: ${caps.device_id}\n\n[ALSA Hardware Parameters]\n${caps.hw_params}`;
  if (caps.usb_stream) {
    text += `\n\n[USB Audio Descriptor (/proc/asound/.../stream0)]\n${caps.usb_stream}`;
  }
  out.textContent = text;
});

$("save-volume").addEventListener("click", async () => {
  await api("/api/config/volume", "PUT", {
    mode: $("vol-mode").value,
    curve: $("vol-curve").value,
    power_on_volume: Number($("vol-power-on").value),
    max_volume: Number($("vol-max").value),
    step_size: Number($("vol-step").value),
    current_volume: Number($("vol-slider").value),
    muted: false,
    hardware_mixer_name: $("vol-hw-mixer").value || "PCM",
  });
  showToast("Volume settings saved");
});

$("save-services").addEventListener("click", async () => {
  const devName = $("renderer-device-name").value.trim() || "Hydra Sound";
  await api("/api/config/renderers", "PUT", {
    device_name: devName,
    pause_timeout_seconds: Number($("pause-timeout").value),
    spotify: {
      enabled: $("sp-enabled").checked,
      device_name: devName,
      auth_mode: $("sp-auth").value,
      username: $("sp-user").value,
      password: $("sp-pass").value,
      bitrate: 320,
    },
    qobuz: {
      enabled: $("qb-enabled").checked,
      device_name: devName,
      email: $("qb-email").value,
      password: $("qb-pass").value,
    },
    airplay: { enabled: $("ap-enabled").checked, device_name: devName, version: "1" },
    dlna: { enabled: $("dlna-enabled").checked, device_name: devName },
    googlecast: { enabled: $("gc-enabled").checked, device_name: devName },
  });
  showToast("Service settings saved");
});

$("btn-check-updates").addEventListener("click", async () => {
  const list = $("modules-list");
  list.textContent = "Checking GitHub releases…";
  const mods = await api("/api/modules/updates");
  list.innerHTML = "";
  mods.forEach((m) => {
    const row = document.createElement("div");
    row.className = "module-row";
    row.innerHTML = `
      <div>
        <strong>${m.display_name}</strong>
        <div class="hint">Installed: ${m.installed_version} | Latest: ${m.latest_version}</div>
      </div>
      <button class="btn btn-sm ${m.update_available ? "btn-primary" : "btn-outline"}">
        ${m.update_available ? "Update Available" : "Reinstall / Update"}
      </button>
    `;
    row.querySelector("button").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Updating…";
      const statusEl = $("modules-update-status");
      const compileNote = (m.id === "spotify" || m.id === "qobuz")
        ? " Compiling Rust source on Raspberry Pi can take 5–10 minutes; please wait."
        : " Please wait…";
      statusEl.textContent = `Updating ${m.display_name}…${compileNote}`;
      showToast(`Updating ${m.display_name}…`);
      try {
        const res = await api(`/api/modules/${m.id}/update`, "POST");
        if (res.ok) {
          statusEl.textContent = `${m.display_name} updated successfully! Refreshing versions…`;
          showToast(`${m.display_name} updated`);
          $("btn-check-updates").click();
        } else {
          statusEl.textContent = `${m.display_name} update failed: ${res.error || res.stderr || "see logs"}`;
          showToast(`Update failed: ${m.display_name}`);
        }
      } catch (err) {
        statusEl.textContent = `${m.display_name} update request timed out or lost connection (build may still be finishing on the Pi).`;
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    });
    list.appendChild(row);
  });
});

$("save-ir").addEventListener("click", async () => {
  const res = await api("/api/config/ir", "PUT", {
    mode: $("ir-mode").value,
    gpio_pin: Number($("ir-gpio-pin").value),
  });
  showToast(res.gpio?.message || "IR settings saved");
});

document.querySelectorAll("[data-learn]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    $("flirc-learn-msg").textContent = `Point remote at FLIRC and press button for ${btn.dataset.learn}…`;
    const res = await api("/api/ir/flirc/learn", "POST", { action: btn.dataset.learn });
    $("flirc-learn-msg").textContent = res.ok ? `Learned ${btn.dataset.learn}!` : (res.error || res.output);
  });
});

$("flirc-clear").addEventListener("click", async () => {
  const res = await api("/api/ir/flirc/clear", "POST");
  $("flirc-learn-msg").textContent = res.ok ? "FLIRC cleared." : res.error;
});

$("net-mode").addEventListener("change", updateStaticIpFieldsState);

$("save-network").addEventListener("click", async () => {
  await api("/api/config/network", "PUT", {
    hostname: $("net-hostname").value,
    interface: $("net-iface").value,
    mode: $("net-mode").value,
    static_ip: $("net-ip").value,
    subnet_mask: $("net-mask").value,
    gateway: $("net-gw").value,
    dns: $("net-dns").value,
    wifi_ssid: $("wifi-ssid").value,
    wifi_password: $("wifi-pass").value,
    wifi_country: $("wifi-country").value.trim().toUpperCase() || "US",
  });
  showToast("Network configuration applied");
});

$("btn-scan-wifi").addEventListener("click", async () => {
  $("wifi-status-msg").textContent = "Scanning nearby Wi-Fi networks…";
  const country = encodeURIComponent($("wifi-country").value.trim().toUpperCase() || "US");
  const nets = await api(`/api/network/wifi/scan?country=${country}`);
  const sel = $("wifi-scan-list");
  sel.innerHTML = '<option value="">-- Select a Wi-Fi network --</option>';
  nets.forEach((n) => {
    const opt = document.createElement("option");
    opt.value = n.ssid;
    opt.textContent = `${n.ssid} (${n.signal}% / ${n.security})`;
    sel.appendChild(opt);
  });
  $("wifi-status-msg").textContent = nets.length ? `Found ${nets.length} network(s).` : "No Wi-Fi networks found.";
});

$("wifi-scan-list").addEventListener("change", (e) => {
  if (e.target.value) $("wifi-ssid").value = e.target.value;
});

$("btn-connect-wifi").addEventListener("click", async () => {
  const ssid = $("wifi-ssid").value.trim();
  if (!ssid) {
    showToast("Enter or select a Wi-Fi SSID");
    return;
  }
  $("wifi-status-msg").textContent = `Connecting to ${ssid}…`;
  const res = await api("/api/network/wifi/connect", "POST", {
    ssid,
    password: $("wifi-pass").value,
    country: $("wifi-country").value.trim().toUpperCase() || "US",
  });
  $("wifi-status-msg").textContent = res.ok ? `Connected to ${ssid}` : (res.error || res.output || "Connection failed");
  showToast(res.ok ? `Connected to ${ssid}` : "Wi-Fi connection failed");
});

$("btn-save-repo").addEventListener("click", async () => {
  const repo = $("sys-github-repo").value.trim();
  await api("/api/config/system", "PUT", { github_repo: repo });
  showToast("GitHub repository saved");
});

$("btn-check-releases").addEventListener("click", async () => {
  const repo = $("sys-github-repo").value.trim();
  if (!repo) {
    showToast("Enter GitHub Repository (owner/repo) first");
    return;
  }
  await api("/api/config/system", "PUT", { github_repo: repo });
  $("sys-release-msg").textContent = `Checking GitHub releases for ${repo}…`;
  const releases = await api("/api/system/releases");
  const sel = $("sys-release-select");
  sel.innerHTML = "";
  if (!releases.length) {
    sel.innerHTML = '<option value="">-- No releases found --</option>';
    $("sys-release-msg").textContent = "No GitHub releases found (check repo name or public visibility).";
    return;
  }
  releases.forEach((r, idx) => {
    const opt = document.createElement("option");
    opt.value = r.tag;
    if (r.tag === "main") {
      opt.textContent = "main [Latest Dev / Unreleased Commits] (branch)";
    } else {
      const latestMark = idx === 0 ? " [Latest Release]" : "";
      opt.textContent = `${r.tag}${latestMark} (${r.published_at})`;
    }
    sel.appendChild(opt);
  });
  $("sys-release-msg").textContent = `Found ${releases.length} release(s). Latest: ${releases[0].tag}`;
});

$("btn-install-release").addEventListener("click", async () => {
  const tag = $("sys-release-select").value;
  if (!tag) {
    showToast("Select a release version first");
    return;
  }
  if (!confirm(`Install Hydra Sound release ${tag} and restart service?`)) return;
  $("sys-release-msg").textContent = `Installing release ${tag}…`;
  const res = await api("/api/system/install-release", "POST", { tag });
  if (res.ok) {
    $("sys-release-msg").textContent = `Installed ${tag}. Restarting Hydra Sound…`;
    showToast(`Installed ${tag}; restarting…`);
  } else {
    $("sys-release-msg").textContent = res.detail || res.error || "Install failed";
    showToast("Release install failed");
  }
});

$("btn-reboot").addEventListener("click", async () => {
  if (!confirm("Reboot the Raspberry Pi now?")) return;
  await api("/api/system/power", "POST", { action: "reboot" });
  showToast("Rebooting Raspberry Pi…");
});

$("btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Shut down the Raspberry Pi now?")) return;
  await api("/api/system/power", "POST", { action: "shutdown" });
  showToast("Shutting down Raspberry Pi…");
});

async function loadSystemLogs() {
  const data = await api("/api/system/logs");
  $("sys-logs-meta").textContent = `Showing ${data.count} / 500 entries (${data.path || "log file"})`;
  const out = $("sys-logs-output");
  out.textContent = data.lines.length ? data.lines.join("\n") : "(Log is empty)";
  out.scrollTop = out.scrollHeight;
}

$("btn-refresh-logs").addEventListener("click", loadSystemLogs);

$("btn-clear-logs").addEventListener("click", async () => {
  await api("/api/system/logs/clear", "POST");
  await loadSystemLogs();
  showToast("System logs cleared");
});

loadInitialConfig().then(connectWebSocket);
