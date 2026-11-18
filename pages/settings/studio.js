const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (name) => document.getElementById(`studio-${name}`);
const providers = { volcengine: "volcengine_voice_api_key", s1: "s1_cover_api_key" };
const sourceLabels = { config: "已保存", environment: "来自环境变量", none: "未配置" };
const active = (job) => job && !["done", "failed"].includes(job.status);
const statuses = {
  preparing: "正在准备素材…", designing: "正在设计音色，请稍候…",
  uploading: "正在上传所选歌曲片段和参考样音…", queued: "翻唱任务已排队…",
  running: "正在换声与混音，请稍候…",
};
const busy = new Set();
const players = {};
let state;
let refreshId = 0;
let poll;
let removeTarget;

function message(name, text, error = false) {
  const node = $(name);
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("error", error);
  node.hidden = !text;
}

function fill(select, options, preferred, empty) {
  const chosen = preferred || select.value;
  select.replaceChildren(...options.map(([value, label]) => new Option(label, value)));
  if (!options.length) select.add(new Option(empty, ""));
  if (options.some(([value]) => chosen === value)) select.value = chosen;
}

function selectedSong() {
  return state?.songs.find(({ id }) => id === $("song").value);
}

const secondsLabel = (seconds) => `${Number(seconds.toFixed(2))} 秒`;

function renderLength(reset = false) {
  const song = selectedSong();
  const start = $("start");
  const duration = $("duration");
  start.disabled = duration.disabled = !song;
  if (!song) return;
  start.max = Math.max(0, Math.floor(song.seconds + 0.01) - 10);
  if (reset) start.value = 0;
  duration.max = Math.min(300, Math.max(10, Math.floor(song.seconds - Number(start.value) + 0.01)));
  if (reset && Number(duration.value) > Number(duration.max)) duration.value = duration.max;
}

function renderSongs(preferred = "") {
  const folded = (text) => text.normalize("NFKC").toLowerCase().trim();
  const query = folded($("song-query").value);
  const previous = $("song").value;
  fill($("song"), state.songs.filter(({ title }) => folded(title).includes(query)).map(
    ({ id, title, seconds }) => [id, `${title} · ${secondsLabel(seconds)}`],
  ), preferred, query ? "未找到匹配的歌曲" : "请先上传歌曲");
  if (previous !== $("song").value) $("rights").checked = false;
  renderSong(previous !== $("song").value);
}

function renderSong(reset = false) {
  const song = selectedSong();
  $("song-info").textContent = song ? `原唱素材 · ${secondsLabel(song.seconds)}` : "请从曲库中选择一首歌。";
  renderLength(reset);
  controls();
}

function controls() {
  if (!state) return;
  const designActive = active(state.jobs.design) || busy.has("design");
  const coverActive = active(state.jobs.cover) || busy.has("cover");
  const voice = $("voices").value;
  const designConfigured = ["config", "environment"].includes(state.credentials[$("provider").value]);
  const coverConfigured = state.credentials.s1 !== "none" && state.ffmpeg_ready;
  $("design").disabled = Boolean(designActive) || !designConfigured;
  $("design").textContent = designActive ? "正在设计…" : designConfigured ? "提交音色设计" : "请先配置设计平台密钥";
  $("cover").disabled = Boolean(coverActive) || !coverConfigured || !selectedSong();
  $("cover").textContent = coverActive ? "正在生成…" : !state.ffmpeg_ready ? "等待服务端安装 FFmpeg" : coverConfigured ? "生成翻唱" : "请先配置翻唱平台密钥";
  $("import").disabled = !state.ffmpeg_ready || busy.has("import");
  $("save-keys").disabled = busy.has("keys");
  $("voice-audio").disabled = !voice || busy.has("voice-audio");
  $("use-voice").disabled = !voice;
  $("remove-voice").disabled = !voice || Boolean(designActive || coverActive) || busy.has("remove-voice");
  $("remove-song").disabled = !selectedSong() || Boolean(coverActive) || busy.has("remove-song");
  $("cover-audio").disabled = busy.has("cover-audio");
}

