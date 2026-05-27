const form = document.querySelector("#compressForm");
const videoInput = document.querySelector("#videoInput");
const dropzone = document.querySelector("#dropzone");
const fileHint = document.querySelector("#fileHint");
const submitButton = document.querySelector("#submitButton");
const jobList = document.querySelector("#jobList");
const serverState = document.querySelector("#serverState");
const workerBadge = document.querySelector("#workerBadge");
const summaryConcurrency = document.querySelector("#summaryConcurrency");
const summaryReduction = document.querySelector("#summaryReduction");
const summaryJobs = document.querySelector("#summaryJobs");
const summaryJobMeta = document.querySelector("#summaryJobMeta");
const queueRunning = document.querySelector("#queueRunning");
const queueQueued = document.querySelector("#queueQueued");
const queueFinished = document.querySelector("#queueFinished");
const downloadAllButton = document.querySelector("#downloadAllButton");
const lanAddress = document.querySelector("#lanAddress");
const profileGrid = document.querySelector("#profileGrid");
const profileLabel = document.querySelector("#profileLabel");
const reductionInput = document.querySelector("#reductionInput");
const reductionValue = document.querySelector("#reductionValue");
const heroReduction = document.querySelector("#heroReduction");
const ratioExplain = document.querySelector("#ratioExplain");
const selectionFiles = document.querySelector("#selectionFiles");
const selectionSize = document.querySelector("#selectionSize");
const estimatedOutput = document.querySelector("#estimatedOutput");

let systemConfig = null;
let pollTimer = null;
let jobCache = [];

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatPercent(value) {
  return `${Number(value).toFixed(1)}%`;
}

function formatDuration(seconds) {
  if (!seconds) return "-";
  const total = Math.round(seconds);
  const hour = Math.floor(total / 3600);
  const minute = Math.floor((total % 3600) / 60);
  const second = total % 60;
  if (hour) return `${hour}:${String(minute).padStart(2, "0")}:${String(second).padStart(2, "0")}`;
  return `${minute}:${String(second).padStart(2, "0")}`;
}

function statusLabel(status) {
  return {
    queued: "排队中",
    running: "压缩中",
    stopping: "停止中",
    stopped: "已停止",
    done: "已完成",
    failed: "失败",
  }[status] || "处理中";
}

function updateReductionWidgets() {
  const reduction = Number(reductionInput.value);
  const remainFactor = 1 - reduction / 100;
  const example = Math.max(200 * remainFactor, 1);
  reductionValue.textContent = formatPercent(reduction);
  heroReduction.textContent = formatPercent(reduction);
  ratioExplain.textContent = `如果原视频是 200MB，按当前设置目标会落到大约 ${example.toFixed(1)}MB。`;
  updateSelectionSummary();
}

function updateSelectionSummary() {
  const files = [...videoInput.files];
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  const reduction = Number(reductionInput.value);
  const remainFactor = Math.max(0.08, 1 - reduction / 100);
  const estimatedBytes = totalBytes * remainFactor;

  selectionFiles.textContent = `${files.length} 个文件`;
  selectionSize.textContent = formatBytes(totalBytes);
  estimatedOutput.textContent = formatBytes(estimatedBytes);

  if (!files.length) {
    fileHint.textContent = "支持多个文件，系统会自动排队并按 3 路并发处理。";
  } else {
    fileHint.textContent = `${files.length} 个文件，总体积 ${formatBytes(totalBytes)}。`;
  }
}

function renderProfiles(contentProfiles) {
  if (!profileGrid || profileGrid.hidden) return;
  const entries = Object.entries(contentProfiles);
  profileGrid.innerHTML = entries
    .map(([key, profile], index) => {
      const checked = index === 0 ? "checked" : "";
      return `
        <label class="profile-option">
          <input type="radio" name="content_profile" value="${key}" ${checked} />
          <span class="profile-card">
            <strong>${profile.name}</strong>
            <span>${profile.description}</span>
          </span>
        </label>
      `;
    })
    .join("");
  const checked = profileGrid.querySelector('input[name="content_profile"]:checked');
  if (checked) {
    profileLabel.textContent = contentProfiles[checked.value].name;
  }
}

function renderSystem(config) {
  systemConfig = config;
  serverState.textContent = config.ffmpeg && config.ffprobe ? "服务正常" : "缺少 ffmpeg";
  workerBadge.textContent = String(config.max_concurrent_jobs);
  summaryConcurrency.textContent = String(config.max_concurrent_jobs);
  summaryReduction.textContent = formatPercent(config.default_target_reduction);
  reductionInput.value = config.default_target_reduction;
  lanAddress.textContent = config.lan_url || "请查看终端输出";
  renderProfiles(config.content_profiles);
  updateReductionWidgets();
}

