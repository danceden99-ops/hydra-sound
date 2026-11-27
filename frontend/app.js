const $ = (id) => document.getElementById(id);
let currentMuted = false;
let currentVolumeMode = "disabled";
let eqState = { enabled: false, active_preset_id: null, presets: [] };

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
  $("cpu-temp").textContent = system.temperature_c !== null
    ? `${((system.temperature_c * 9) / 5 + 32).toFixed(1)}°F`
    : "N/A";

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
  const art = $("track-art");
  if (art) {
    if (stream.artwork_url) {
      art.src = stream.artwork_url;
      art.hidden = false;
    } else {
      art.hidden = true;
      art.removeAttribute("src");
    }
  }

  $("incoming-format").textContent = formatStream(stream.incoming_rate, stream.incoming_bit_depth, stream.incoming_format);
  $("outgoing-format").textContent = formatStream(stream.outgoing_rate, stream.outgoing_bit_depth, stream.outgoing_format);

  $("vol-mode-badge").textContent = data.usb_safety_muted ? "MUTED (USB SAFETY)" : volume.mode.toUpperCase();
  currentVolumeMode = volume.mode;
  const usbBanner = $("usb-safety-banner");
  if (usbBanner) usbBanner.hidden = !data.usb_safety_muted;
  const hwBanner = $("hw-volume-error-banner");
  if (hwBanner) {
    if (data.hardware_volume_error) {
      hwBanner.textContent = data.hardware_volume_error;
      hwBanner.hidden = false;
    } else {
      hwBanner.hidden = true;
    }
  }
  const toneBtn = $("btn-test-tone");
  if (toneBtn) {
    const active = Boolean(data.test_tone_active);
    toneBtn.textContent = active ? "Stop Test Tone" : "Play Test Tone (440 Hz)";
    toneBtn.classList.toggle("btn-danger", active);
    toneBtn.classList.toggle("btn-outline", !active);
  }
  const disabled = volume.mode === "disabled";
  $("vol-slider").disabled = disabled;
  $("btn-mute").disabled = disabled;
  const effMax = data.effective_max_volume ?? volume.max_volume;
  $("vol-slider").max = effMax;
  $("vol-slider").value = volume.current_volume;
  $("vol-slider").title =
    data.hard_max_volume != null
      ? `OS hard limit active: ${data.hard_max_volume}% (effective max: ${effMax}%)`
      : "";
  currentMuted = Boolean(volume.muted);
  $("vol-value").textContent = volume.muted ? "MUTED" : `${volume.current_volume}%`;

  const strip = $("renderer-status-strip");
  if (strip && data.renderers) {
    strip.innerHTML = "";
    Object.entries(data.renderers).forEach(([id, r]) => {
      if (!r.enabled) return;
      const b = document.createElement("span");
      const cls =
        r.status === "connected" ? "badge badge-success" :
        r.status === "error" ? "badge badge-danger" :
        r.status === "advertising" ? "badge badge-accent" : "badge";
      b.className = cls;
      b.title = r.detail || "";
      const label =
        r.status === "connected" ? "Connected" :
        r.status === "advertising" ? "Ready" :
        r.status === "error" ? "Error" :
        r.status === "not_installed" ? "Not Installed" : "Stopped";
      b.textContent = `${r.display_name}: ${label}`;
      strip.appendChild(b);
    });
  }
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

function formatRateLabel(rate) {
  return `${rate.toLocaleString()} Hz`;
}