function render({ voice, song } = {}) {
  const { credentials, designers, presets, default_voice: defaultVoice, voices, jobs } = state;
  for (const [name, source] of Object.entries(credentials)) {
    $(`${name}-state`).textContent = sourceLabels[source];
    $(`${name}-key`).placeholder = source === "none" ? "填写平台 API Key" : "已配置；留空保留";
  }
  $("connection").textContent = `火山 ${sourceLabels[credentials.volcengine]} · 翻唱 ${sourceLabels[credentials.s1]}`;
  $("tools").textContent = state.ffmpeg_ready ? "FFmpeg 和 ffprobe 已就绪。" : "服务端未检测到 FFmpeg / ffprobe：安装并加入 PATH 后刷新。音色设计不依赖它们。";
  $("song-count").textContent = `${state.songs.length} 首`;
  fill($("provider"), Object.entries(designers));
  fill($("voices"), voices.map(({ id, name }) => [id, name]), voice, "暂无已设计音色");
  fill($("cover-voice"), [
    ...Object.entries(presets).map(([id, name]) => [id, `默认｜${name}${state.has_preset_key ? "" : "（需 DeepSeek 密钥）"}`]),
    ...voices.map(({ id, name }) => [id, `设计｜${name}`]),
  ], $("cover-voice").value || defaultVoice);
  $("voices").disabled = !voices.length;
  if (players.voice?.id !== $("voices").value) clearPlayer("voice");
  if (players.cover?.id !== jobs.cover?.id) clearPlayer("cover");
  renderSongs(song);
  const design = jobs.design;
  const cover = jobs.cover;
  const remaining = Number.isInteger(design?.remaining) ? `剩余设计次数：${design.remaining}。` : "";
  message("design-job", !design ? "" : design.error || (design.status === "done" ? `音色已保存，可直接用于翻唱。${remaining}` : statuses[design.status] || "任务未完成。"), design?.status === "failed");
  message("cover-job", !cover ? "" : cover.error || (cover.status === "done" ? `${cover.title} · ${cover.voice_name} · 所选片段 ${cover.seconds} 秒 · 已完成` : statuses[cover.status] || "任务未完成。"), cover?.status === "failed");
  $("cover-result").hidden = cover?.status !== "done";
}

async function refresh(preferred = {}) {
  clearTimeout(poll);
  const requestId = ++refreshId;
  try {
    const updated = await bridge.apiGet("studio/state");
    if (requestId !== refreshId) return;
    const first = !state;
    if (!preferred.voice && updated.jobs.design?.status === "done" && state?.jobs.design?.status !== "done") {
      preferred.voice = updated.jobs.design.voice_id;
    }
    state = updated;
    if (first) {
      $("reference").value = state.reference_text;
      for (const section of ["keys", "design", "import", "cover"]) $(`${section}-fields`).disabled = false;
    }
    render(preferred);
    message("message", "");
  } catch (error) {
    if (requestId === refreshId) message("message", error.message || "无法读取工作台状态，请刷新页面。", true);
  } finally {
    if (requestId === refreshId && state && Object.values(state.jobs).some(active)) poll = setTimeout(refresh, 2200);
  }
}

async function run(action, area, work, pending = "") {
  if (!state || busy.has(action)) return;
  busy.add(action);
  controls();
  message(area, pending);
  try { await work(); }
  catch (error) { message(area, error.message || "操作未完成，请重试。", true); }
  finally { busy.delete(action); controls(); }
}

function onForm(name, work) {
  $(`${name}-form`).addEventListener("submit", (event) => { event.preventDefault(); work(); });
}

onForm("keys", () => run("keys", "keys-message", async () => {
  const payload = {};
  for (const [name, field] of Object.entries(providers)) {
    payload[field] = $(`${name}-key`).value.trim();
    payload[`clear_${field}`] = $(`clear-${name}`).checked;
  }
  const credentials = await bridge.apiPost("studio/keys", payload);
  $("keys-form").reset();
  state.credentials = credentials;
  render();
  message("keys-message", "平台密钥设置已保存。");
}));

$("provider").addEventListener("change", controls);
$("language").addEventListener("change", () => {
  const english = "The evening breeze drifts through the trees. We walk home beneath the warm streetlights, listening to the city grow quiet. It is an ordinary day, but one I would like to remember.";
  const reference = $("reference");
  if (!reference.value || [state.reference_text, english].includes(reference.value)) {
    reference.value = $("language").value === "1" ? english : state.reference_text;
  }
});

onForm("design", () => run("design", "design-message", async () => {
  const job = await bridge.apiPost("studio/design", {
    provider: $("provider").value, speaker_id: $("slot").value.trim(), name: $("name").value.trim(),
    language: Number($("language").value), prompt: $("prompt").value.trim(),
    text: $("reference").value.trim(), replace_slot: $("replace").checked,
  });
  state.jobs.design = job;
  $("replace").checked = false;
  message("design-message", "");
  render();
  await refresh();
}, "正在提交音色设计…"));

$("song-file").addEventListener("change", () => {
  $("song-name").value = ($("song-file").files[0]?.name || "").replace(/\.[^.]+$/, "").slice(0, 100);
});

onForm("import", () => run("import", "import-message", async () => {
  const file = $("song-file").files[0];
  const title = $("song-name").value.trim();
  const extension = file?.name.match(/\.(mp3|wav|m4a|flac)$/i)?.[0];
  if (!bridge.upload) throw new Error("当前 AstrBot 管理面板不支持插件上传，请升级后重试。");
  if (!extension || !file.size || file.size > 12 * 1024 * 1024) throw new Error("请选择不超过 12 MiB 的 MP3、WAV、M4A 或 FLAC 文件。");
  if (!title || /[/\\\u0000-\u001f]/u.test(title)) throw new Error("歌曲名称不能为空，也不能包含斜杠或控制字符。");
  const song = await bridge.upload("studio/songs/import", new File([file], `${title}${extension}`, { type: file.type }));
  $("import-form").reset();
  $("song-query").value = "";
  await refresh({ song: song.id });
  message("import-message", `已导入 ${song.title}。`);
}, "正在上传和检查音频…"));