function renderSummary(summary) {
  summaryJobs.textContent = String(summary.total);
  summaryJobMeta.textContent = `运行中 ${summary.running} / 排队 ${summary.queued} / 已完成 ${summary.finished}`;
  queueRunning.textContent = `运行中 ${summary.running}`;
  queueQueued.textContent = `排队中 ${summary.queued}`;
  queueFinished.textContent = `已完成 ${summary.finished}`;
  const canDownloadAll = summary.finished > 0 && summary.running + summary.queued === 0;
  downloadAllButton.classList.toggle("is-disabled", !canDownloadAll);
  downloadAllButton.toggleAttribute("aria-disabled", !canDownloadAll);
  if (canDownloadAll) {
    downloadAllButton.href = "/download-all";
    downloadAllButton.setAttribute("download", `compressed_videos_${new Date().toISOString().slice(0, 10)}.zip`);
  } else {
    downloadAllButton.removeAttribute("href");
    downloadAllButton.removeAttribute("download");
  }
}

function renderJobs(jobs) {
  jobCache = jobs;
  if (!jobs.length) {
    jobList.innerHTML = `
      <div class="job-empty">
        <strong>还没有压缩任务</strong>
        <p>上传视频后，这里会持续显示排队、压缩进度和下载入口。</p>
      </div>
    `;
    return;
  }

  jobList.innerHTML = jobs
    .map((job) => {
      const strategy = job.strategy || {};
      const status = job.status || "queued";
      const progress = Number(job.progress || 0);
      const canStop = ["queued", "running"].includes(status);
      const canDownload = status === "done" && job.output_name;
      const sourceInfo = [
        formatBytes(job.input_size),
        job.width && job.height ? `${job.width}x${job.height}` : null,
        job.duration ? formatDuration(job.duration) : null,
      ]
        .filter(Boolean)
        .join(" / ");
      const resultInfo =
        status === "done"
          ? `输出 ${formatBytes(job.compressed_size)}，实际缩小 ${formatPercent(job.saved_ratio || 0)}`
          : job.message || "等待处理中";

      return `
        <article class="job-row status-${status}">
          <div class="job-file">
            <strong>${job.filename}</strong>
            <span>${resultInfo}</span>
          </div>
          <div class="job-progress">
            <div class="mini-progress">
              <div class="mini-progress-bar" style="width:${progress}%"></div>
            </div>
            <span>${statusLabel(status)} ${status === "done" ? "" : `${progress.toFixed(1)}%`}</span>
          </div>
          <div class="job-actions">
            ${canDownload ? `<a class="job-download" href="/download/${encodeURIComponent(job.output_name)}" download="${job.output_name}">下载文件</a>` : ""}
            <button class="job-stop" data-stop="${job.id}" ${canStop ? "" : "disabled"}>停止</button>
          </div>
        </article>
      `;
    })
    .join("");
}

async function loadSystem() {
  const response = await fetch("/api/system");
  if (!response.ok) throw new Error("读取系统配置失败");
  const data = await response.json();
  renderSystem(data);
  renderSummary(data.summary);
}

async function loadJobs() {
  const response = await fetch("/api/jobs");
  if (!response.ok) throw new Error("读取任务列表失败");
  const data = await response.json();
  renderSummary(data.summary);
  renderJobs(data.jobs);
  const active = data.summary.running + data.summary.queued;
  if (!active && pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function stopJob(jobId) {
  await fetch(`/api/jobs/${jobId}/stop`, { method: "POST" });
  await loadJobs();
}

function ensurePolling() {
  if (pollTimer) return;
  pollTimer = window.setInterval(() => {
    loadJobs().catch((error) => {
      console.error(error);
    });
  }, 1600);
}

reductionInput.addEventListener("input", updateReductionWidgets);
videoInput.addEventListener("change", updateSelectionSummary);

if (profileGrid && !profileGrid.hidden) {
  profileGrid.addEventListener("change", (event) => {
    const input = event.target.closest('input[name="content_profile"]');
    if (!input || !systemConfig) return;
    profileLabel.textContent = systemConfig.content_profiles[input.value].name;
  });
}

["dragenter", "dragover"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  });
});

dropzone.addEventListener("drop", (event) => {
  if (!event.dataTransfer.files.length) return;
  videoInput.files = event.dataTransfer.files;
  updateSelectionSummary();
});

jobList.addEventListener("click", async (event) => {
  const stopButton = event.target.closest("[data-stop]");
  if (!stopButton) return;
  stopButton.disabled = true;
  await stopJob(stopButton.dataset.stop);
});

downloadAllButton.addEventListener("click", (event) => {
  if (downloadAllButton.getAttribute("aria-disabled") === "true") {
    event.preventDefault();
    fileHint.textContent = "当前用户还没有全部完成的压缩结果可下载。";
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!videoInput.files.length) {
    fileHint.textContent = "请先选择要压缩的视频文件。";
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "上传中...";
  const formData = new FormData(form);

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "创建任务失败");

    submitButton.disabled = false;
    submitButton.textContent = "开始批量压缩";
    videoInput.value = "";
    updateSelectionSummary();
    await loadJobs();
    ensurePolling();
  } catch (error) {
    submitButton.disabled = false;
    submitButton.textContent = "开始批量压缩";
    fileHint.textContent = error.message;
  }
});

Promise.all([loadSystem(), loadJobs()])
  .then(() => {
    if (jobCache.some((job) => ["queued", "running"].includes(job.status))) {
      ensurePolling();
    }
  })
  .catch((error) => {
    console.error(error);
    serverState.textContent = "服务异常";
    fileHint.textContent = "页面初始化失败，请检查后端服务是否启动。";
  });