async function refreshResampleCapabilities(deviceId) {
  const hint = $("resample-recommendation");
  const rateSel = $("resample-rate");
  const bitsSel = $("resample-bits");
  if (!deviceId || !rateSel || !bitsSel) return;

  try {
    const caps = await api(`/api/audio/capabilities?device_id=${encodeURIComponent(deviceId)}`);
    const rates = caps.supported_rates || [];
    const bits = caps.supported_bit_depths || [];
    const recRate = caps.recommended_rate;
    const recBits = caps.recommended_bit_depth;

    Array.from(rateSel.options).forEach((opt) => {
      const val = Number(opt.value);
      const supported = rates.length === 0 || rates.includes(val);
      opt.disabled = !supported;
      let label = formatRateLabel(val);
      if (!supported) label += " (Unsupported)";
      else if (val === recRate) label += " ★ Recommended";
      opt.textContent = label;
    });

    Array.from(bitsSel.options).forEach((opt) => {
      const val = Number(opt.value);
      const supported = bits.length === 0 || bits.includes(val);
      opt.disabled = !supported;
      let label = `${val}-bit`;
      if (!supported) label += " (Unsupported)";
      else if (val === recBits) label += " ★ Recommended";
      opt.textContent = label;
    });

    if (hint) {
      if (rates.length > 0 || bits.length > 0) {
        const bText = bits.length ? bits.map((b) => `${b}-bit`).join(", ") : "Unknown";
        const rMax = rates.length ? formatRateLabel(Math.max(...rates)) : "Unknown";
        const recText = [recBits ? `${recBits}-bit` : null, recRate ? formatRateLabel(recRate) : null]
          .filter(Boolean)
          .join(" / ");
        hint.textContent = `Detected DAC Playback: ${bText} (up to ${rMax}). Highest-quality choice: ${recText}.`;
      } else {
        hint.textContent = "Could not probe DAC capabilities while idle/busy; all standard rates shown.";
      }
    }
  } catch {
    if (hint) hint.textContent = "";
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
    opt.dataset.deviceType = d.device_type || "";
    devSel.appendChild(opt);
  });
  devSel.value = cfg.audio.device_id;

  $("resample-enabled").checked = cfg.audio.resample_enabled;
  $("dither-24bit-spdif").checked = Boolean(cfg.audio.dither_24bit_spdif);
  $("resample-rate").value = cfg.audio.resample_rate;
  $("resample-bits").value = cfg.audio.resample_bit_depth;
  $("resample-algo").value = cfg.audio.resample_algorithm;
  refreshResampleCapabilities(cfg.audio.device_id);

  $("vol-mode").value = cfg.volume.mode;
  $("vol-curve").value = cfg.volume.curve;
  $("vol-power-on").value = cfg.volume.power_on_volume;
  $("vol-max").value = cfg.volume.max_volume;
  $("vol-step").value = cfg.volume.step_size;
  $("vol-hw-mixer").value = cfg.volume.hardware_mixer_name;

  $("renderer-device-name").value = cfg.renderers.device_name || "Hydra Sound";
  $("lock-mode").value = cfg.renderers.lock_mode || "preempt_paused";
  $("pause-timeout").value = cfg.renderers.pause_timeout_seconds;
  $("sp-enabled").checked = cfg.renderers.spotify.enabled;
  $("sp-auth").value = cfg.renderers.spotify.auth_mode;
  $("sp-user").value = cfg.renderers.spotify.username;
  $("sp-pass").value = cfg.renderers.spotify.password;

  $("qb-enabled").checked = cfg.renderers.qobuz.enabled;
  $("qb-auth").value = cfg.renderers.qobuz.auth_mode || "zeroconf";
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
  if (cfg.eq) loadEQState(cfg.eq);
  $("wifi-country").value = cfg.network.wifi_country || "US";
  $("wifi-ssid").value = cfg.network.wifi_ssid || "";
  $("wifi-pass").value = cfg.network.wifi_password || "";
  updateStaticIpFieldsState();

  $("sys-github-repo").value = cfg.system?.github_repo || verInfo.github_repo || "";
  const loggingEnabled = Boolean(cfg.system?.logging_enabled);
  $("sys-logging-enabled").checked = loggingEnabled;
  $("sys-logs-meta").textContent = loggingEnabled
    ? 'File logging is enabled. Click "Refresh Logs" to view the latest entries.'
    : "File logging is disabled (SD card protection).";
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

