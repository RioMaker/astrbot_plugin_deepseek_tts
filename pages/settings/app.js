const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
let state = null;
let audioURL = null;
let busy = false;
let dirty = false;
const voices = { stella: ["海星", "Stella · 女声"], tide: ["暗潮", "Tide · 男声"] };

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}
function selectedVoice() {
  return document.querySelector('input[name="voice"]:checked').value;
}
function updateVoice() {
  const [label, kind] = voices[selectedVoice()];
  $("preview-voice").textContent = label;
  $("preview-voice-kind").textContent = kind;
  $("preview-title").textContent = selectedVoice() === "stella" ? "听听她的声音" : "听听他的声音";
}
function updateMode() {
  $("mode-help").textContent = $("reply-mode").value === "automatic"
    ? "普通 AI 回复会自动朗读；流式回复在文字完成后追加语音。请关闭 AstrBot 内置 TTS，以免重复转换。"
    : "可可子可通过语音工具回复，也可以用 /ds语音 手动朗读。";
}
function countText() {
  $("text-count").textContent = `${Array.from($("preview-text").value).length} / 100`;
}
function setBusy(value) {
  busy = value;
  $("settings-fields").disabled = value || !state;
  $("preview").disabled = value || !state;
  $("preview-text").disabled = value || !state;
}
function render(next, initial = false) {
  state = next;
  const s = state.settings;
  for (const [id, field] of [["enabled", "enabled"], ["filter-markdown", "filter_markdown"], ["allow-private", "allow_private"]]) $(id).checked = s[field];
  document.querySelector(`input[name="voice"][value="${s.voice_id}"]`).checked = true;
  $("reply-mode").value = s.auto_reply ? "automatic" : "on_demand";
  $("output-mode").value = s.output_mode;
  $("max-chars").value = s.max_chars;
  $("timeout").value = s.timeout_seconds;
  $("cooldown").value = s.cooldown_seconds;
  $("groups").value = s.allowed_group_ids.join("\n");
  $("api-key").value = "";
  $("clear-key").checked = false;
  $("api-key").placeholder = s.key_configured ? "已配置；留空保留，输入新密钥以替换" : "填写 DeepSeek 官方 API Key";
  $("key-state").textContent = s.key_source === "environment" ? "来自环境变量" : s.key_configured ? "已保存" : "尚未配置";
  $("connection").textContent = !s.enabled ? "语音回复已关闭" : s.key_configured ? "已配置 · 可以使用" : "等待配置密钥";
  $("connection").className = `badge${s.key_configured && s.enabled ? " ready" : ""}`;
  if (initial) $("preview-text").value = state.preview_text;
  dirty = false;
  $("save-state").textContent = "设置已同步";
  updateVoice(); updateMode(); countText(); setBusy(false);
}
function changes() {
  return {
    enabled: $("enabled").checked, api_key: $("api-key").value.trim(),
    voice_id: selectedVoice(), auto_reply: $("reply-mode").value === "automatic",
    output_mode: $("output-mode").value, filter_markdown: $("filter-markdown").checked,
    max_chars: Number($("max-chars").value), timeout_seconds: Number($("timeout").value),
    cooldown_seconds: Number($("cooldown").value), allow_private: $("allow-private").checked,
    allowed_group_ids: $("groups").value.split(/[\s,，;；]+/).filter(Boolean),
  };
}
$("settings-form").addEventListener("input", () => {
  dirty = true;
  $("save-state").textContent = "有未保存的修改";
  updateVoice(); updateMode();
});
$("preview-text").addEventListener("input", countText);
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !state || !$("settings-form").reportValidity()) return;
  const payload = { revision: state.revision, changes: changes(), clear_api_key: $("clear-key").checked };
  setBusy(true); $("save").textContent = "保存中…";
  try {
    render(await bridge.apiPost("settings/save", payload));
    notice("设置已保存，新的语音请求会立即使用这些设置。");
  } catch (error) { notice(error.message || "保存失败，请重试。", true); }
  finally { setBusy(false); $("save").textContent = "保存设置"; }
});
$("preview").addEventListener("click", async () => {
  if (busy || !state) return;
  if ($("api-key").value.trim() || $("clear-key").checked) { notice("请先保存密钥修改，再生成试听。", true); return; }
  if (!state.settings.key_configured) { notice("请先填写并保存 DeepSeek API Key。", true); return; }
  const text = $("preview-text").value.trim();
  if (!text || Array.from(text).length > 100) { notice("请输入 1～100 字的试听文案。", true); return; }
  const voice = selectedVoice();
  const previousDirty = dirty;
  setBusy(true); notice("正在生成试听，请稍候…");
  $("preview").textContent = "正在合成…";
  document.querySelector(".preview-panel").classList.add("generating");
  $("player").pause();
  try {
    const result = await bridge.apiPost("preview", { text, voice_id: voice });
    if (result.content_type !== "audio/wav" || typeof result.audio_base64 !== "string" || result.audio_base64.length > 3 * 1024 * 1024) throw new Error("试听音频格式无效。");
    const binary = atob(result.audio_base64);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const nextURL = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
    if (audioURL) URL.revokeObjectURL(audioURL);
    audioURL = nextURL;
    $("player").src = audioURL;
    $("download").href = audioURL;
    $("download").download = `deepseek-${voice}.wav`;
    $("audio-info").textContent = `${voices[voice][0]} · ${result.duration_seconds} 秒`;
    $("player-area").hidden = false;
    notice(previousDirty ? "试听已生成。音色或其他设置尚未保存。" : "试听已生成，可以播放或下载。");
    try { await $("player").play(); } catch { /* Some browsers require another user gesture. */ }
  } catch (error) { notice(error.message || "试听失败，请稍后重试。", true); }
  finally {
    setBusy(false); $("preview").textContent = "▶ 生成试听";
    document.querySelector(".preview-panel").classList.remove("generating");
  }
});
window.addEventListener("beforeunload", () => { if (audioURL) URL.revokeObjectURL(audioURL); });

async function initialize() {
  if (!bridge) throw new Error("请从 AstrBot 管理面板的 DeepSeek TTS 插件详情中打开“语音设置与试听”。");
  await bridge.ready();
  render(await bridge.apiGet("state"), true);
}
initialize().catch((error) => {
  $("connection").textContent = "连接未完成";
  $("connection").className = "badge error";
  notice(error.message || "无法读取配置，请刷新页面。", true);
});