$("song-query").addEventListener("input", () => renderSongs());
$("song-query").addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
$("song").addEventListener("change", () => { $("rights").checked = false; renderSong(true); });
$("start").addEventListener("input", () => renderLength());
$("cover-voice").addEventListener("change", () => { $("rights").checked = false; });

onForm("cover", () => run("cover", "cover-message", async () => {
  const job = await bridge.apiPost("studio/cover", {
    song_id: $("song").value, voice_id: $("cover-voice").value, start_seconds: Number($("start").value),
    duration_seconds: Number($("duration").value), rights_confirmed: $("rights").checked,
  });
  state.jobs.cover = job;
  $("rights").checked = false;
  message("cover-message", "");
  message("cover-audio-message", "");
  render();
  await refresh();
}, "正在提交翻唱任务…"));

function clearPlayer(kind) {
  const previous = players[kind];
  if (!previous) return;
  const player = $(`${kind}-player`);
  player.pause();
  player.removeAttribute("src");
  player.load();
  $(`${kind}-download`).removeAttribute("href");
  $(`${kind}-player-area`).hidden = true;
  URL.revokeObjectURL(previous.url);
  delete players[kind];
}

async function loadPlayer(kind, id, route, name, current) {
  const { content_type: type, audio_base64: encoded } = await bridge.apiGet(route);
  if (id !== current()) return;
  const limits = { "audio/wav": ["wav", 2], "audio/mpeg": ["mp3", kind === "voice" ? 2 : 16] };
  const format = limits[type];
  if (!format || (kind === "cover" && type !== "audio/mpeg") || typeof encoded !== "string" || !encoded.length || encoded.length > 4 * Math.ceil(format[1] * 1024 * 1024 / 3)) {
    throw new Error("返回的音频格式无效或超出大小限制。");
  }
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  clearPlayer(kind);
  const url = URL.createObjectURL(new Blob([bytes], { type }));
  players[kind] = { id, url };
  $(`${kind}-player`).src = url;
  $(`${kind}-download`).href = url;
  $(`${kind}-download`).download = `${name}.${format[0]}`;
  $(`${kind}-player-area`).hidden = false;
}

$("voices").addEventListener("change", () => { clearPlayer("voice"); message("voices-message", ""); controls(); });
$("use-voice").addEventListener("click", () => {
  $("cover-voice").value = $("voices").value;
  $("rights").checked = false;
  $("cover-voice").focus();
});
$("voice-audio").addEventListener("click", () => run("voice-audio", "voices-message", async () => {
  const voice = state.voices.find(({ id }) => id === $("voices").value);
  if (voice) await loadPlayer("voice", voice.id, `studio/voices/${voice.id}/audio`, voice.name, () => $("voices").value);
}));
$("cover-audio").addEventListener("click", () => run("cover-audio", "cover-audio-message", async () => {
  const job = state.jobs.cover;
  await loadPlayer("cover", job.id, `studio/cover/${job.id}/audio`, `${job.title} - ${job.voice_name}`, () => state.jobs.cover?.id);
}));

const dialog = $("remove-dialog");
function confirmRemoval(kind, item) {
  if (!item) return;
  const name = kind === "voice" ? item.name : item.title;
  removeTarget = { kind, id: item.id };
  $("remove-description").textContent = `「${name}」将从本地工作台移除。${kind === "voice" ? "这不会释放火山引擎槽位。" : ""}`;
  dialog.returnValue = "";
  dialog.showModal();
}
$("remove-voice").addEventListener("click", () => confirmRemoval("voice", state.voices.find(({ id }) => id === $("voices").value)));
$("remove-song").addEventListener("click", () => confirmRemoval("song", selectedSong()));
dialog.addEventListener("close", () => {
  const target = removeTarget;
  removeTarget = undefined;
  if (!target || dialog.returnValue !== "remove") return;
  const area = target.kind === "voice" ? "voices-message" : "cover-message";
  run(`remove-${target.kind}`, area, async () => {
    await bridge.apiPost(`studio/${target.kind}s/${target.id}/remove`, {});
    await refresh();
    message(area, "已移除本地素材。");
  });
});

document.addEventListener("deepseek-settings-saved", () => { if (state) refresh(); });
window.addEventListener("beforeunload", () => { clearTimeout(poll); for (const { url } of Object.values(players)) URL.revokeObjectURL(url); });

async function initialize() {
  if (!bridge) return;
  await bridge.ready();
  await refresh();
}
initialize().catch(() => message("message", "无法连接工作台，请从 AstrBot 管理面板重新打开。", true));