function isSelectedDeviceUsb() {
  const devSel = $("audio-device");
  const opt = devSel.options[devSel.selectedIndex];
  const dtype = opt?.dataset?.deviceType || "";
  const label = opt?.textContent || "";
  return dtype === "usb" || label.toLowerCase().includes("usb");
}

function promptUsbDisabledConfirm() {
  return confirm(
    "SPEAKER PROTECTION WARNING\n\n" +
    "You are routing to a USB DAC with Volume Mode = Disabled (0 dB / 100% full-scale pass-through with NO max-volume limiter).\n\n" +
    "Until confirmed, output will stay MUTED.\n\n" +
    "Are you sure this is intentional and your downstream amp/DAC volume is safe?"
  );
}

$("save-audio").addEventListener("click", async () => {
  const devSel = $("audio-device");
  const res = await api("/api/config/audio", "PUT", {
    device_id: devSel.value,
    device_name: devSel.options[devSel.selectedIndex]?.textContent || "DAC",
    bit_perfect: !$("resample-enabled").checked,
    resample_enabled: $("resample-enabled").checked,
    dither_24bit_spdif: $("dither-24bit-spdif").checked,
    resample_rate: Number($("resample-rate").value),
    resample_bit_depth: Number($("resample-bits").value),
    resample_algorithm: $("resample-algo").value,
  });
  if (res.usb_safety_muted) {
    if (promptUsbDisabledConfirm()) {
      await api("/api/volume/confirm-usb-disabled", "POST");
      showToast("Audio settings applied (USB 0 dB pass-through confirmed)");
      return;
    }
    showToast("Output muted until USB 0 dB pass-through is confirmed");
    return;
  }
  showToast("Audio settings applied");
});

$("audio-device").addEventListener("change", (e) => {
  refreshResampleCapabilities(e.target.value);
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

$("btn-test-tone").addEventListener("click", async () => {
  const isStopping = $("btn-test-tone").classList.contains("btn-danger");
  if (!isStopping && currentVolumeMode === "disabled") {
    const ok = window.confirm(
      "Volume Mode is set to Disabled (0 dB full-scale pass-through).\n\n" +
      "The 440 Hz test tone will play at 0 dBFS. Make sure your downstream amplifier or preamp volume is turned down before continuing."
    );
    if (!ok) return;
  }
  const res = await api("/api/audio/test-tone", "POST");
  if (res.playing) {
    const msg = currentVolumeMode === "disabled"
      ? "Test tone playing (0 dB pass-through)"
      : `Test tone playing at ${res.volume.current_volume}% volume`;
    showToast(msg);
  } else {
    showToast("Test tone stopped");
  }
});

$("save-volume").addEventListener("click", async () => {
  const mode = $("vol-mode").value;
  let confirmed = false;
  if (mode === "disabled" && isSelectedDeviceUsb()) {
    confirmed = promptUsbDisabledConfirm();
  }
  const res = await api("/api/config/volume", "PUT", {
    mode,
    curve: $("vol-curve").value,
    power_on_volume: Number($("vol-power-on").value),
    max_volume: Number($("vol-max").value),
    step_size: Number($("vol-step").value),
    hardware_mixer_name: $("vol-hw-mixer").value || "PCM",
    usb_disabled_confirmed: confirmed,
  });
  if (res.fallback_to_software) {
    $("vol-mode").value = res.volume?.mode || "software";
    currentVolumeMode = $("vol-mode").value;
    showToast("No hardware mixer available — switched to Software DSP volume control");
    alert(
      `Hardware volume mode is not supported by this audio device:\n\n${res.hardware_error || "No usable ALSA playback mixer found."}\n\nHydra Sound automatically switched to Software DSP (CamillaDSP) volume control so your volume limit remains protected.`
    );
  } else if (res.usb_safety_muted) {
    showToast("Output muted until USB 0 dB pass-through is confirmed");
  } else {
    showToast("Volume settings saved");
  }
});

$("btn-confirm-usb-disabled").addEventListener("click", async () => {
  if (!promptUsbDisabledConfirm()) return;
  await api("/api/volume/confirm-usb-disabled", "POST");
  showToast("USB 0 dB pass-through confirmed and unmuted");
});

$("save-services").addEventListener("click", async () => {
  const devName = $("renderer-device-name").value.trim() || "Hydra Sound";
  await api("/api/config/renderers", "PUT", {
    device_name: devName,
    lock_mode: $("lock-mode").value,
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
      auth_mode: $("qb-auth").value,
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
  const loggingEnabled = $("sys-logging-enabled").checked;
  await api("/api/config/system", "PUT", { github_repo: repo, logging_enabled: loggingEnabled });
  showToast("GitHub repository saved");
});

$("btn-check-releases").addEventListener("click", async () => {
  const repo = $("sys-github-repo").value.trim();
  if (!repo) {
    showToast("Enter GitHub Repository (owner/repo) first");
    return;
  }
  await api("/api/config/system", "PUT", {
    github_repo: repo,
    logging_enabled: $("sys-logging-enabled").checked,
  });
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
  const statusLabel = data.enabled ? "Enabled" : "Disabled";
  $("sys-logs-meta").textContent =
    `Logging: ${statusLabel} — Showing ${data.count} / ${data.max_entries || 2000} entries (${data.path || "log file"})`;
  const out = $("sys-logs-output");
  out.textContent = data.lines.length ? data.lines.join("\n") : "(Log is empty)";
  out.scrollTop = out.scrollHeight;
}

$("sys-logging-enabled").addEventListener("change", async () => {
  const enabled = $("sys-logging-enabled").checked;
  const repo = $("sys-github-repo").value.trim();
  await api("/api/config/system", "PUT", { github_repo: repo, logging_enabled: enabled });
  showToast(enabled ? "File logging enabled" : "File logging disabled (SD card protection)");
  await loadSystemLogs();
});

$("btn-refresh-logs").addEventListener("click", loadSystemLogs);

$("btn-clear-logs").addEventListener("click", async () => {
  await api("/api/system/logs/clear", "POST");
  await loadSystemLogs();
  showToast("System logs cleared");
});

// ======================== Room & Speaker EQ ========================
function getEditingPreset() {
  const pid = $("eq-preset-select")?.value;
  return (eqState.presets || []).find((p) => p.id === pid) || null;
}

function loadEQState(eq) {
  eqState = eq || { enabled: false, active_preset_id: null, presets: [] };
  if ($("eq-enabled")) $("eq-enabled").checked = Boolean(eqState.enabled);

  const quick = $("eq-quick-select");
  const sel = $("eq-preset-select");
  if (quick) {
    quick.innerHTML = '<option value="off">Off (Bit-Perfect)</option>';
    (eqState.presets || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      quick.appendChild(opt);
    });
    quick.value = eqState.enabled && eqState.active_preset_id ? eqState.active_preset_id : "off";
  }
  if ($("eq-quick-note")) {
    $("eq-quick-note").textContent = eqState.enabled && eqState.active_preset_id ? "Active" : "Bypassed";
  }
  if (sel) {
    const prev = sel.value;
    sel.innerHTML = "";
    (eqState.presets || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    });
    if (eqState.active_preset_id && (eqState.presets || []).some((p) => p.id === eqState.active_preset_id)) {
      sel.value = eqState.active_preset_id;
    } else if (prev && (eqState.presets || []).some((p) => p.id === prev)) {
      sel.value = prev;
    }
  }
  renderSelectedEQPreset();
}

function renderSelectedEQPreset() {
  const preset = getEditingPreset();
  const tbody = $("eq-bands-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!preset) {
    $("eq-preset-meta").textContent = "No presets yet — click New or import a REW / rePhase file below.";
    $("eq-delay-left").value = 0;
    $("eq-delay-right").value = 0;
    drawEQChart([]);
    return;
  }
  $("eq-target-curve").value = preset.target_curve || "harman";
  $("eq-delay-left").value = preset.left_delay_ms || 0;
  $("eq-delay-right").value = preset.right_delay_ms || 0;

  const firInfo = (preset.left_impulse_path || preset.right_impulse_path)
    ? ` · FIR: ${preset.impulse_taps || "?"} taps @ ${preset.impulse_sample_rate || "?"} Hz`
    : "";
  $("eq-preset-meta").textContent =
    `Source: ${preset.source_type}${firInfo} · Bands: ${(preset.bands || []).length}`;

  (preset.bands || []).forEach((b, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="eq-b-on" ${b.enabled ? "checked" : ""} /></td>
      <td><select class="eq-ch">
        <option value="both" ${b.channel === "both" ? "selected" : ""}>Both</option>
        <option value="left" ${b.channel === "left" ? "selected" : ""}>Left</option>
        <option value="right" ${b.channel === "right" ? "selected" : ""}>Right</option>
      </select></td>
      <td><select class="eq-type">
        <option value="Peaking" ${b.type === "Peaking" ? "selected" : ""}>Peaking</option>
        <option value="LowShelf" ${b.type === "LowShelf" ? "selected" : ""}>LowShelf</option>
        <option value="HighShelf" ${b.type === "HighShelf" ? "selected" : ""}>HighShelf</option>
        <option value="Lowpass" ${b.type === "Lowpass" ? "selected" : ""}>Lowpass</option>
        <option value="Highpass" ${b.type === "Highpass" ? "selected" : ""}>Highpass</option>
      </select></td>
      <td><input type="number" class="eq-freq" min="10" max="24000" step="0.5" value="${b.freq}" /></td>
      <td><input type="number" class="eq-gain" min="-24" max="12" step="0.1" value="${b.gain}" /></td>
      <td><input type="number" class="eq-q" min="0.1" max="25" step="0.05" value="${b.q}" /></td>
      <td><button class="btn btn-sm btn-outline eq-del" data-idx="${idx}">×</button></td>
    `;
    tr.querySelectorAll("input, select").forEach((inp) => {
      inp.addEventListener("change", () => {
        preset.bands = collectBandsFromTable();
        drawEQChart(preset);
      });
    });
    tr.querySelector(".eq-del").addEventListener("click", () => {
      preset.bands.splice(idx, 1);
      renderSelectedEQPreset();
    });
    tbody.appendChild(tr);
  });
  drawEQChart(preset);
}

function collectBandsFromTable() {
  const rows = document.querySelectorAll("#eq-bands-body tr");
  return Array.from(rows).map((tr) => ({
    enabled: tr.querySelector(".eq-b-on").checked,
    channel: tr.querySelector(".eq-ch").value,
    type: tr.querySelector(".eq-type").value,
    freq: Number(tr.querySelector(".eq-freq").value) || 1000,
    gain: Number(tr.querySelector(".eq-gain").value) || 0,
    q: Number(tr.querySelector(".eq-q").value) || 1.0,
  }));
}

async function drawEQChart(preset) {
  const canvas = $("eq-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const bands = Array.isArray(preset) ? preset : (preset?.bands || []);
  const targetCurve = (!Array.isArray(preset) && preset?.target_curve) || "harman";
  const peakImp = (!Array.isArray(preset) && preset?.impulse_peak_gain_db) || 0;
  const data = await api("/api/eq/preview", "POST", {
    bands,
    target_curve: targetCurve,
    impulse_peak_gain_db: peakImp,
  });

  const padL = 42, padR = 16, padT = 16, padB = 26;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const minDb = -18, maxDb = 18;
  const xOf = (f) => padL + ((Math.log10(Math.max(20, Math.min(20000, f))) - Math.log10(20)) / 3.0) * plotW;
  const yOf = (db) => padT + ((maxDb - Math.max(minDb, Math.min(maxDb, db))) / (maxDb - minDb)) * plotH;

  // Grid
  ctx.strokeStyle = "#30363d";
  ctx.fillStyle = "#8b949e";
  ctx.font = "11px sans-serif";
  ctx.lineWidth = 1;
  [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach((f) => {
    const x = xOf(f);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, H - padB);
    ctx.stroke();
    ctx.fillText(f >= 1000 ? `${f / 1000}k` : `${f}`, x - 8, H - 8);
  });
  [-12, -6, 0, 6, 12].forEach((db) => {
    const y = yOf(db);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(W - padR, y);
    ctx.stroke();
    ctx.fillText(`${db > 0 ? "+" : ""}${db}`, 6, y + 4);
  });

  const plotLine = (freqs, vals, color, width = 2, dash = []) => {
    if (!freqs || !vals || !freqs.length) return;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    freqs.forEach((f, i) => {
      const x = xOf(f);
      const y = yOf(vals[i]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  };

  plotLine(data.freqs, data.target, "#8b949e", 1.5, [4, 4]);
  if (!Array.isArray(preset) && preset?.measurement_freqs?.length) {
    plotLine(preset.measurement_freqs, preset.measurement_spl, "#f59e0b", 1.5);
    const pred = data.freqs.map((f, i) => {
      const m = preset.measurement_spl[Math.min(i, preset.measurement_spl.length - 1)] || 0;
      return m + (data.biquad_left[i] || 0);
    });
    plotLine(data.freqs, pred, "#3fb950", 2);
  }
  plotLine(data.freqs, data.biquad_left, "#38bdf8", 2);
  if (data.biquad_right.some((v, i) => Math.abs(v - data.biquad_left[i]) > 0.05)) {
    plotLine(data.freqs, data.biquad_right, "#f472b6", 2, [5, 3]);
  }
  if ($("eq-chart-legend")) {
    $("eq-chart-legend").textContent = `Headroom Pre-Gain: -${data.headroom_db > 0 ? (data.headroom_db + 0.1).toFixed(1) : "0.0"} dB`;
  }
}

$("eq-quick-select")?.addEventListener("change", async () => {
  const val = $("eq-quick-select").value;
  const enabled = val !== "off";
  const next = {
    ...eqState,
    enabled,
    active_preset_id: enabled ? val : eqState.active_preset_id,
  };
  const res = await api("/api/eq", "PUT", next);
  if (res.ok) {
    loadEQState(res.eq);
    showToast(enabled ? "Room EQ enabled" : "Room EQ bypassed (Bit-Perfect)");
  }
});

$("eq-enabled")?.addEventListener("change", async () => {
  const enabled = $("eq-enabled").checked;
  const next = { ...eqState, enabled };
  const res = await api("/api/eq", "PUT", next);
  if (res.ok) {
    loadEQState(res.eq);
    showToast(enabled ? "Room EQ enabled" : "Room EQ disabled");
  }
});

$("eq-preset-select")?.addEventListener("change", renderSelectedEQPreset);

$("btn-eq-add-band")?.addEventListener("click", () => {
  const preset = getEditingPreset();
  if (!preset) { showToast("Create or import a preset first"); return; }
  preset.bands = collectBandsFromTable();
  if (preset.bands.length >= 24) { showToast("Maximum 24 bands per preset"); return; }
  preset.bands.push({ channel: "both", type: "Peaking", freq: 1000, gain: 0, q: 1.4, enabled: true });
  renderSelectedEQPreset();
});

$("btn-eq-new")?.addEventListener("click", async () => {
  const name = prompt("New EQ Preset Name:", "Custom Room EQ");
  if (!name) return;
  const res = await api("/api/eq/presets", "POST", {
    id: "",
    name: name.trim(),
    source_type: "manual",
    target_curve: "harman",
    bands: [{ channel: "both", type: "Peaking", freq: 80, gain: -3.0, q: 2.0, enabled: true }],
  });
  if (res.ok) { loadEQState(res.eq); showToast(`Created preset "${name.trim()}"`); }
});

$("btn-eq-rename")?.addEventListener("click", async () => {
  const preset = getEditingPreset();
  if (!preset) return;
  const name = prompt("Rename EQ Preset:", preset.name);
  if (!name) return;
  preset.name = name.trim();
  const res = await api("/api/eq/presets", "POST", preset);
  if (res.ok) { loadEQState(res.eq); showToast("Preset renamed"); }
});

$("btn-eq-delete")?.addEventListener("click", async () => {
  const preset = getEditingPreset();
  if (!preset || !confirm(`Delete preset "${preset.name}"?`)) return;
  const res = await api(`/api/eq/presets/${encodeURIComponent(preset.id)}`, "DELETE");
  if (res.ok) { loadEQState(res.eq); showToast("Preset deleted"); }
});

$("btn-eq-save-preset")?.addEventListener("click", async () => {
  const preset = getEditingPreset();
  if (!preset) return;
  preset.target_curve = $("eq-target-curve").value;
  preset.left_delay_ms = Number($("eq-delay-left").value) || 0;
  preset.right_delay_ms = Number($("eq-delay-right").value) || 0;
  preset.bands = collectBandsFromTable();
  const res = await api("/api/eq/presets", "POST", preset);
  if (res.ok) { loadEQState(res.eq); showToast("EQ preset saved & applied"); }
});

function readFileAsText(input) {
  return new Promise((resolve, reject) => {
    const f = input?.files?.[0];
    if (!f) { resolve(null); return; }
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = reject;
    r.readAsText(f);
  });
}

function readFileAsBase64(input) {
  return new Promise((resolve, reject) => {
    const f = input?.files?.[0];
    if (!f) { resolve(null); return; }
    const r = new FileReader();
    r.onload = () => {
      const res = String(r.result || "");
      resolve({ name: f.name, b64: res.includes(",") ? res.split(",", 2)[1] : res });
    };
    r.onerror = reject;
    r.readAsDataURL(f);
  });
}

$("btn-eq-import-filters")?.addEventListener("click", async () => {
  const fileText = await readFileAsText($("eq-imp-flt-file"));
  const content = fileText || $("eq-imp-flt-text").value;
  if (!content.trim()) { showToast("Select a REW filter file or paste filter text"); return; }
  const res = await api("/api/eq/import/filters", "POST", {
    preset_name: $("eq-imp-flt-name").value.trim() || "REW Filters",
    channel: $("eq-imp-flt-ch").value,
    content,
  });
  if (res.ok) { loadEQState(res.eq); showToast(`Imported ${res.imported_count} filter(s)`); }
  else showToast(res.detail || "Filter import failed");
});

$("btn-eq-import-meas")?.addEventListener("click", async () => {
  const fileText = await readFileAsText($("eq-imp-meas-file"));
  const content = fileText || $("eq-imp-meas-text").value;
  if (!content.trim()) { showToast("Select a REW measurement (.txt/.frd) or paste data"); return; }
  const res = await api("/api/eq/import/measurement", "POST", {
    preset_name: $("eq-imp-meas-name").value.trim() || "REW Auto-EQ",
    channel: $("eq-imp-meas-ch").value,
    target_curve: $("eq-imp-meas-target").value,
    content,
  });
  if (res.ok) { loadEQState(res.eq); showToast(`Generated ${res.imported_count} Auto-EQ filter(s)`); }
  else showToast(res.detail || "Measurement import failed");
});

$("btn-eq-import-fir")?.addEventListener("click", async () => {
  const f = await readFileAsBase64($("eq-imp-fir-file"));
  if (!f) { showToast("Select a rePhase .wav, .txt, or .bin file"); return; }
  const res = await api("/api/eq/import/impulse", "POST", {
    preset_name: $("eq-imp-fir-name").value.trim() || "rePhase FIR",
    side: $("eq-imp-fir-side").value,
    filename: f.name,
    content_b64: f.b64,
    sample_rate: Number($("eq-imp-fir-rate").value) || 44100,
  });
  if (res.ok) { loadEQState(res.eq); showToast("Imported rePhase FIR impulse"); }
  else showToast(res.detail || "FIR import failed");
});

loadInitialConfig().then(connectWebSocket);
