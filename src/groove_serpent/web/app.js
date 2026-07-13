"use strict";

let project = null;
let dirty = false;
let editRevision = 0;
let dragMarker = null;
let dragSnapshot = null;
let dragChanged = false;
let selectedMarker = null;
let currentPreviewEnd = null;
let boundaryLoop = null;
let analyzerBaselineMarkers = [];
let analyzerBaselineTrackCount = 0;
let analyzerTopologyCompatible = true;
const boundaryUndoStack = [];
const boundaryRedoStack = [];
const BOUNDARY_HISTORY_LIMIT = 100;
let recognitionReady = false;
let identifyingTrack = null;
let recognitionRequestId = 0;
let recognitionAbortController = null;
let releaseRequestId = 0;
let providerBusy = false;
let projectConflict = false;
let providerOperationId = 0;
let lookupReturnFocus = null;
let exportReturnFocus = null;
let evidencePayload = null;
let evidenceAbortController = null;
let evidenceRequestId = 0;
let evidenceLoadTimer = null;
let evidenceSelection = null;
let evidenceDragAnchor = null;
let listeningOutput = "archival";
let topologyProposalContext = null;
let evidenceOverrideFocus = null;
let restorationScan = null;
let restorationRecipe = null;
let restorationPreview = null;
let restorationSelectedCandidate = null;
const restorationDecisions = new Map();
const restorationPreviewed = new Set();

const canvas = document.getElementById("waveform");
const context = canvas.getContext("2d");
const statusElement = document.getElementById("status");
const audio = document.getElementById("audioPlayer");
audio.removeAttribute("src");
audio.load();
const trackRows = document.getElementById("trackRows");
const markerReadout = document.getElementById("markerReadout");
const markerSelect = document.getElementById("markerSelect");
const markerButtons = [
  ...document.querySelectorAll("[data-nudge-ms], [data-nudge-samples]"),
  document.getElementById("snapMarker"),
];
const auditionButtons = [
  document.getElementById("playBeforeMarker"),
  document.getElementById("playAcrossMarker"),
  document.getElementById("playAfterMarker"),
  document.getElementById("loopMarker"),
  document.getElementById("cancelBoundaryPreview"),
];
const auditionStatus = document.getElementById("auditionStatus");
const coverArt = document.getElementById("coverArt");
const coverPlaceholder = document.getElementById("coverPlaceholder");
const metadataResults = document.getElementById("metadataResults");
const releasePreview = document.getElementById("releasePreview");
const evidenceCanvas = document.getElementById("evidenceCanvas");
const evidenceContext = evidenceCanvas.getContext("2d");
const evidenceStatus = document.getElementById("evidenceStatus");
const speedInputs = {
  capture: document.getElementById("speedCaptureRpm"),
  intended: document.getElementById("speedIntendedRpm"),
  fine: document.getElementById("speedFineFactor"),
};
const restorationAudio = {
  before: document.getElementById("restorationOriginal"),
  proposed: document.getElementById("restorationProposed"),
  removed: document.getElementById("restorationRemoved"),
};

const metadataInputs = {
  artist: document.getElementById("metaArtist"),
  album: document.getElementById("metaAlbum"),
  album_artist: document.getElementById("metaAlbumArtist"),
  year: document.getElementById("metaYear"),
  genre: document.getElementById("metaGenre"),
  side: document.getElementById("metaSide"),
};

function setStatus(message, kind = "") {
  statusElement.textContent = message;
  statusElement.className = `status ${kind}`.trim();
}

function handleProjectConflict(error) {
  const integrityMessage = typeof error?.message === "string"
    ? error.message.toLowerCase()
    : "";
  const sourceMismatch = integrityMessage.includes("source audio changed")
    || integrityMessage.includes("source audio file could not be found")
    || integrityMessage.includes("predates source hashing");
  if (error?.status !== 409 && !sourceMismatch) return false;
  projectConflict = true;
  const integrity = document.getElementById("sourceIntegrity");
  integrity.textContent = sourceMismatch ? "SOURCE MISMATCH" : "PROJECT CONFLICT";
  integrity.className = "integrity-badge failed";
  document.getElementById("saveIdentity").textContent = "STATE · locked";
  providerOperationId += 1;
  setProviderBusy(true);
  audio.removeAttribute("src");
  audio.load();
  setStatus(
    sourceMismatch
      ? "Source identity verification failed. Playback and editing are disabled; restore the original capture and reload."
      : "This project changed in another tab or process. Reload the page before editing or saving again.",
    "error",
  );
  return true;
}

function markDirty() {
  editRevision += 1;
  dirty = true;
  setStatus("Unsaved changes", "busy");
  document.getElementById("saveIdentity").textContent = "STATE · unsaved changes";
}

function setProviderBusy(busy, trackNumber = null) {
  providerBusy = busy;
  if (busy) {
    if (evidenceLoadTimer !== null) {
      window.clearTimeout(evidenceLoadTimer);
      evidenceLoadTimer = null;
    }
    if (evidenceAbortController) {
      evidenceAbortController.abort();
      evidenceAbortController = null;
    }
    evidenceRequestId += 1;
  }
  if (busy && (!audio.paused || currentPreviewEnd !== null || boundaryLoop !== null)) {
    stopBoundaryPreview(false);
  }
  identifyingTrack = busy ? trackNumber : null;
  for (const input of Object.values(metadataInputs)) input.disabled = busy;
  for (const input of Object.values(speedInputs)) input.disabled = busy;
  markerSelect.disabled = busy;
  canvas.setAttribute("aria-disabled", String(busy));
  document.getElementById("saveButton").disabled = !project || busy;
  document.getElementById("exportButton").disabled = !project || busy;
  document.getElementById("findReleaseButton").disabled = !project || busy;
  document.getElementById("runReleaseSearch").disabled = busy;
  document.getElementById("refreshEvidence").disabled = busy || selectedMarker === null;
  for (const button of document.querySelectorAll("[data-provider-action]")) {
    button.disabled = busy;
  }
  for (const button of document.querySelectorAll("[data-intended-rpm]")) button.disabled = busy;
  document.getElementById("listenArchival").disabled = busy;
  document.getElementById("saveCheckpoint").disabled = busy || !project;
  if (project) {
    renderTrackTable();
    selectMarker(selectedMarker);
    refreshBoundaryControls();
    renderEvidenceDetails();
    updateSpeedView();
    renderPersistentHistory();
    renderRestorationCandidates();
  }
  if (!busy && project && selectedMarker !== null && !projectConflict) {
    scheduleEvidenceLoad(0);
  }
}

async function beginProviderOperation(trackNumber = null) {
  if (providerBusy) {
    setStatus("Another metadata or identification operation is already running", "busy");
    return null;
  }
  const id = ++providerOperationId;
  releaseRequestId += 1;
  setProviderBusy(true, trackNumber);
  if (dirty && !(await saveProject())) {
    if (id === providerOperationId) setProviderBusy(false);
    return null;
  }
  if (id !== providerOperationId) return null;
  return { id, revision: editRevision };
}

function endProviderOperation(id) {
  if (id !== providerOperationId) return;
  setProviderBusy(false);
}

function sampleToSeconds(sample) {
  return sample / project.source.sample_rate;
}

function secondsToSample(seconds) {
  return Math.round(seconds * project.source.sample_rate);
}

function formatTime(seconds, milliseconds = true) {
  const totalMilliseconds = Math.max(0, Math.round((Number(seconds) || 0) * 1000));
  const totalSeconds = Math.floor(totalMilliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const wholeSeconds = totalSeconds % 60;
  const fraction = totalMilliseconds % 1000;
  const base = hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}`
    : `${minutes}:${String(wholeSeconds).padStart(2, "0")}`;
  return milliseconds ? `${base}.${String(fraction).padStart(3, "0")}` : base;
}

function sourceSampleCount() {
  const count = project?.source?.sample_count;
  if (Number.isSafeInteger(count) && count >= 0) return count;
  const legacyCount = Math.round(
    (Number(project?.source?.duration_seconds) || 0) * (Number(project?.source?.sample_rate) || 0),
  );
  return Number.isSafeInteger(legacyCount) && legacyCount >= 0 ? legacyCount : 0;
}

function minimumTrackSamples() {
  return Math.max(1, Math.round(project.source.sample_rate * 0.25));
}

function cloneTracks(tracks = project.tracks) {
  return tracks.map((track) => ({ ...track }));
}

function captureBoundaryState() {
  return {
    tracks: cloneTracks(),
    selectedMarker,
    analyzerTopologyCompatible,
  };
}

function pushBoundaryUndo(state = captureBoundaryState()) {
  boundaryUndoStack.push(state);
  if (boundaryUndoStack.length > BOUNDARY_HISTORY_LIMIT) boundaryUndoStack.shift();
  boundaryRedoStack.length = 0;
}

function clearBoundaryHistory() {
  boundaryUndoStack.length = 0;
  boundaryRedoStack.length = 0;
  refreshBoundaryControls();
}

function stopBoundaryPreview(updateStatus = true) {
  boundaryLoop = null;
  currentPreviewEnd = null;
  audio.pause();
  const loopButton = document.getElementById("loopMarker");
  loopButton.setAttribute("aria-pressed", "false");
  loopButton.classList.remove("active");
  loopButton.textContent = "Loop across";
  if (updateStatus) auditionStatus.textContent = "Boundary audition stopped.";
  refreshBoundaryControls();
}

function normalizeTrackNumbersAndTimes() {
  project.tracks.forEach((track, index) => {
    track.number = index + 1;
    track.start_seconds = sampleToSeconds(track.start_sample);
    track.end_seconds = sampleToSeconds(track.end_sample);
  });
}

function restoreBoundaryState(state) {
  stopBoundaryPreview(false);
  project.tracks = cloneTracks(state.tracks);
  analyzerTopologyCompatible = state.analyzerTopologyCompatible !== false;
  normalizeTrackNumbersAndTimes();
  const maximumMarker = project.tracks.length;
  selectedMarker = Number.isInteger(state.selectedMarker)
    && state.selectedMarker >= 0
    && state.selectedMarker <= maximumMarker
    ? state.selectedMarker
    : null;
  renderTrackTable();
  selectMarker(selectedMarker);
}

function undoBoundaryEdit() {
  if (providerBusy || dragMarker !== null || !boundaryUndoStack.length) return;
  const previous = boundaryUndoStack.pop();
  boundaryRedoStack.push(captureBoundaryState());
  restoreBoundaryState(previous);
  markDirty();
  setStatus("Boundary edit undone", "success");
}

function redoBoundaryEdit() {
  if (providerBusy || dragMarker !== null || !boundaryRedoStack.length) return;
  const next = boundaryRedoStack.pop();
  boundaryUndoStack.push(captureBoundaryState());
  restoreBoundaryState(next);
  markDirty();
  setStatus("Boundary edit redone", "success");
}

function topologyMatchesAnalyzer() {
  return analyzerTopologyCompatible
    && project?.tracks?.length === analyzerBaselineTrackCount
    && markerSamples().length === analyzerBaselineMarkers.length;
}

function initializeBoundarySession({ preserveAnalyzerBaseline = false } = {}) {
  stopBoundaryPreview(false);
  dragMarker = null;
  dragSnapshot = null;
  dragChanged = false;
  boundaryUndoStack.length = 0;
  boundaryRedoStack.length = 0;
  if (!preserveAnalyzerBaseline || !topologyMatchesAnalyzer()) {
    analyzerBaselineMarkers = [...markerSamples()];
    analyzerBaselineTrackCount = project.tracks.length;
    analyzerTopologyCompatible = true;
  }
  refreshBoundaryControls();
}

function adjacentDurationReadout(index) {
  if (index === 0) {
    const next = project.tracks[0];
    return `next track ${String(next.number).padStart(2, "0")} ${formatTime(next.end_seconds - next.start_seconds)}`;
  }
  if (index === project.tracks.length) {
    const previous = project.tracks.at(-1);
    return `previous track ${String(previous.number).padStart(2, "0")} ${formatTime(previous.end_seconds - previous.start_seconds)}`;
  }
  const previous = project.tracks[index - 1];
  const next = project.tracks[index];
  return `track ${String(previous.number).padStart(2, "0")} ${formatTime(previous.end_seconds - previous.start_seconds)} · `
    + `track ${String(next.number).padStart(2, "0")} ${formatTime(next.end_seconds - next.start_seconds)}`;
}

function refreshBoundaryControls() {
  const markers = markerSamples();
  const hasSelection = Number.isInteger(selectedMarker)
    && selectedMarker >= 0
    && selectedMarker < markers.length;
  const internalSelection = hasSelection
    && selectedMarker > 0
    && selectedMarker < markers.length - 1;
  const canEditSelected = Boolean(project) && hasSelection && !providerBusy;

  for (const button of markerButtons) button.disabled = !canEditSelected;
  for (const button of auditionButtons.slice(0, 4)) button.disabled = !canEditSelected;
  document.getElementById("cancelBoundaryPreview").disabled = providerBusy
    || (boundaryLoop === null && currentPreviewEnd === null && audio.paused);
  document.getElementById("addMarkerAtPlayhead").disabled = !project || providerBusy;
  document.getElementById("deleteMarker").disabled = providerBusy || !internalSelection;
  document.getElementById("undoBoundary").disabled = providerBusy || dragMarker !== null || !boundaryUndoStack.length;
  document.getElementById("redoBoundary").disabled = providerBusy || dragMarker !== null || !boundaryRedoStack.length;

  const restore = document.getElementById("restoreAnalyzerMarker");
  const canRestore = canEditSelected
    && topologyMatchesAnalyzer()
    && analyzerBaselineMarkers[selectedMarker] !== markers[selectedMarker];
  restore.disabled = !canRestore;
  restore.title = topologyMatchesAnalyzer()
    ? "Move the selected marker back to its original analyzer position"
    : "Restore is available after the track count matches the analyzer again";
}

function metadataValue(key) {
  if (key === "side") {
    const sides = [...new Set(
      project.tracks.map((track) => String(track.side || "").trim()).filter(Boolean)
    )];
    if (sides.length > 1) return "";
    return project.metadata.side || sides[0] || "";
  }
  return project.metadata[key] || project.tracks[0]?.[key] || "";
}

function syncMetadataToTracks() {
  const previousArtist = project.metadata.artist || "";
  const trackSides = new Set(
    project.tracks.map((track) => String(track.side || "").trim()).filter(Boolean)
  );
  const preserveMixedSides = trackSides.size > 1 && !metadataInputs.side.value.trim();
  const values = {};
  for (const [key, input] of Object.entries(metadataInputs)) {
    values[key] = input.value.trim();
    if (values[key]) project.metadata[key] = values[key];
    else delete project.metadata[key];
  }
  for (const track of project.tracks) {
    track.album = values.album;
    track.album_artist = values.album_artist || values.artist;
    track.year = values.year;
    track.genre = values.genre;
    if (!preserveMixedSides) track.side = values.side;
    if (!track.artist || track.artist === previousArtist) {
      track.artist = values.artist;
    }
  }
}

function markerSamples() {
  if (!project?.tracks?.length) return [];
  return [
    project.tracks[0].start_sample,
    ...project.tracks.map((track) => track.end_sample),
  ];
}

function refreshMarkerSelect() {
  const markers = markerSamples();
  const options = [];
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Choose marker…";
  options.push(placeholder);
  markers.forEach((sample, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    const kind = index === 0
      ? "Music start"
      : (index === markers.length - 1 ? "Music end" : `Cut ${index}`);
    option.textContent = `${kind} · ${formatTime(sampleToSeconds(sample))}`;
    options.push(option);
  });
  markerSelect.replaceChildren(...options);
  markerSelect.value = selectedMarker === null ? "" : String(selectedMarker);
}

function selectMarker(index) {
  const markers = markerSamples();
  const normalized = Number.isInteger(index) && index >= 0 && index < markers.length
    ? index
    : null;
  if (normalized !== selectedMarker) evidenceOverrideFocus = null;
  if (normalized !== selectedMarker && (boundaryLoop !== null || currentPreviewEnd !== null)) {
    stopBoundaryPreview(false);
  }
  selectedMarker = normalized;
  const selectedSample = selectedMarker === null ? null : markers[selectedMarker];
  markerReadout.textContent = selectedSample === null
    ? "Select a marker to fine-tune it"
    : `Marker ${selectedMarker + 1}/${markers.length} · ${formatTime(sampleToSeconds(selectedSample))} · `
      + `sample ${selectedSample.toLocaleString()} · ${adjacentDurationReadout(selectedMarker)}`;
  auditionStatus.textContent = selectedSample === null
    ? "Select a marker to audition its boundary."
    : `Ready at ${formatTime(sampleToSeconds(selectedSample))}; audition windows are five seconds.`;
  refreshMarkerSelect();
  refreshBoundaryControls();
  drawWaveform();
  if (dragMarker === null) scheduleEvidenceLoad();
}

function updateMarker(index, sample, { recordHistory = true, markChange = true } = {}) {
  const markers = markerSamples();
  if (!Number.isInteger(index) || index < 0 || index >= markers.length) return false;
  const minimumSpacing = minimumTrackSamples();
  const minimum = index === 0 ? 0 : markers[index - 1] + minimumSpacing;
  const maximumSource = sourceSampleCount();
  const maximum = index === markers.length - 1
    ? maximumSource
    : markers[index + 1] - minimumSpacing;
  const bounded = Math.max(minimum, Math.min(maximum, Math.round(sample)));
  if (bounded === markers[index]) return false;
  const before = recordHistory ? captureBoundaryState() : null;

  if (index === 0) {
    project.tracks[0].start_sample = bounded;
    project.tracks[0].start_seconds = sampleToSeconds(bounded);
  } else if (index === markers.length - 1) {
    const last = project.tracks[project.tracks.length - 1];
    last.end_sample = bounded;
    last.end_seconds = sampleToSeconds(bounded);
  } else {
    const previous = project.tracks[index - 1];
    const next = project.tracks[index];
    previous.end_sample = bounded;
    previous.end_seconds = sampleToSeconds(bounded);
    next.start_sample = bounded;
    next.start_seconds = sampleToSeconds(bounded);
  }
  selectedMarker = index;
  if (recordHistory) pushBoundaryUndo(before);
  if (markChange) markDirty();
  drawWaveform();
  renderTrackTable();
  selectMarker(index);
  return true;
}

function canMergeTracks(left, right) {
  if (!left || !right || left.end_sample !== right.start_sample) return false;
  const leftSide = String(left.side || "").trim();
  const rightSide = String(right.side || "").trim();
  return !leftSide || !rightSide || leftSide === rightSide;
}

function splitTrackAtSample(sample, expectedTrackIndex = null) {
  if (providerBusy || !project) return false;
  const exactSample = Math.max(0, Math.min(sourceSampleCount(), Math.round(sample)));
  const minimumSpacing = minimumTrackSamples();
  const trackIndex = project.tracks.findIndex((track, index) =>
    (expectedTrackIndex === null || expectedTrackIndex === index)
    && exactSample >= track.start_sample + minimumSpacing
    && exactSample <= track.end_sample - minimumSpacing
  );
  if (trackIndex < 0) {
    setStatus("Place the playhead at least 250 ms inside the track you want to split", "error");
    return false;
  }

  const before = captureBoundaryState();
  const original = project.tracks[trackIndex];
  const resetIdentity = {
    confidence: 0,
    expected_duration_seconds: null,
    musicbrainz_recording_id: "",
    musicbrainz_track_id: "",
  };
  const left = {
    ...original,
    ...resetIdentity,
    end_sample: exactSample,
    end_seconds: sampleToSeconds(exactSample),
  };
  const right = {
    ...original,
    ...resetIdentity,
    title: `${original.title || `Track ${trackIndex + 1}`} (split)`,
    start_sample: exactSample,
    start_seconds: sampleToSeconds(exactSample),
  };
  project.tracks.splice(trackIndex, 1, left, right);
  analyzerTopologyCompatible = false;
  normalizeTrackNumbersAndTimes();
  selectedMarker = trackIndex + 1;
  pushBoundaryUndo(before);
  markDirty();
  renderTrackTable();
  selectMarker(selectedMarker);
  setStatus(`Added a marker at ${formatTime(sampleToSeconds(exactSample))}`, "success");
  return true;
}

function mergeAtBoundary(boundaryIndex) {
  if (providerBusy || !Number.isInteger(boundaryIndex)) return false;
  const left = project.tracks[boundaryIndex - 1];
  const right = project.tracks[boundaryIndex];
  if (!canMergeTracks(left, right)) {
    setStatus("Only contiguous tracks on the same side can be merged", "error");
    return false;
  }

  const before = captureBoundaryState();
  const merged = {
    ...left,
    end_sample: right.end_sample,
    end_seconds: right.end_seconds,
    side: left.side || right.side || "",
    confidence: 0,
    expected_duration_seconds: null,
    musicbrainz_recording_id: "",
    musicbrainz_track_id: "",
  };
  project.tracks.splice(boundaryIndex - 1, 2, merged);
  analyzerTopologyCompatible = false;
  normalizeTrackNumbersAndTimes();
  selectedMarker = Math.min(boundaryIndex, project.tracks.length);
  pushBoundaryUndo(before);
  markDirty();
  renderTrackTable();
  selectMarker(selectedMarker);
  setStatus(`Merged tracks at cut ${boundaryIndex}`, "success");
  return true;
}

function addMarkerAtPlayhead(expectedTrackIndex = null) {
  const seconds = Number(audio.currentTime);
  if (!Number.isFinite(seconds)) {
    setStatus("Playback has not supplied a usable playhead position", "error");
    return false;
  }
  return splitTrackAtSample(secondsToSample(seconds), expectedTrackIndex);
}

function deleteSelectedMarker() {
  const markers = markerSamples();
  if (selectedMarker === null || selectedMarker <= 0 || selectedMarker >= markers.length - 1) {
    setStatus("Select an internal cut marker to merge its adjacent tracks", "error");
    return false;
  }
  return mergeAtBoundary(selectedMarker);
}

function restoreSelectedAnalyzerMarker() {
  if (!topologyMatchesAnalyzer() || selectedMarker === null) return;
  const restored = updateMarker(selectedMarker, analyzerBaselineMarkers[selectedMarker]);
  if (restored) setStatus("Selected marker restored to the analyzer baseline", "success");
}

function auditionRange(startSeconds, endSeconds, label, loop = false) {
  if (providerBusy || selectedMarker === null) return;
  const exactDuration = sampleToSeconds(sourceSampleCount());
  const start = Math.max(0, Math.min(exactDuration, startSeconds));
  const end = Math.max(start, Math.min(exactDuration, endSeconds));
  if (end - start < 0.01) {
    setStatus("This marker is too close to the source edge for that audition", "error");
    return;
  }
  currentPreviewEnd = end;
  boundaryLoop = loop ? { start, end } : null;
  audio.currentTime = start;
  const loopButton = document.getElementById("loopMarker");
  loopButton.setAttribute("aria-pressed", String(loop));
  loopButton.classList.toggle("active", loop);
  loopButton.textContent = loop ? "Looping across" : "Loop across";
  auditionStatus.textContent = `${label}: ${formatTime(start)} to ${formatTime(end)}${loop ? ", looping" : ""}.`;
  refreshBoundaryControls();
  audio.play().catch(() => {
    stopBoundaryPreview(false);
    setStatus("Browser could not play this source format", "error");
  });
}

function auditionSelected(mode, loop = false) {
  if (selectedMarker === null) return;
  const markerSeconds = sampleToSeconds(markerSamples()[selectedMarker]);
  if (mode === "before") auditionRange(markerSeconds - 5, markerSeconds, "Playing before marker", loop);
  else if (mode === "after") auditionRange(markerSeconds, markerSeconds + 5, "Playing after marker", loop);
  else auditionRange(markerSeconds - 2.5, markerSeconds + 2.5, "Playing across marker", loop);
}

function resizeCanvas() {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawWaveform();
}

function xForSeconds(seconds, width) {
  const exactDuration = sampleToSeconds(sourceSampleCount());
  return exactDuration > 0 ? (seconds / exactDuration) * width : 0;
}

function secondsForX(x, width) {
  const exactDuration = sampleToSeconds(sourceSampleCount());
  return Math.max(0, Math.min(exactDuration, x / width * exactDuration));
}

function drawWaveform() {
  if (!project || !canvas.clientWidth) return;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const middle = height / 2;
  context.clearRect(0, 0, width, height);

  const tracks = project.tracks;
  tracks.forEach((track, index) => {
    const start = xForSeconds(track.start_seconds, width);
    const end = xForSeconds(track.end_seconds, width);
    context.fillStyle = index % 2 ? "rgba(255,255,255,0.025)" : "rgba(216,255,79,0.025)";
    context.fillRect(start, 0, Math.max(0, end - start), height);
  });

  const points = project.analysis.waveform || [];
  if (points.length) {
    context.beginPath();
    points.forEach((value, index) => {
      const x = index / Math.max(1, points.length - 1) * width;
      const y = middle - value * (middle - 16);
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    for (let index = points.length - 1; index >= 0; index -= 1) {
      const value = points[index];
      const x = index / Math.max(1, points.length - 1) * width;
      const y = middle + value * (middle - 16);
      context.lineTo(x, y);
    }
    context.closePath();
    context.fillStyle = "rgba(245, 241, 232, 0.72)";
    context.fill();
  }

  context.save();
  context.setLineDash([4, 5]);
  context.strokeStyle = "rgba(120, 169, 255, 0.62)";
  context.lineWidth = 1;
  for (const candidate of project.analysis.candidates || []) {
    if (candidate.selected) continue;
    const x = xForSeconds(candidate.cut_seconds, width);
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, height);
    context.stroke();
  }
  context.restore();

  const markers = markerSamples();
  markers.forEach((sample, index) => {
    const seconds = sampleToSeconds(sample);
    const x = xForSeconds(seconds, width);
    const selected = index === selectedMarker;
    context.strokeStyle = selected ? "#78a9ff" : "#d8ff4f";
    context.lineWidth = selected ? 4 : (index === 0 || index === markers.length - 1 ? 2 : 3);
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, height);
    context.stroke();
    context.fillStyle = selected ? "#78a9ff" : "#d8ff4f";
    context.beginPath();
    context.moveTo(x - 6, 0);
    context.lineTo(x + 6, 0);
    context.lineTo(x, 9);
    context.closePath();
    context.fill();
  });
}

function markerNearPointer(event) {
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const markers = markerSamples();
  let nearest = null;
  let distance = 13;
  markers.forEach((sample, index) => {
    const markerX = xForSeconds(sampleToSeconds(sample), rect.width);
    const current = Math.abs(markerX - x);
    if (current < distance) {
      nearest = index;
      distance = current;
    }
  });
  return nearest;
}

canvas.addEventListener("pointerdown", (event) => {
  if (providerBusy) return;
  const marker = markerNearPointer(event);
  if (marker === null) {
    selectMarker(null);
    return;
  }
  selectMarker(marker);
  dragMarker = marker;
  dragSnapshot = captureBoundaryState();
  dragChanged = false;
  canvas.setPointerCapture(event.pointerId);
});

canvas.addEventListener("pointermove", (event) => {
  if (dragMarker === null) return;
  const rect = canvas.getBoundingClientRect();
  const seconds = secondsForX(event.clientX - rect.left, rect.width);
  const changed = updateMarker(
    dragMarker,
    secondsToSample(seconds),
    { recordHistory: false, markChange: false },
  );
  if (changed && !dragChanged) {
    markDirty();
    dragChanged = true;
  }
});

canvas.addEventListener("pointerup", (event) => {
  if (dragMarker !== null) canvas.releasePointerCapture(event.pointerId);
  if (dragChanged && dragSnapshot) pushBoundaryUndo(dragSnapshot);
  dragMarker = null;
  dragSnapshot = null;
  dragChanged = false;
  refreshBoundaryControls();
  scheduleEvidenceLoad();
});
canvas.addEventListener("pointercancel", () => {
  if (dragChanged && dragSnapshot) pushBoundaryUndo(dragSnapshot);
  dragMarker = null;
  dragSnapshot = null;
  dragChanged = false;
  refreshBoundaryControls();
  scheduleEvidenceLoad();
});

function nudgeSelectedSamples(samples) {
  if (providerBusy || selectedMarker === null) return;
  updateMarker(selectedMarker, markerSamples()[selectedMarker] + samples);
}

function nudgeSelected(milliseconds) {
  const samples = Math.round(project.source.sample_rate * milliseconds / 1000);
  nudgeSelectedSamples(samples);
}

for (const button of document.querySelectorAll("[data-nudge-ms]")) {
  button.addEventListener("click", () => nudgeSelected(Number(button.dataset.nudgeMs)));
}
for (const button of document.querySelectorAll("[data-nudge-samples]")) {
  button.addEventListener("click", () => nudgeSelectedSamples(Number(button.dataset.nudgeSamples)));
}

markerSelect.addEventListener("change", () => {
  const value = markerSelect.value;
  selectMarker(value === "" ? null : Number(value));
});

document.getElementById("snapMarker").addEventListener("click", () => {
  if (providerBusy || selectedMarker === null) return;
  const current = markerSamples()[selectedMarker];
  const candidates = project.analysis.candidates || [];
  if (!candidates.length) {
    setStatus("No detected gaps are available for snapping", "error");
    return;
  }
  const nearest = candidates.reduce((best, candidate) =>
    Math.abs(candidate.cut_sample - current) < Math.abs(best.cut_sample - current)
      ? candidate
      : best
  );
  updateMarker(selectedMarker, nearest.cut_sample);
  setStatus(`Snapped to a ${Math.round(nearest.score * 100)}% gap candidate`, "success");
});

document.getElementById("addMarkerAtPlayhead").addEventListener("click", () => addMarkerAtPlayhead());
document.getElementById("deleteMarker").addEventListener("click", deleteSelectedMarker);
document.getElementById("undoBoundary").addEventListener("click", undoBoundaryEdit);
document.getElementById("redoBoundary").addEventListener("click", redoBoundaryEdit);
document.getElementById("restoreAnalyzerMarker").addEventListener("click", restoreSelectedAnalyzerMarker);
document.getElementById("playBeforeMarker").addEventListener("click", () => auditionSelected("before"));
document.getElementById("playAcrossMarker").addEventListener("click", () => auditionSelected("across"));
document.getElementById("playAfterMarker").addEventListener("click", () => auditionSelected("after"));
document.getElementById("loopMarker").addEventListener("click", () => {
  if (boundaryLoop !== null) stopBoundaryPreview();
  else auditionSelected("across", true);
});
document.getElementById("cancelBoundaryPreview").addEventListener("click", () => stopBoundaryPreview());

canvas.addEventListener("keydown", (event) => {
  if (
    providerBusy
    || selectedMarker === null
    || !["ArrowLeft", "ArrowRight"].includes(event.key)
  ) return;
  event.preventDefault();
  const direction = event.key === "ArrowLeft" ? -1 : 1;
  if (event.ctrlKey || event.metaKey) nudgeSelected(direction * 1000);
  else if (event.shiftKey) nudgeSelected(direction * 100);
  else if (event.altKey) nudgeSelected(direction * 10);
  else nudgeSelectedSamples(direction);
});

document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey) || providerBusy) return;
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target?.isContentEditable) return;
  const key = event.key.toLowerCase();
  if (key === "z" && event.shiftKey) {
    event.preventDefault();
    redoBoundaryEdit();
  } else if (key === "z") {
    event.preventDefault();
    undoBoundaryEdit();
  } else if (key === "y") {
    event.preventDefault();
    redoBoundaryEdit();
  }
});

function confidenceClass(value) {
  if (value < 0.45) return "low";
  if (value < 0.7) return "medium";
  return "high";
}

function renderTrackTable() {
  trackRows.replaceChildren();
  project.tracks.forEach((track, trackIndex) => {
    const row = document.createElement("tr");
    if (track.confidence < 0.45) row.classList.add("low-confidence");

    const number = document.createElement("td");
    number.className = "track-number";
    number.textContent = String(track.number).padStart(2, "0");
    row.append(number);

    const previewCell = document.createElement("td");
    const preview = document.createElement("button");
    preview.className = "preview";
    preview.textContent = "Play";
    preview.disabled = providerBusy;
    preview.setAttribute("aria-label", `Play track ${track.number}: ${track.title}`);
    preview.addEventListener("click", () => {
      stopBoundaryPreview(false);
      currentPreviewEnd = track.end_seconds;
      audio.currentTime = Math.max(0, track.start_seconds);
      auditionStatus.textContent = `Playing track ${track.number} until ${formatTime(track.end_seconds)}.`;
      refreshBoundaryControls();
      audio.play().catch(() => setStatus("Browser could not play this source format", "error"));
    });
    const identify = document.createElement("button");
    identify.className = "identify";
    identify.dataset.providerAction = "true";
    identify.textContent = identifyingTrack === track.number ? "ID…" : "ID";
    identify.disabled = !recognitionReady || providerBusy;
    identify.setAttribute(
      "aria-label",
      `Identify track ${track.number}: ${track.title} from its audio fingerprint`,
    );
    identify.title = recognitionReady
      ? "Identify this track from its audio fingerprint"
      : "AcoustID needs an API key; open Find release for setup status";
    identify.addEventListener("click", () => identifyTrack(track));
    const actions = document.createElement("div");
    actions.className = "track-actions";
    actions.append(preview, identify);
    previewCell.append(actions);
    row.append(previewCell);

    const boundaryCell = document.createElement("td");
    const boundaryActions = document.createElement("div");
    boundaryActions.className = "row-boundary-actions";
    const split = document.createElement("button");
    split.className = "row-boundary-action";
    split.dataset.rowAction = "split";
    split.dataset.trackIndex = String(trackIndex);
    split.textContent = "Split here";
    split.disabled = providerBusy;
    split.setAttribute("aria-label", `Split track ${track.number} at the audio playhead`);
    split.title = "Uses the current audio playhead when it is inside this track";
    split.addEventListener("click", () => addMarkerAtPlayhead(trackIndex));

    const mergePrevious = document.createElement("button");
    mergePrevious.className = "row-boundary-action";
    mergePrevious.dataset.rowAction = "merge-previous";
    mergePrevious.dataset.trackIndex = String(trackIndex);
    mergePrevious.textContent = "Merge previous";
    mergePrevious.disabled = providerBusy
      || trackIndex === 0
      || !canMergeTracks(project.tracks[trackIndex - 1], track);
    mergePrevious.setAttribute("aria-label", `Merge track ${track.number} with the previous track`);
    mergePrevious.addEventListener("click", () => mergeAtBoundary(trackIndex));

    const mergeNext = document.createElement("button");
    mergeNext.className = "row-boundary-action";
    mergeNext.dataset.rowAction = "merge-next";
    mergeNext.dataset.trackIndex = String(trackIndex);
    mergeNext.textContent = "Merge next";
    mergeNext.disabled = providerBusy
      || trackIndex >= project.tracks.length - 1
      || !canMergeTracks(track, project.tracks[trackIndex + 1]);
    mergeNext.setAttribute("aria-label", `Merge track ${track.number} with the next track`);
    mergeNext.addEventListener("click", () => mergeAtBoundary(trackIndex + 1));
    boundaryActions.append(split, mergePrevious, mergeNext);
    boundaryCell.append(boundaryActions);
    row.append(boundaryCell);

    const titleCell = document.createElement("td");
    const title = document.createElement("input");
    title.value = track.title;
    title.disabled = providerBusy;
    title.setAttribute("aria-label", `Track ${track.number} title`);
    title.addEventListener("input", () => {
      track.title = title.value;
      clearBoundaryHistory();
      markDirty();
    });
    titleCell.append(title);
    row.append(titleCell);

    const artistCell = document.createElement("td");
    const artist = document.createElement("input");
    artist.value = track.artist || "";
    artist.disabled = providerBusy;
    artist.setAttribute("aria-label", `Track ${track.number} artist`);
    artist.addEventListener("input", () => {
      track.artist = artist.value;
      clearBoundaryHistory();
      markDirty();
    });
    artistCell.append(artist);
    row.append(artistCell);

    for (const value of [track.start_seconds, track.end_seconds, track.end_seconds - track.start_seconds]) {
      const cell = document.createElement("td");
      cell.className = "time";
      cell.textContent = formatTime(value);
      row.append(cell);
    }

    const confidenceCell = document.createElement("td");
    const confidence = document.createElement("span");
    confidence.className = `confidence ${confidenceClass(track.confidence)}`;
    confidence.textContent = `${Math.round(track.confidence * 100)}%`;
    confidenceCell.append(confidence);
    row.append(confidenceCell);

    trackRows.append(row);
  });
}

audio.addEventListener("timeupdate", () => {
  if (currentPreviewEnd !== null && audio.currentTime >= currentPreviewEnd) {
    if (boundaryLoop !== null) {
      audio.currentTime = boundaryLoop.start;
      if (audio.paused) {
        audio.play().catch(() => {
          stopBoundaryPreview(false);
          setStatus("Browser could not continue the boundary loop", "error");
        });
      }
    } else {
      currentPreviewEnd = null;
      audio.pause();
      auditionStatus.textContent = "Audition finished.";
      refreshBoundaryControls();
    }
  }
  document.getElementById("evidencePlayheadReadout").textContent =
    `${formatTime(audio.currentTime)} · sample ${Math.round(audio.currentTime * (project?.source?.sample_rate || 0)).toLocaleString()}`;
  drawEvidence();
});

function clearPlaybackPreview(message) {
  boundaryLoop = null;
  currentPreviewEnd = null;
  const loopButton = document.getElementById("loopMarker");
  loopButton.setAttribute("aria-pressed", "false");
  loopButton.classList.remove("active");
  loopButton.textContent = "Loop across";
  if (message) auditionStatus.textContent = message;
  refreshBoundaryControls();
}

audio.addEventListener("pause", () => clearPlaybackPreview("Playback paused."));
audio.addEventListener("ended", () => clearPlaybackPreview("Playback reached the end of the source."));
audio.addEventListener("error", () => clearPlaybackPreview("Browser playback is unavailable for this source."));
audio.addEventListener("emptied", () => clearPlaybackPreview("Audio source reloaded."));

for (const [key, input] of Object.entries(metadataInputs)) {
  input.addEventListener("input", () => {
    if (providerBusy) return;
    const previousValue = project.metadata[key] || "";
    project.metadata[key] = input.value;
    if (["album", "album_artist", "year", "genre", "side"].includes(key)) {
      for (const track of project.tracks) track[key] = input.value;
    }
    if (key === "artist") {
      for (const track of project.tracks) {
        if (!track.artist || track.artist === previousValue) track.artist = input.value;
      }
      renderTrackTable();
    }
    clearBoundaryHistory();
    markDirty();
  });
}

function evidenceBounds() {
  if (!project || selectedMarker === null) return null;
  const markers = markerSamples();
  const marker = Number.isSafeInteger(evidenceOverrideFocus)
    ? evidenceOverrideFocus
    : markers[selectedMarker];
  const total = sourceSampleCount();
  if (!Number.isSafeInteger(marker) || total < 1) return null;
  const focus = Math.max(0, Math.min(total - 1, marker));
  const seconds = Number(document.getElementById("evidenceZoom").value);
  const frames = Math.max(1, Math.round(seconds * project.source.sample_rate));
  let start = Math.max(0, focus - Math.floor(frames / 2));
  let end = Math.min(total, start + frames);
  start = Math.max(0, end - frames);
  if (end <= start) return null;
  return { start, end, focus };
}

function scheduleEvidenceLoad(delay = 140) {
  if (evidenceLoadTimer !== null) window.clearTimeout(evidenceLoadTimer);
  if (!project || selectedMarker === null || providerBusy || dragMarker !== null) {
    if (selectedMarker === null) {
      evidencePayload = null;
      evidenceSelection = null;
      evidenceStatus.textContent = "Select any track marker to inspect its local evidence.";
      evidenceStatus.className = "evidence-status";
      document.getElementById("refreshEvidence").disabled = true;
      renderEvidenceDetails();
      drawEvidence();
    }
    return;
  }
  evidenceLoadTimer = window.setTimeout(() => {
    evidenceLoadTimer = null;
    loadEvidence();
  }, delay);
}

function focusEvidenceAtSample(sample) {
  if (!project || !Number.isSafeInteger(sample)) return;
  evidenceOverrideFocus = Math.max(0, Math.min(sourceSampleCount() - 1, sample));
  scheduleEvidenceLoad(0);
}

async function loadEvidence() {
  const bounds = evidenceBounds();
  if (!bounds || projectConflict) return;
  const requestedRevision = project.revision;
  const requestedProjectSha256 = project.project_sha256;
  if (evidenceAbortController) evidenceAbortController.abort();
  evidenceAbortController = new AbortController();
  const requestId = ++evidenceRequestId;
  evidenceStatus.textContent = "Decoding one exact source window and measuring both views…";
  evidenceStatus.className = "evidence-status busy";
  document.getElementById("refreshEvidence").disabled = true;
  try {
    const payload = await postJson(
      "/api/evidence",
      {
        start_sample: bounds.start,
        end_sample: bounds.end,
        focus_sample: bounds.focus,
      },
      { signal: evidenceAbortController.signal },
    );
    if (requestId !== evidenceRequestId) return;
    if (
      project.revision !== requestedRevision
      || project.project_sha256 !== requestedProjectSha256
    ) {
      scheduleEvidenceLoad(0);
      return;
    }
    if (
      payload.project_revision !== requestedRevision
      || payload.project_sha256 !== requestedProjectSha256
    ) {
      const conflict = new Error(
        "The project changed while this evidence window was being decoded. Reload before continuing."
      );
      conflict.status = 409;
      handleProjectConflict(conflict);
      return;
    }
    project.source_receipt = payload.source_receipt;
    evidencePayload = payload;
    evidenceSelection = null;
    evidenceStatus.textContent = "Evidence is aligned to the selected marker on one exact sample grid.";
    evidenceStatus.className = "evidence-status";
    renderEvidenceDetails();
    resizeEvidenceCanvas();
    updateRestorationControls();
  } catch (error) {
    if (error.name === "AbortError" || requestId !== evidenceRequestId) return;
    evidencePayload = null;
    evidenceStatus.textContent = error.message;
    evidenceStatus.className = "evidence-status error";
    renderEvidenceDetails();
    drawEvidence();
    handleProjectConflict(error);
  } finally {
    if (requestId === evidenceRequestId) {
      evidenceAbortController = null;
      document.getElementById("refreshEvidence").disabled = providerBusy || selectedMarker === null;
    }
  }
}

function resizeEvidenceCanvas() {
  const ratio = window.devicePixelRatio || 1;
  const rect = evidenceCanvas.getBoundingClientRect();
  evidenceCanvas.width = Math.max(1, Math.round(rect.width * ratio));
  evidenceCanvas.height = Math.max(1, Math.round(rect.height * ratio));
  evidenceContext.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawEvidence();
}

function evidenceXForSample(sample, width) {
  const selection = evidencePayload?.selection;
  if (!selection) return 0;
  const span = selection.end_sample_exclusive - selection.start_sample;
  return span > 0 ? (sample - selection.start_sample) / span * width : 0;
}

function evidenceSampleForX(clientX) {
  const selection = evidencePayload?.selection;
  if (!selection) return null;
  const rect = evidenceCanvas.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, clientX - rect.left));
  const span = selection.end_sample_exclusive - selection.start_sample;
  const offset = Math.min(span - 1, Math.max(0, Math.floor(x / rect.width * span)));
  return selection.start_sample + offset;
}

function spectrogramColor(db) {
  const normalized = Math.max(0, Math.min(1, (Number(db) + 105) / 105));
  const stops = [
    [0.00, [5, 6, 8]],
    [0.28, [31, 16, 50]],
    [0.56, [103, 39, 85]],
    [0.78, [226, 99, 57]],
    [1.00, [236, 244, 164]],
  ];
  let left = stops[0];
  let right = stops.at(-1);
  for (let index = 1; index < stops.length; index += 1) {
    if (normalized <= stops[index][0]) {
      left = stops[index - 1];
      right = stops[index];
      break;
    }
  }
  const amount = (normalized - left[0]) / Math.max(1e-9, right[0] - left[0]);
  const rgb = left[1].map((value, index) => Math.round(value + (right[1][index] - value) * amount));
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

function drawEvidence() {
  const width = evidenceCanvas.clientWidth;
  const height = evidenceCanvas.clientHeight;
  if (!width || !height) return;
  evidenceContext.clearRect(0, 0, width, height);
  evidenceContext.fillStyle = "#090908";
  evidenceContext.fillRect(0, 0, width, height);
  if (!evidencePayload) {
    evidenceContext.fillStyle = "#aaa79f";
    evidenceContext.font = "13px system-ui, sans-serif";
    evidenceContext.textAlign = "center";
    evidenceContext.fillText("Select a marker to load synchronized local evidence", width / 2, height / 2);
    return;
  }

  const waveformTop = 24;
  const waveformBottom = Math.floor(height * 0.43);
  const spectrumTop = waveformBottom + 30;
  const spectrumBottom = height - 24;
  const spectrumHeight = Math.max(1, spectrumBottom - spectrumTop);
  const waveform = evidencePayload.waveform;
  const channels = waveform.channels || [];
  const channelHeight = Math.max(1, (waveformBottom - waveformTop) / Math.max(1, channels.length));

  evidenceContext.strokeStyle = "rgba(255,255,255,0.08)";
  evidenceContext.lineWidth = 1;
  for (let channel = 0; channel < channels.length; channel += 1) {
    const middle = waveformTop + channelHeight * (channel + 0.5);
    evidenceContext.beginPath();
    evidenceContext.moveTo(0, middle);
    evidenceContext.lineTo(width, middle);
    evidenceContext.stroke();
    const minimum = channels[channel].minimum;
    const maximum = channels[channel].maximum;
    evidenceContext.strokeStyle = "rgba(245, 241, 232, 0.82)";
    evidenceContext.beginPath();
    for (let index = 0; index < minimum.length; index += 1) {
      const x = (index + 0.5) / minimum.length * width;
      const amplitude = channelHeight * 0.42;
      evidenceContext.moveTo(x, middle - Number(maximum[index]) * amplitude);
      evidenceContext.lineTo(x, middle - Number(minimum[index]) * amplitude);
    }
    evidenceContext.stroke();
    evidenceContext.fillStyle = "#aaa79f";
    evidenceContext.font = "10px system-ui, sans-serif";
    evidenceContext.textAlign = "left";
    evidenceContext.fillText(channels.length === 2 ? (channel === 0 ? "L" : "R") : `CH ${channel + 1}`, 7, middle - channelHeight * 0.34);
  }

  const spectrum = evidencePayload.spectrogram;
  const rows = spectrum.dbfs || [];
  const columns = rows[0]?.length || 0;
  if (rows.length && columns) {
    const cellWidth = width / columns;
    const cellHeight = spectrumHeight / rows.length;
    for (let row = 0; row < rows.length; row += 1) {
      const y = spectrumBottom - (row + 1) * cellHeight;
      for (let column = 0; column < columns; column += 1) {
        evidenceContext.fillStyle = spectrogramColor(rows[row][column]);
        evidenceContext.fillRect(
          Math.floor(column * cellWidth),
          Math.floor(y),
          Math.ceil(cellWidth + 0.5),
          Math.ceil(cellHeight + 0.5),
        );
      }
    }
  }

  evidenceContext.fillStyle = "#aaa79f";
  evidenceContext.font = "10px system-ui, sans-serif";
  evidenceContext.textAlign = "left";
  evidenceContext.fillText("WAVEFORM · EXACT PCM MIN/MAX", 7, 14);
  evidenceContext.fillText("SPECTROGRAM · CHANNEL-AVERAGED POWER", 7, spectrumTop - 10);

  for (const event of evidencePayload.transients || []) {
    const x = evidenceXForSample(event.sample, width);
    evidenceContext.strokeStyle = event.protected_by_default
      ? "rgba(216,255,79,0.82)"
      : "rgba(120,169,255,0.66)";
    evidenceContext.lineWidth = event.protected_by_default ? 2 : 1;
    evidenceContext.beginPath();
    evidenceContext.moveTo(x, waveformTop);
    evidenceContext.lineTo(x, spectrumBottom);
    evidenceContext.stroke();
  }

  if (evidenceSelection) {
    const x1 = evidenceXForSample(evidenceSelection.start, width);
    const x2 = evidenceXForSample(evidenceSelection.end, width);
    evidenceContext.fillStyle = "rgba(216, 255, 79, 0.14)";
    evidenceContext.fillRect(Math.min(x1, x2), waveformTop, Math.max(1, Math.abs(x2 - x1)), spectrumBottom - waveformTop);
    evidenceContext.strokeStyle = "rgba(216,255,79,0.85)";
    evidenceContext.lineWidth = 1;
    for (const x of [x1, x2]) {
      evidenceContext.beginPath();
      evidenceContext.moveTo(x, waveformTop);
      evidenceContext.lineTo(x, spectrumBottom);
      evidenceContext.stroke();
    }
  }

  const focusX = evidenceXForSample(evidencePayload.selection.focus_sample, width);
  evidenceContext.strokeStyle = "#d8ff4f";
  evidenceContext.lineWidth = 2;
  evidenceContext.beginPath();
  evidenceContext.moveTo(focusX, 0);
  evidenceContext.lineTo(focusX, spectrumBottom);
  evidenceContext.stroke();

  if (project && Number.isFinite(audio.currentTime)) {
    const playheadSample = Math.round(audio.currentTime * project.source.sample_rate);
    if (
      playheadSample >= evidencePayload.selection.start_sample
      && playheadSample < evidencePayload.selection.end_sample_exclusive
    ) {
      const playheadX = evidenceXForSample(playheadSample, width);
      evidenceContext.strokeStyle = "rgba(255,255,255,0.92)";
      evidenceContext.lineWidth = 1;
      evidenceContext.beginPath();
      evidenceContext.moveTo(playheadX, 0);
      evidenceContext.lineTo(playheadX, spectrumBottom);
      evidenceContext.stroke();
    }
  }
}

function addEvidenceMetric(container, label, value) {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  row.append(term, detail);
  container.append(row);
}

function renderEvidenceDetails() {
  const metrics = document.getElementById("evidenceMetrics");
  const observations = document.getElementById("evidenceObservations");
  const transients = document.getElementById("transientEvidence");
  metrics.replaceChildren();
  observations.replaceChildren();
  transients.replaceChildren();
  document.getElementById("playEvidenceSelection").disabled = !evidenceSelection || providerBusy;
  document.getElementById("clearEvidenceSelection").disabled = !evidenceSelection || providerBusy;
  if (!evidencePayload) {
    document.getElementById("evidenceViewReadout").textContent = "Select a marker";
    document.getElementById("evidenceFocusReadout").textContent = "—";
    document.getElementById("evidenceSelectionReadout").textContent = "None";
    return;
  }
  const selection = evidencePayload.selection;
  const focus = evidencePayload.focus_evidence;
  document.getElementById("evidenceViewReadout").textContent =
    `${formatTime(selection.start_seconds)} — ${formatTime(selection.end_seconds)}`;
  document.getElementById("evidenceFocusReadout").textContent =
    `${formatTime(selection.focus_seconds)} · sample ${selection.focus_sample.toLocaleString()}`;
  document.getElementById("evidenceSelectionReadout").textContent = evidenceSelection
    ? `${(evidenceSelection.end - evidenceSelection.start).toLocaleString()} samples`
    : "None";
  addEvidenceMetric(metrics, "Energy before", `${focus.left_rms_dbfs.toFixed(1)} dBFS`);
  addEvidenceMetric(metrics, "Energy after", `${focus.right_rms_dbfs.toFixed(1)} dBFS`);
  addEvidenceMetric(metrics, "Energy change", `${focus.right_minus_left_db >= 0 ? "+" : ""}${focus.right_minus_left_db.toFixed(1)} dB`);
  addEvidenceMetric(metrics, "Spectral continuity", `${Math.round(focus.spectral_cosine_similarity * 100)}%`);
  addEvidenceMetric(metrics, "Centroid before / after", `${Math.round(focus.left_spectral_centroid_hz)} / ${Math.round(focus.right_spectral_centroid_hz)} Hz`);
  for (const text of focus.observations || []) {
    const item = document.createElement("li");
    item.textContent = text;
    observations.append(item);
  }
  const nearest = [...(evidencePayload.transients || [])]
    .sort((left, right) =>
      Math.abs(left.sample - selection.focus_sample) - Math.abs(right.sample - selection.focus_sample)
    )
    .slice(0, 4);
  for (const event of nearest) {
    const item = document.createElement("div");
    item.className = `transient-chip${event.protected_by_default ? " protected" : ""}`;
    const label = document.createElement("span");
    label.textContent = event.morphology_hint.replaceAll("_", " ");
    const timing = document.createElement("span");
    timing.textContent = `${formatTime(event.time_seconds)} · ${Math.round(event.hint_confidence * 100)}% hint`;
    item.append(label, timing);
    transients.append(item);
  }
}

evidenceCanvas.addEventListener("pointerdown", (event) => {
  if (!evidencePayload || providerBusy) return;
  const sample = evidenceSampleForX(event.clientX);
  if (sample === null) return;
  evidenceDragAnchor = sample;
  evidenceSelection = { start: sample, end: Math.min(sample + 1, evidencePayload.selection.end_sample_exclusive) };
  evidenceCanvas.setPointerCapture(event.pointerId);
  renderEvidenceDetails();
  drawEvidence();
});

evidenceCanvas.addEventListener("pointermove", (event) => {
  if (evidenceDragAnchor === null) return;
  const sample = evidenceSampleForX(event.clientX);
  if (sample === null) return;
  evidenceSelection = {
    start: Math.min(evidenceDragAnchor, sample),
    end: Math.min(
      evidencePayload.selection.end_sample_exclusive,
      Math.max(evidenceDragAnchor, sample) + 1,
    ),
  };
  renderEvidenceDetails();
  drawEvidence();
});

evidenceCanvas.addEventListener("pointerup", (event) => {
  if (evidenceDragAnchor !== null) evidenceCanvas.releasePointerCapture(event.pointerId);
  evidenceDragAnchor = null;
  renderEvidenceDetails();
  drawEvidence();
});
evidenceCanvas.addEventListener("pointercancel", () => { evidenceDragAnchor = null; });

document.getElementById("evidenceZoom").addEventListener("change", () => scheduleEvidenceLoad(0));
document.getElementById("refreshEvidence").addEventListener("click", () => scheduleEvidenceLoad(0));
document.getElementById("clearEvidenceSelection").addEventListener("click", () => {
  evidenceSelection = null;
  renderEvidenceDetails();
  drawEvidence();
});
document.getElementById("playEvidenceSelection").addEventListener("click", () => {
  if (!evidenceSelection || providerBusy) return;
  stopBoundaryPreview(false);
  audio.currentTime = sampleToSeconds(evidenceSelection.start);
  currentPreviewEnd = sampleToSeconds(evidenceSelection.end);
  auditionStatus.textContent = `Playing exact selection of ${(evidenceSelection.end - evidenceSelection.start).toLocaleString()} source samples.`;
  refreshBoundaryControls();
  audio.play().catch(() => setStatus("Browser could not play this source format", "error"));
});

function speedValues() {
  const capture = Number(speedInputs.capture.value);
  const intended = Number(speedInputs.intended.value);
  const fine = Number(speedInputs.fine.value);
  if (
    !Number.isFinite(capture) || !Number.isFinite(intended) || !Number.isFinite(fine)
    || capture < 10 || capture > 100 || intended < 10 || intended > 100
    || fine < 0.5 || fine > 2
  ) return null;
  const factor = capture / intended * fine;
  if (!Number.isFinite(factor) || factor < 0.5 || factor > 2) return null;
  return { capture, intended, fine, factor };
}

function updateSpeedView({ persist = false } = {}) {
  const values = speedValues();
  const summary = document.getElementById("speedSummary");
  const corrected = document.getElementById("listenCorrected");
  if (!values) {
    summary.textContent = "INVALID SPEED SETTINGS";
    summary.style.color = "var(--danger)";
    corrected.disabled = true;
    document.getElementById("speedDuration").textContent = "—";
    document.getElementById("speedPitch").textContent = "—";
    document.getElementById("speedRealizable").textContent = "—";
    document.getElementById("exportSpeedNote").textContent = "Correct the speed settings before export.";
    return null;
  }
  summary.style.color = "";
  summary.textContent = `SOURCE FACTOR ${values.factor.toFixed(6)}×`;
  corrected.disabled = providerBusy;
  const delta = (values.factor - 1) * 100;
  const correctedDuration = Number(project?.source?.duration_seconds || 0) * values.factor;
  const semitones = -12 * Math.log2(values.factor);
  const sourceRate = Number(project?.source?.sample_rate || 0);
  const integerRate = Math.max(1, Math.floor(sourceRate / values.factor + 0.5));
  const effectiveFactor = sourceRate / integerRate;
  document.getElementById("speedDuration").textContent =
    `${formatTime(correctedDuration)} · ${delta >= 0 ? "+" : ""}${delta.toFixed(3)}%`;
  document.getElementById("speedPitch").textContent =
    `${semitones >= 0 ? "+" : ""}${semitones.toFixed(3)} semitones`;
  document.getElementById("speedRealizable").textContent =
    `${integerRate.toLocaleString()} Hz · ${effectiveFactor.toFixed(9)}×`;
  document.getElementById("correctedListeningLabel").textContent = Math.abs(delta) < 0.00005
    ? "No correction required"
    : `${Math.abs(delta).toFixed(3)}% ${delta > 0 ? "fast source → slowed" : "slow source → accelerated"}`;
  document.getElementById("exportSpeedNote").textContent = Math.abs(values.factor - 1) < 1e-9
    ? "Archival timing will be exported."
    : `Corrected derivatives will use requested source factor ${values.factor.toFixed(9)}×; the exact effective factor is recorded after integer-rate mapping.`;
  if (persist && project) {
    const next = {
      speed_capture_rpm: values.capture.toFixed(9),
      speed_intended_rpm: values.intended.toFixed(9),
      speed_fine_factor: values.fine.toFixed(9),
    };
    if (Object.entries(next).some(([key, value]) => project.metadata[key] !== value)) {
      Object.assign(project.metadata, next);
      markDirty();
    }
  }
  if (listeningOutput === "corrected") {
    audio.preservesPitch = false;
    audio.mozPreservesPitch = false;
    audio.webkitPreservesPitch = false;
    audio.playbackRate = 1 / values.factor;
  }
  return values;
}

function setListeningOutput(mode) {
  const values = updateSpeedView();
  if (mode === "corrected" && !values) return;
  listeningOutput = mode;
  const archival = document.getElementById("listenArchival");
  const corrected = document.getElementById("listenCorrected");
  archival.classList.toggle("active", mode === "archival");
  corrected.classList.toggle("active", mode === "corrected");
  archival.setAttribute("aria-pressed", String(mode === "archival"));
  corrected.setAttribute("aria-pressed", String(mode === "corrected"));
  if (mode === "archival") {
    audio.playbackRate = 1;
    audio.preservesPitch = true;
    audio.mozPreservesPitch = true;
    audio.webkitPreservesPitch = true;
    setStatus("Auditioning the untouched archival timing", "success");
  } else {
    audio.preservesPitch = false;
    audio.mozPreservesPitch = false;
    audio.webkitPreservesPitch = false;
    audio.playbackRate = 1 / values.factor;
    setStatus(`Auditioning pitch + tempo correction at ${values.factor.toFixed(6)}×`, "success");
  }
}

function initializeSpeedControls() {
  const metadata = project.metadata || {};
  const fallbackFactor = Number(metadata.speed_source_factor);
  const capture = Number(metadata.speed_capture_rpm);
  const intended = Number(metadata.speed_intended_rpm);
  const fine = Number(metadata.speed_fine_factor);
  speedInputs.capture.value = Number.isFinite(capture) && capture >= 10 && capture <= 100
    ? capture.toFixed(6)
    : "33.333333";
  speedInputs.intended.value = Number.isFinite(intended) && intended >= 10 && intended <= 100
    ? intended.toFixed(6)
    : "33.333333";
  speedInputs.fine.value = Number.isFinite(fine) && fine >= 0.5 && fine <= 2
    ? fine.toFixed(6)
    : (Number.isFinite(fallbackFactor) && fallbackFactor >= 0.5 && fallbackFactor <= 2
      ? fallbackFactor.toFixed(6)
      : "1.000000");
  listeningOutput = "archival";
  setListeningOutput("archival");
  updateSpeedView();
}

for (const input of Object.values(speedInputs)) {
  input.addEventListener("change", () => updateSpeedView({ persist: true }));
}
for (const button of document.querySelectorAll("[data-intended-rpm]")) {
  button.addEventListener("click", () => {
    speedInputs.intended.value = Number(button.dataset.intendedRpm).toFixed(6);
    updateSpeedView({ persist: true });
  });
}
document.getElementById("listenArchival").addEventListener("click", () => setListeningOutput("archival"));
document.getElementById("listenCorrected").addEventListener("click", () => setListeningOutput("corrected"));

function projectIdentityReceipt() {
  return {
    expected_revision: project.revision,
    expected_project_sha256: project.project_sha256,
    expected_source_receipt: project.source_receipt.receipt,
  };
}

function requireCurrentResponseIdentity(payload, operationLabel) {
  if (
    payload?.project_revision !== project?.revision
    || payload?.project_sha256 !== project?.project_sha256
    || payload?.source_receipt?.receipt !== project?.source_receipt?.receipt
  ) {
    const conflict = new Error(
      `${operationLabel} returned evidence for a different project or source state. Reload before continuing.`
    );
    conflict.status = 409;
    throw conflict;
  }
}

function setRestorationStatus(message, kind = "") {
  const element = document.getElementById("restorationStatus");
  element.textContent = message;
  element.className = `status ${kind}`.trim();
}

function clearRestorationPreview() {
  restorationPreview = null;
  restorationSelectedCandidate = null;
  document.getElementById("restorationPreviewTitle").textContent = "Select a candidate";
  document.getElementById("restorationPreviewProof").textContent =
    "Original and Proposed remain at identical gain. Removed Signal is a declared-gain residue.";
  document.getElementById("restorationPreviewMetrics").replaceChildren();
  for (const player of Object.values(restorationAudio)) {
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
}

function clearRestorationWorkflow() {
  restorationScan = null;
  restorationRecipe = null;
  restorationDecisions.clear();
  restorationPreviewed.clear();
  clearRestorationPreview();
  renderRestorationCandidates();
}

async function beginRestorationArtifactOperation() {
  const savedDirtyEdits = dirty;
  const operation = await beginProviderOperation();
  if (!operation) return null;
  if (savedDirtyEdits) {
    clearRestorationWorkflow();
    endProviderOperation(operation.id);
    setRestorationStatus(
      "Project edits were saved. Run a new scan so every restoration proof binds to the new project state.",
      "busy",
    );
    return null;
  }
  return operation;
}

function handleRestorationError(error) {
  const message = typeof error?.message === "string" ? error.message.toLowerCase() : "";
  if (
    error?.status === 409
    && message.includes("registered")
    && message.includes("older project or source state")
  ) {
    clearRestorationWorkflow();
    setRestorationStatus(
      "This restoration proof belongs to an older project state. Run a new verified scan.",
      "error",
    );
    return true;
  }
  return handleProjectConflict(error);
}

function currentRestorationCoverage() {
  const coverage = restorationScan?.coverage;
  return coverage && typeof coverage === "object" ? coverage : null;
}

function restorationReviewNeedsNoDerivative() {
  const coverage = currentRestorationCoverage();
  return Boolean(
    restorationScan
    && restorationScan.candidates?.length === 0
    && coverage?.restoration_status === "complete"
    && !coverage?.candidate_scan_truncated
  );
}

function updateRestorationControls() {
  const candidates = restorationScan?.candidates || [];
  const pending = candidates.filter((candidate) => {
    const decision = restorationDecisions.get(candidate.id);
    return !decision || (
      decision.decision === "protected" && !decision.classification
    );
  }).length;
  const approved = candidates.filter(
    (candidate) => restorationDecisions.get(candidate.id)?.decision === "approved"
  ).length;
  const rejected = candidates.filter(
    (candidate) => restorationDecisions.get(candidate.id)?.decision === "rejected"
  ).length;
  const protectedCount = candidates.filter(
    (candidate) => restorationDecisions.get(candidate.id)?.decision === "protected"
      && restorationDecisions.get(candidate.id)?.classification
  ).length;
  const coverage = currentRestorationCoverage();
  const completeCoverage = coverage?.restoration_status === "complete";
  const noDerivative = restorationReviewNeedsNoDerivative();
  document.getElementById("restorationDecisionSummary").textContent = candidates.length
    ? `${approved} approved · ${rejected} kept original · ${protectedCount} protected · ${pending} pending`
    : noDerivative
      ? "Review complete · No repairable events were found · No restored derivative is necessary."
      : "Every retained candidate needs an explicit decision.";
  document.getElementById("restorationPreviewPanel").classList.toggle(
    "hidden", candidates.length === 0
  );
  document.getElementById("restorationLayout").classList.toggle(
    "single-column", candidates.length === 0
  );
  document.getElementById("saveRestorationRecipe").classList.toggle(
    "hidden", noDerivative
  );
  document.getElementById("renderRestoredSide").classList.toggle(
    "hidden", noDerivative
  );
  document.getElementById("startRestorationScan").disabled = providerBusy || !project;
  document.getElementById("useEvidenceWindow").disabled = providerBusy || !evidencePayload;
  document.getElementById("saveRestorationRecipe").disabled =
    providerBusy || !restorationScan || pending !== 0 || candidates.length === 0;
  document.getElementById("renderRestoredSide").disabled =
    providerBusy || !restorationRecipe || approved === 0 || !completeCoverage;
  document.getElementById("renderRestoredSide").title = completeCoverage
    ? "Render the full reviewed music range"
    : "A full untruncated scan of the music range is required before using the Restored label";
}

function renderRestorationSummary() {
  const container = document.getElementById("restorationSummary");
  container.replaceChildren();
  if (!restorationScan) return;
  const summary = restorationScan.summary || {};
  const coverage = currentRestorationCoverage();
  const coveragePercent = Number(coverage?.scanned_music_percent);
  for (const text of [
    ...(Number.isFinite(coveragePercent)
      ? [`${coveragePercent.toFixed(1)}% music coverage`]
      : []),
    ...(coverage?.restoration_status
      ? [`${coverage.restoration_status} review`]
      : []),
    ...(coverage?.candidate_scan_truncated
      ? [`${coverage.unretained_detections || 0} detections unretained`]
      : []),
    `${summary.retained ?? restorationScan.candidates.length} retained`,
    `${summary.repairable ?? 0} repairable`,
    `${summary.impulse ?? 0} impulses`,
    `${summary.clipped ?? 0} clipped runs`,
  ]) {
    const item = document.createElement("span");
    item.textContent = text;
    container.append(item);
  }
}

function setRestorationDecision(candidate, decision) {
  restorationRecipe = null;
  if (decision === "approved" && !restorationPreviewed.has(candidate.id)) {
    setRestorationStatus("Create and inspect Original / Proposed / Removed Signal before approval", "error");
    return;
  }
  restorationDecisions.set(candidate.id, {
    candidate_id: candidate.id,
    decision,
    ...(decision === "protected" ? { classification: "" } : {}),
  });
  renderRestorationCandidates();
}

function renderRestorationCandidates() {
  renderRestorationSummary();
  const container = document.getElementById("restorationCandidates");
  container.replaceChildren();
  const candidates = restorationScan?.candidates || [];
  if (!candidates.length) {
    const empty = document.createElement("p");
    empty.className = "quiet";
    empty.textContent = restorationReviewNeedsNoDerivative()
      ? "Review complete. No repairable events were found. No restored derivative is necessary."
      : restorationScan
        ? "The selected range produced no retained candidates."
        : "Run a scan to inspect candidate morphology and audition proposals.";
    container.append(empty);
    updateRestorationControls();
    return;
  }
  for (const candidate of candidates) {
    const card = document.createElement("article");
    card.className = `restoration-candidate${restorationSelectedCandidate === candidate.id ? " selected" : ""}`;
    const identity = document.createElement("div");
    identity.className = "candidate-identity";
    const title = document.createElement("strong");
    title.textContent = `${candidate.type === "clipped" ? "Clipped run" : "Impulse"} · ${Math.round(candidate.confidence * 100)}% detector confidence`;
    const detail = document.createElement("span");
    detail.textContent = `${formatTime(candidate.peak_frame / project.source.sample_rate)} · samples ${candidate.start_frame.toLocaleString()}–${candidate.end_frame_exclusive.toLocaleString()} · ch ${candidate.channels.map((value) => value + 1).join(",")}`;
    identity.append(title, detail);

    const controls = document.createElement("div");
    const decisions = document.createElement("div");
    decisions.className = "candidate-decisions";
    const current = restorationDecisions.get(candidate.id);
    const choices = [
      ["rejected", "Keep original"],
      ["approved", "Apply proposed"],
      ["protected", "Protect structure"],
    ];
    for (const [value, label] of choices) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.className = `${value === "protected" ? "protected " : ""}${current?.decision === value ? "active" : ""}`.trim();
      button.disabled = providerBusy || (value === "approved" && (!candidate.repairable || !restorationPreviewed.has(candidate.id)));
      button.title = value === "approved" && !restorationPreviewed.has(candidate.id)
        ? "Create the lossless audition preview first"
        : "";
      button.addEventListener("click", () => setRestorationDecision(candidate, value));
      decisions.append(button);
    }
    controls.append(decisions);
    const protection = document.createElement("label");
    protection.className = `candidate-protection${current?.decision === "protected" ? " visible" : ""}`;
    const select = document.createElement("select");
    const protectionOptions = [
      ["", "Choose protected event…"],
      ["needle-drop", "Needle drop"],
      ["needle-pickup", "Needle pickup"],
      ["handling-event", "Handling event"],
      ["other-structural-event", "Other structural event"],
    ];
    for (const [value, label] of protectionOptions) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    }
    select.value = current?.classification || "";
    select.disabled = providerBusy;
    select.addEventListener("change", () => {
      restorationRecipe = null;
      restorationDecisions.set(candidate.id, {
        candidate_id: candidate.id,
        decision: "protected",
        classification: select.value,
      });
      updateRestorationControls();
    });
    protection.append(select);
    controls.append(protection);

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "candidate-preview-button";
    preview.textContent = restorationPreviewed.has(candidate.id) ? "Rebuild preview" : "Preview + inspect";
    preview.disabled = providerBusy || !candidate.repairable;
    preview.addEventListener("click", () => previewRestorationCandidate(candidate));
    card.append(identity, controls, preview);
    container.append(card);
  }
  updateRestorationControls();
}

function showRestorationPreview(preview) {
  restorationPreview = preview;
  const candidate = preview?.candidates?.[0];
  document.getElementById("restorationPreviewTitle").textContent = candidate
    ? `${candidate.type} at ${formatTime(candidate.peak_frame / project.source.sample_rate)}`
    : "Lossless candidate audition";
  const audition = preview?.audition || {};
  document.getElementById("restorationPreviewProof").textContent =
    `Original gain ${Number(audition.before_linear_gain ?? 1).toFixed(1)}× · Proposed gain ${Number(audition.proposed_linear_gain ?? 1).toFixed(1)}× · Removed Signal ${Number(audition.removed_linear_gain ?? 1).toFixed(1)}× declared residue gain.`;
  for (const [role, player] of Object.entries(restorationAudio)) {
    const entry = preview?.audio?.[role];
    if (entry?.url) {
      player.src = entry.url;
      player.volume = 1;
      player.playbackRate = 1;
      player.load();
    } else {
      player.removeAttribute("src");
      player.load();
    }
  }
  const metrics = document.getElementById("restorationPreviewMetrics");
  metrics.replaceChildren();
  const proofItems = [
    `${preview?.metrics?.changed_scalar_samples ?? "?"} changed scalar samples`,
    preview?.proof?.outside_approved_windows_and_channels_identical
      ? "Outside-window PCM identical"
      : "Check outside-window proof",
    preview?.proof?.lossless_preview_round_trip
      ? "Lossless round trip verified"
      : "Round-trip proof unavailable",
    preview?.proof?.source_unchanged ? "Source unchanged" : "Check source proof",
  ];
  for (const text of proofItems) {
    const item = document.createElement("span");
    item.textContent = text;
    metrics.append(item);
  }
}

async function loadRestorationStatus() {
  if (!project) return;
  try {
    const payload = await request("/api/restoration/status");
    if (
      payload.project_revision !== project.revision
      || payload.project_sha256 !== project.project_sha256
    ) return;
    project.source_receipt = payload.source_receipt;
    restorationScan = payload.current_scan?.stale ? null : payload.current_scan;
    restorationRecipe = payload.current_recipe?.stale ? null : payload.current_recipe;
    restorationPreview = payload.current_preview?.stale ? null : payload.current_preview;
    restorationDecisions.clear();
    restorationPreviewed.clear();
    restorationSelectedCandidate = null;
    for (const decision of restorationRecipe?.decisions || []) {
      restorationDecisions.set(decision.candidate_id, { ...decision });
    }
    if (restorationPreview) {
      for (const candidate of restorationPreview.candidates || []) {
        restorationPreviewed.add(candidate.id);
      }
      showRestorationPreview(restorationPreview);
    } else {
      clearRestorationPreview();
    }
    renderRestorationCandidates();
    if (payload.current_render && !payload.current_render.stale) {
      const count = payload.current_render.repairs?.length || 0;
      setRestorationStatus(`Full restored-side proof available · ${count} approved repair${count === 1 ? "" : "s"}`, "success");
    } else if (restorationReviewNeedsNoDerivative()) {
      setRestorationStatus(
        "Review complete · No repairable events were found · No restored derivative is necessary",
        "success",
      );
    } else if (restorationScan) {
      setRestorationStatus(`Scan ready · ${restorationScan.candidates.length} candidates`);
    }
  } catch (error) {
    if (!handleProjectConflict(error)) setRestorationStatus(error.message, "error");
  }
}

document.getElementById("useEvidenceWindow").addEventListener("click", () => {
  if (!evidencePayload) return;
  document.getElementById("restorationScanStart").value = evidencePayload.selection.start_seconds.toFixed(6);
  document.getElementById("restorationScanEnd").value = evidencePayload.selection.end_seconds.toFixed(6);
  setRestorationStatus("Microscope window copied into the scan range", "success");
});

document.getElementById("startRestorationScan").addEventListener("click", async () => {
  const operation = await beginProviderOperation();
  if (!operation) return;
  const startText = document.getElementById("restorationScanStart").value.trim();
  const endText = document.getElementById("restorationScanEnd").value.trim();
  const maximum = Number(document.getElementById("restorationMaxCandidates").value);
  if (!Number.isInteger(maximum)) {
    endProviderOperation(operation.id);
    setRestorationStatus("Maximum candidates must be a whole number", "error");
    return;
  }
  setRestorationStatus("Scanning exact source PCM; no audio is being changed…", "busy");
  try {
    const body = { ...projectIdentityReceipt(), max_candidates: maximum };
    if (startText) body.start_seconds = Number(startText);
    if (endText) body.end_seconds = Number(endText);
    const payload = await postJson("/api/restoration/scan", body);
    if (operation.id !== providerOperationId) return;
    project.source_receipt = payload.source_receipt;
    restorationScan = payload.scan;
    restorationRecipe = null;
    restorationDecisions.clear();
    restorationPreviewed.clear();
    clearRestorationPreview();
    renderRestorationCandidates();
    const coverage = currentRestorationCoverage();
    const coverageText = Number.isFinite(Number(coverage?.scanned_music_percent))
      ? ` · ${Number(coverage.scanned_music_percent).toFixed(1)}% music coverage · ${coverage.restoration_status}`
      : "";
    if (restorationReviewNeedsNoDerivative()) {
      setRestorationStatus(
        "Review complete · No repairable events were found · No restored derivative is necessary",
        "success",
      );
    } else {
      setRestorationStatus(
        `Scan complete · ${restorationScan.candidates.length} retained candidates${coverageText}`,
        coverage?.restoration_status === "complete" ? "success" : "busy",
      );
    }
  } catch (error) {
    if (!handleProjectConflict(error)) setRestorationStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
});

async function previewRestorationCandidate(candidate) {
  restorationSelectedCandidate = candidate.id;
  focusEvidenceAtSample(candidate.peak_frame);
  const operation = await beginRestorationArtifactOperation();
  if (!operation) return;
  setRestorationStatus("Building lossless Original / Proposed / Removed Signal clips…", "busy");
  try {
    const payload = await postJson("/api/restoration/preview", {
      ...projectIdentityReceipt(),
      scan_token: restorationScan.token,
      candidate_ids: [candidate.id],
      context_seconds: 2,
    });
    if (operation.id !== providerOperationId) return;
    project.source_receipt = payload.source_receipt;
    restorationPreviewed.add(candidate.id);
    showRestorationPreview(payload.preview);
    renderRestorationCandidates();
    setRestorationStatus("Preview ready at matched Original / Proposed gain", "success");
  } catch (error) {
    if (!handleRestorationError(error)) setRestorationStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
}

document.getElementById("saveRestorationRecipe").addEventListener("click", async () => {
  if (!restorationScan) return;
  const operation = await beginRestorationArtifactOperation();
  if (!operation) return;
  setRestorationStatus("Saving the complete reviewed decision recipe…", "busy");
  try {
    const decisions = restorationScan.candidates.map((candidate) => ({
      ...restorationDecisions.get(candidate.id),
    }));
    const payload = await postJson("/api/restoration/recipe", {
      ...projectIdentityReceipt(),
      scan_token: restorationScan.token,
      decisions,
    });
    if (operation.id !== providerOperationId) return;
    project.source_receipt = payload.source_receipt;
    restorationRecipe = payload.recipe;
    updateRestorationControls();
    setRestorationStatus("Reviewed restoration recipe saved", "success");
  } catch (error) {
    if (!handleRestorationError(error)) setRestorationStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
});

document.getElementById("renderRestoredSide").addEventListener("click", async () => {
  if (!restorationScan || !restorationRecipe) return;
  const operation = await beginRestorationArtifactOperation();
  if (!operation) return;
  setRestorationStatus("Streaming the full music range and proving unchanged PCM outside approved windows…", "busy");
  try {
    const payload = await postJson("/api/restoration/render", {
      ...projectIdentityReceipt(),
      scan_token: restorationScan.token,
      recipe_token: restorationRecipe.token,
    });
    if (operation.id !== providerOperationId) return;
    project.source_receipt = payload.source_receipt;
    const render = payload.render;
    const exact = render.pcm_proof?.outside_approved_windows_and_channels_identical;
    setRestorationStatus(
      `Restored side complete · ${render.repairs?.length || 0} repairs · outside-window identity ${exact ? "proved" : "needs review"} · SHA ${String(render.restored?.sha256 || "").slice(0, 12)}…`,
      exact ? "success" : "error",
    );
  } catch (error) {
    if (!handleRestorationError(error)) setRestorationStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
});

for (const player of Object.values(restorationAudio)) {
  player.addEventListener("play", () => {
    audio.pause();
    for (const other of Object.values(restorationAudio)) {
      if (other !== player) other.pause();
    }
  });
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  let payload;
  try { payload = await response.json(); }
  catch { payload = { error: `HTTP ${response.status}` }; }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function postJson(path, payload, options = {}) {
  return request(path, {
    ...options,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function updateCoverArt() {
  const hasArtwork = Boolean(project.metadata.cover_art_path);
  coverArt.classList.toggle("hidden", !hasArtwork);
  coverPlaceholder.classList.toggle("hidden", hasArtwork);
  if (hasArtwork) {
    coverArt.src = `/artwork?v=${encodeURIComponent(project.updated_at || Date.now())}`;
    document.getElementById("coverCaption").textContent = "Local artwork saved with this project.";
  } else {
    coverArt.removeAttribute("src");
    document.getElementById("coverCaption").textContent = "Artwork stays beside this project.";
  }
  const releaseId = project.metadata.musicbrainz_release_id;
  document.getElementById("releaseIdentity").textContent = releaseId
    ? `MusicBrainz release ${releaseId}`
    : "Manual metadata is always available offline.";
}

function updateExportIdentityNote() {
  document.getElementById("exportIdentityNote").textContent = project
    ? `Pinned request · revision ${project.revision} · project ${String(project.project_sha256 || "").slice(0, 12)}… · source ${String(project.source?.sha256 || "").slice(0, 12)}…`
    : "Export identity is unavailable until the project loads.";
}

function refreshProjectView() {
  for (const [key, input] of Object.entries(metadataInputs)) input.value = metadataValue(key);
  document.getElementById("lookupArtist").value = metadataValue("artist");
  document.getElementById("lookupAlbum").value = metadataValue("album");
  renderTrackTable();
  refreshMarkerSelect();
  resizeCanvas();
  updateCoverArt();
  renderPersistentHistory();
  updateExportIdentityNote();
}

function renderPersistentHistory() {
  const timeline = document.getElementById("historyTimeline");
  const restore = document.getElementById("restorePoint");
  timeline.replaceChildren();
  const events = [];
  for (const entry of project?.edit_history || []) {
    events.push({
      kind: "edit",
      timestamp: entry.timestamp,
      title: String(entry.action || "saved edit").replaceAll("-", " "),
      summary: entry.summary,
      sequence: entry.sequence,
    });
  }
  for (const [index, checkpoint] of (project?.checkpoints || []).entries()) {
    events.push({
      kind: "checkpoint",
      timestamp: checkpoint.created_at,
      title: checkpoint.name,
      summary: `Checkpoint at project revision ${checkpoint.project_revision}`,
      checkpointIndex: index,
    });
  }
  events.sort((left, right) => String(left.timestamp).localeCompare(String(right.timestamp)));
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "quiet";
    empty.textContent = "No saved edits yet. The analyzer baseline is still available as a restore point.";
    timeline.append(empty);
  }
  for (const event of events) {
    const item = document.createElement("article");
    item.className = `history-event${event.kind === "checkpoint" ? " checkpoint" : ""}`;
    const title = document.createElement("strong");
    title.textContent = event.title;
    const summary = document.createElement("span");
    summary.textContent = event.summary || "Saved project state";
    const time = document.createElement("span");
    const renderedTime = new Date(event.timestamp);
    time.textContent = Number.isNaN(renderedTime.getTime())
      ? String(event.timestamp)
      : renderedTime.toLocaleString();
    item.append(title, summary, time);
    timeline.append(item);
  }

  const options = [];
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Choose a saved state…";
  options.push(placeholder);
  if (project?.analyzer_baseline?.state) {
    const analyzer = document.createElement("option");
    analyzer.value = "analyzer";
    analyzer.textContent = "Analyzer baseline";
    options.push(analyzer);
  }
  (project?.checkpoints || []).forEach((checkpoint, index) => {
    const option = document.createElement("option");
    option.value = `checkpoint:${index}`;
    option.textContent = `Checkpoint · ${checkpoint.name}`;
    options.push(option);
  });
  [...(project?.edit_history || [])].reverse().forEach((entry) => {
    const before = document.createElement("option");
    before.value = `history-before:${entry.sequence}`;
    before.textContent = `Before #${entry.sequence} · ${entry.summary}`;
    options.push(before);
    const after = document.createElement("option");
    after.value = `history-after:${entry.sequence}`;
    after.textContent = `After #${entry.sequence} · ${entry.summary}`;
    options.push(after);
  });
  restore.replaceChildren(...options);
  restore.disabled = providerBusy || options.length <= 1;
  document.getElementById("restoreSavedState").disabled = true;
  document.getElementById("saveCheckpoint").disabled = providerBusy || !project;
}

function selectedPersistentState() {
  const value = document.getElementById("restorePoint").value;
  if (value === "analyzer") return project.analyzer_baseline?.state || null;
  const [kind, rawIdentifier] = value.split(":", 2);
  if (kind === "checkpoint") {
    const checkpoint = project.checkpoints?.[Number(rawIdentifier)];
    return checkpoint?.state || null;
  }
  if (kind === "history-before" || kind === "history-after") {
    const entry = project.edit_history?.find((item) => item.sequence === Number(rawIdentifier));
    return kind === "history-before" ? entry?.before || null : entry?.after || null;
  }
  return null;
}

function previewPersistentRestore() {
  const state = selectedPersistentState();
  if (!state || !Array.isArray(state.tracks) || typeof state.metadata !== "object") {
    setStatus("Choose a valid saved state to preview", "error");
    return;
  }
  pushBoundaryUndo(captureBoundaryState());
  stopBoundaryPreview(false);
  project.tracks = state.tracks.map((track) => ({ ...track }));
  project.metadata = { ...state.metadata };
  normalizeTrackNumbersAndTimes();
  analyzerTopologyCompatible = project.tracks.length === analyzerBaselineTrackCount;
  selectedMarker = null;
  markDirty();
  refreshProjectView();
  selectMarker(project.tracks.length > 1 ? 1 : 0);
  setStatus("Saved state loaded as an unsaved preview; audition it, then save or undo", "busy");
}

document.getElementById("restorePoint").addEventListener("change", () => {
  document.getElementById("restoreSavedState").disabled =
    providerBusy || !selectedPersistentState();
});
document.getElementById("restoreSavedState").addEventListener("click", previewPersistentRestore);

document.getElementById("saveCheckpoint").addEventListener("click", async () => {
  if (!project || providerBusy) return;
  if (dirty && !(await saveProject())) return;
  const label = document.getElementById("checkpointLabel").value.trim();
  if (!label) {
    setStatus("Enter a checkpoint label", "error");
    document.getElementById("checkpointLabel").focus();
    return;
  }
  setProviderBusy(true);
  setStatus("Saving checkpoint…", "busy");
  try {
    const payload = await postJson("/api/checkpoint", {
      name: label,
      expected_revision: project.revision,
      expected_project_sha256: project.project_sha256,
    });
    project = payload.project;
    document.getElementById("checkpointLabel").value = "";
    dirty = false;
    initializeBoundarySession({ preserveAnalyzerBaseline: true });
    refreshProjectView();
    document.getElementById("revisionIdentity").textContent = `REVISION ${project.revision}`;
    document.getElementById("saveIdentity").textContent = "STATE · all changes saved";
    setStatus("Checkpoint saved", "success");
  } catch (error) {
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
  } finally {
    setProviderBusy(false);
  }
});

function openLookup() {
  if (!project) {
    setStatus("The project has not loaded; lookup is unavailable", "error");
    return;
  }
  const panel = document.getElementById("lookupPanel");
  lookupReturnFocus = document.activeElement;
  panel.classList.remove("hidden");
  document.getElementById("findReleaseButton").setAttribute("aria-expanded", "true");
  document.getElementById("lookupArtist").value = metadataInputs.artist.value;
  document.getElementById("lookupAlbum").value = metadataInputs.album.value;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  panel.focus({ preventScroll: true });
}

async function loadRecognitionReadiness() {
  const element = document.getElementById("recognitionReadiness");
  try {
    const readiness = await request("/api/recognition/status");
    recognitionReady = Boolean(readiness.ready);
    element.classList.toggle("ready", recognitionReady);
    element.textContent = `Acoustic song ID · ${readiness.message}`;
  } catch (error) {
    recognitionReady = false;
    element.textContent = `Acoustic song ID unavailable · ${error.message}`;
  }
  if (project) renderTrackTable();
}

function releaseSummary(result) {
  const formats = (result.formats || []).join(", ") || "format unknown";
  const place = [result.date, result.country].filter(Boolean).join(" · ");
  const label = [result.label, result.catalog_number].filter(Boolean).join(" ");
  return [result.artist, place, formats, `${result.track_count || "?"} tracks`, label]
    .filter(Boolean)
    .join(" · ");
}

function renderReleaseResults(results) {
  metadataResults.replaceChildren();
  releasePreview.classList.add("hidden");
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "quiet";
    empty.textContent = "No releases matched. Try a shorter album title or remove punctuation.";
    metadataResults.append(empty);
    return;
  }
  for (const result of results) {
    const card = document.createElement("article");
    card.className = "release-result";
    const copy = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = result.title;
    const summary = document.createElement("p");
    summary.textContent = releaseSummary(result);
    const score = document.createElement("span");
    score.className = "result-score";
    score.textContent = ` ${Math.round(Number(result.score) || 0)}% match`;
    summary.append(score);
    copy.append(title, summary);
    const review = document.createElement("button");
    review.textContent = "Review pressing";
    review.dataset.providerAction = "true";
    review.disabled = providerBusy;
    review.addEventListener("click", () => loadRelease(result.id));
    card.append(copy, review);
    metadataResults.append(card);
  }
}

async function searchReleases() {
  if (providerBusy) return;
  const button = document.getElementById("runReleaseSearch");
  const artist = document.getElementById("lookupArtist").value.trim();
  const album = document.getElementById("lookupAlbum").value.trim();
  if (!artist || !album) {
    setStatus("Enter both artist and album before searching", "error");
    return;
  }
  releaseRequestId += 1;
  const requestId = releaseRequestId;
  releasePreview.replaceChildren();
  releasePreview.classList.add("hidden");
  button.disabled = true;
  metadataResults.textContent = "Searching MusicBrainz…";
  setStatus("Searching releases…", "busy");
  try {
    const payload = await postJson("/api/metadata/search", { artist, album });
    if (requestId !== releaseRequestId) return;
    renderReleaseResults(payload.results || payload.releases || []);
    setStatus("Choose the pressing that matches your record", "success");
  } catch (error) {
    if (requestId !== releaseRequestId) return;
    metadataResults.textContent = "";
    releasePreview.replaceChildren();
    releasePreview.classList.add("hidden");
    setStatus(error.message, "error");
  } finally {
    if (requestId === releaseRequestId) button.disabled = false;
  }
}

async function loadRelease(releaseId) {
  if (providerBusy) return;
  releaseRequestId += 1;
  const requestId = releaseRequestId;
  releasePreview.classList.remove("hidden");
  releasePreview.textContent = "Loading the pressing track list…";
  try {
    const payload = await postJson("/api/metadata/release", { release_id: releaseId });
    if (requestId !== releaseRequestId) return;
    const details = payload.release || payload;
    renderReleasePreview(details);
    releasePreview.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    if (requestId !== releaseRequestId) return;
    releasePreview.textContent = "";
    releasePreview.classList.add("hidden");
    setStatus(error.message, "error");
  }
}

function renderReleasePreview(details) {
  releasePreview.replaceChildren();
  const header = document.createElement("div");
  header.className = "release-preview-header";
  const copy = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = details.title;
  const summary = document.createElement("p");
  summary.className = "quiet";
  summary.textContent = releaseSummary(details);
  copy.append(title, summary);
  header.append(copy);
  releasePreview.append(header);

  const selections = details.selections || [];
  const list = document.createElement("div");
  list.className = "selection-list";
  if (!selections.length) {
    const empty = document.createElement("p");
    empty.className = "quiet";
    empty.textContent = "This pressing does not expose a usable ordered track selection.";
    list.append(empty);
  }
  for (const selection of selections) {
    const card = document.createElement("article");
    card.className = "selection-card";
    const selectionCopy = document.createElement("div");
    const heading = document.createElement("h3");
    const selectionCount = Number(selection.track_count) || (selection.tracks || []).length;
    const sameCount = selectionCount === project.tracks.length;
    heading.textContent = `${selection.label || selection.key} · ${selectionCount} tracks`;
    const names = (selection.tracks || []).map((track) => track.title).join(" · ");
    const tracks = document.createElement("p");
    tracks.className = "tracklist-preview";
    tracks.textContent = names;
    selectionCopy.append(heading, tracks);
    const apply = document.createElement("button");
    apply.className = "primary";
    apply.dataset.providerAction = "true";
    apply.disabled = providerBusy;
    apply.textContent = sameCount
      ? "Use metadata + artwork"
      : `Preview topology ${project.tracks.length} → ${selectionCount}`;
    apply.addEventListener("click", () => {
      if (sameCount) applyRelease(details.id, selection.key);
      else previewTopology(details.id, selection.key);
    });
    card.append(selectionCopy, apply);
    list.append(card);
  }
  releasePreview.append(list);
}

function renderTopologyProposal(payload) {
  const previous = releasePreview.querySelector(".topology-proposal");
  if (previous) previous.remove();
  const proposal = payload.proposal;
  const panel = document.createElement("section");
  panel.className = "topology-proposal";
  const kicker = document.createElement("span");
  kicker.className = "kicker";
  kicker.textContent = "REVERSIBLE TOPOLOGY PROPOSAL";
  const heading = document.createElement("h3");
  heading.textContent = `${payload.current_track_count} → ${payload.proposed_track_count} tracks · ${proposal.operation}`;
  const explanation = document.createElement("p");
  explanation.className = "quiet";
  explanation.textContent = proposal.uncertain
    ? "One or more proposed cuts need careful audition. Applying saves the complete prior state in persistent history."
    : "Every proposed cut has supporting gap or duration evidence. Audition the boundaries before applying.";
  panel.append(kicker, heading, explanation);

  const warnings = [...(proposal.warnings || [])];
  for (const boundary of proposal.boundaries || []) warnings.push(...(boundary.warnings || []));
  if (warnings.length) {
    const list = document.createElement("ul");
    list.className = "topology-warnings";
    for (const warning of [...new Set(warnings)]) {
      const item = document.createElement("li");
      item.textContent = warning;
      list.append(item);
    }
    panel.append(list);
  }

  const grid = document.createElement("div");
  grid.className = "topology-boundaries";
  for (const boundary of proposal.boundaries || []) {
    const item = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = `Cut ${boundary.boundary_number} · ${formatTime(boundary.chosen_seconds)}`;
    const detail = document.createElement("span");
    const evidence = boundary.candidate_match
      ? `${Math.round(boundary.candidate_match.score * 100)}% measured-gap support`
      : "duration/topology estimate only";
    detail.textContent = `${boundary.chosen_sample.toLocaleString()} samples · ${evidence} · ${Math.round(boundary.confidence * 100)}% confidence`;
    item.append(label, detail);
    grid.append(item);
  }
  panel.append(grid);
  const apply = document.createElement("button");
  apply.className = "primary";
  apply.textContent = "Apply proposal, save prior state, then fetch metadata";
  apply.addEventListener("click", applyTopologyProposal);
  panel.append(apply);
  releasePreview.append(panel);
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function previewTopology(releaseId, selectionKey) {
  const operation = await beginProviderOperation();
  if (!operation) return;
  setStatus("Building a reversible split/merge proposal…", "busy");
  try {
    const payload = await postJson("/api/topology/propose", {
      release_id: releaseId,
      selection_key: selectionKey,
      expected_revision: project.revision,
      expected_project_sha256: project.project_sha256,
    });
    if (operation.id !== providerOperationId) return;
    topologyProposalContext = {
      releaseId,
      selectionKey,
      proposal: payload.proposal,
    };
    renderTopologyProposal(payload);
    setStatus("Review every proposed topology boundary before applying", payload.proposal.uncertain ? "busy" : "success");
  } catch (error) {
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
}

async function applyTopologyProposal() {
  if (!topologyProposalContext) return;
  const context = topologyProposalContext;
  const operation = await beginProviderOperation();
  if (!operation) return;
  let topologySaved = false;
  setStatus("Saving the reviewed topology with an exact reversal state…", "busy");
  try {
    const applied = await postJson("/api/topology/apply", {
      proposal: context.proposal,
      expected_revision: project.revision,
      expected_project_sha256: project.project_sha256,
    });
    if (operation.id !== providerOperationId) return;
    project = applied.project;
    topologySaved = true;
    dirty = false;
    initializeBoundarySession({ preserveAnalyzerBaseline: true });
    refreshProjectView();
    document.getElementById("revisionIdentity").textContent = `REVISION ${project.revision}`;
    document.getElementById("saveIdentity").textContent = "STATE · topology saved";

    setStatus("Topology saved; applying release metadata and artwork…", "busy");
    const metadata = await postJson("/api/metadata/apply", {
      release_id: context.releaseId,
      selection_key: context.selectionKey,
      download_artwork: true,
      expected_revision: project.revision,
      expected_project_sha256: project.project_sha256,
    });
    if (operation.id !== providerOperationId) return;
    project = metadata.project;
    topologyProposalContext = null;
    dirty = false;
    initializeBoundarySession({ preserveAnalyzerBaseline: true });
    refreshProjectView();
    document.getElementById("revisionIdentity").textContent = `REVISION ${project.revision}`;
    document.getElementById("saveIdentity").textContent = "STATE · all changes saved";
    selectMarker(project.tracks.length > 1 ? 1 : 0);
    setStatus(metadata.warning || "Topology, metadata, and artwork applied", metadata.warning ? "busy" : "success");
  } catch (error) {
    if (topologySaved) {
      setStatus(
        `Topology was saved and remains reversible, but metadata application stopped: ${error.message}`,
        "error",
      );
    } else if (!handleProjectConflict(error)) {
      setStatus(error.message, "error");
    }
  } finally {
    endProviderOperation(operation.id);
  }
}

async function applyRelease(releaseId, selectionKey) {
  const operation = await beginProviderOperation();
  if (!operation) return;
  setStatus("Applying release metadata and fetching artwork…", "busy");
  try {
    const payload = await postJson("/api/metadata/apply", {
      release_id: releaseId,
      selection_key: selectionKey,
      download_artwork: true,
      expected_revision: project.revision,
      expected_project_sha256: project.project_sha256,
    });
    if (operation.id !== providerOperationId) return;
    if (editRevision !== operation.revision) {
      dirty = true;
      setStatus(
        "Release metadata was saved, but newer browser edits need reconciliation before continuing",
        "error",
      );
      return;
    }
    project = payload.project || await request("/api/project");
    dirty = false;
    initializeBoundarySession({ preserveAnalyzerBaseline: true });
    refreshProjectView();
    setStatus(payload.warning || "Release metadata applied and project saved", payload.warning ? "busy" : "success");
  } catch (error) {
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
  } finally {
    endProviderOperation(operation.id);
  }
}

async function identifyTrack(track) {
  if (!recognitionReady) {
    openLookup();
    setStatus("Acoustic ID needs the setup shown in the lookup panel", "error");
    return;
  }
  const operation = await beginProviderOperation(track.number);
  if (!operation) return;
  recognitionRequestId += 1;
  const requestId = recognitionRequestId;
  if (recognitionAbortController) recognitionAbortController.abort();
  recognitionAbortController = new AbortController();
  openLookup();
  metadataResults.textContent = `Fingerprinting track ${track.number} locally…`;
  releasePreview.classList.add("hidden");
  setStatus(`Identifying track ${track.number}…`, "busy");
  const requestedRegion = {
    track_number: track.number,
    start_sample: track.start_sample,
    end_sample_exclusive: track.end_sample,
  };
  try {
    const payload = await postJson(
      "/api/recognition/identify",
      { ...projectIdentityReceipt(), track_number: track.number },
      { signal: recognitionAbortController.signal },
    );
    if (
      requestId !== recognitionRequestId
      || operation.id !== providerOperationId
      || editRevision !== operation.revision
    ) return;
    requireCurrentResponseIdentity(payload, "Acoustic recognition");
    if (
      payload.track_region?.track_number !== requestedRegion.track_number
      || payload.track_region?.start_sample !== requestedRegion.start_sample
      || payload.track_region?.end_sample_exclusive !== requestedRegion.end_sample_exclusive
    ) {
      const conflict = new Error(
        "Acoustic recognition returned a different track region. Reload before using the result."
      );
      conflict.status = 409;
      throw conflict;
    }
    renderRecognitionResults(track, payload.matches || []);
  } catch (error) {
    if (error.name === "AbortError" || requestId !== recognitionRequestId) return;
    metadataResults.textContent = "";
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
  } finally {
    if (requestId === recognitionRequestId) {
      recognitionAbortController = null;
      endProviderOperation(operation.id);
    }
  }
}

function renderRecognitionResults(track, matches) {
  metadataResults.replaceChildren();
  const context = document.createElement("p");
  context.className = "quiet";
  context.textContent = `Acoustic matches for track ${track.number}: ${track.title}`;
  metadataResults.append(context);
  if (!matches.length) {
    const empty = document.createElement("p");
    empty.textContent = "No confident fingerprint match was returned for this track.";
    metadataResults.append(empty);
    setStatus("No acoustic match found", "busy");
    return;
  }
  for (const match of matches) {
    const card = document.createElement("article");
    card.className = "recognition-result";
    const copy = document.createElement("div");
    const heading = document.createElement("h3");
    heading.textContent = match.title || "Untitled match";
    const detail = document.createElement("p");
    const percent = Math.round((Number(match.score) || 0) * 100);
    const matchedArtist = match.artist || match.artist_credit || "Unknown artist";
    detail.textContent = `${matchedArtist} · ${percent}% acoustic match`;
    copy.append(heading, detail);
    const actions = document.createElement("div");
    actions.className = "track-actions";
    const use = document.createElement("button");
    use.className = "primary";
    use.textContent = "Use song match";
    use.addEventListener("click", () => {
      track.title = match.title || track.title;
      track.artist = matchedArtist === "Unknown artist" ? track.artist : matchedArtist;
      track.musicbrainz_recording_id = match.recording_mbid || "";
      clearBoundaryHistory();
      markDirty();
      renderTrackTable();
      setStatus(`Track ${track.number} updated; save to keep the match`, "success");
    });
    actions.append(use);
    const firstRelease = (match.releases || match.release_candidates || [])[0];
    if (firstRelease?.id || firstRelease?.release_id || firstRelease?.release_mbid) {
      const review = document.createElement("button");
      review.textContent = "Review its release";
      review.dataset.providerAction = "true";
      review.disabled = providerBusy;
      review.addEventListener("click", () => loadRelease(
        firstRelease.id || firstRelease.release_id || firstRelease.release_mbid
      ));
      actions.append(review);
    }
    card.append(copy, actions);
    metadataResults.append(card);
  }
  setStatus(`Found ${matches.length} acoustic match${matches.length === 1 ? "" : "es"}`, "success");
}

async function saveProject() {
  if (!project) {
    setStatus("The project has not loaded; nothing can be saved", "error");
    return false;
  }
  syncMetadataToTracks();
  const savingRevision = editRevision;
  const body = JSON.stringify({
    metadata: project.metadata,
    tracks: project.tracks,
    expected_revision: project.revision,
    expected_project_sha256: project.project_sha256,
  });
  const button = document.getElementById("saveButton");
  button.disabled = true;
  setStatus("Saving project…", "busy");
  try {
    const saved = await request("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    project.revision = saved.revision;
    project.project_sha256 = saved.project_sha256;
    project.source_receipt = saved.source_receipt;
    project.updated_at = saved.updated_at;
    document.getElementById("revisionIdentity").textContent = `REVISION ${project.revision}`;
    updateExportIdentityNote();
    if (editRevision === savingRevision) {
      if (saved.project) {
        project.analyzer_baseline = saved.project.analyzer_baseline;
        project.edit_history = saved.project.edit_history;
        project.checkpoints = saved.project.checkpoints;
        project.schema_version = saved.project.schema_version;
        project.default_output_dir = saved.project.default_output_dir;
      }
      dirty = false;
      document.getElementById("saveIdentity").textContent = "STATE · all changes saved";
      renderPersistentHistory();
      setStatus("Project saved", "success");
      scheduleEvidenceLoad(0);
      loadRestorationStatus();
      return true;
    }
    dirty = true;
    document.getElementById("saveIdentity").textContent = "STATE · newer edits unsaved";
    setStatus("Earlier changes saved; newer edits still need saving", "busy");
    return false;
  } catch (error) {
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
    return false;
  } finally {
    button.disabled = project === null || providerBusy;
  }
}

document.getElementById("saveButton").addEventListener("click", saveProject);

document.getElementById("findReleaseButton").addEventListener("click", openLookup);
document.getElementById("closeLookup").addEventListener("click", () => {
  document.getElementById("lookupPanel").classList.add("hidden");
  document.getElementById("findReleaseButton").setAttribute("aria-expanded", "false");
  if (lookupReturnFocus instanceof HTMLElement) lookupReturnFocus.focus();
});
document.getElementById("runReleaseSearch").addEventListener("click", searchReleases);
for (const input of [document.getElementById("lookupArtist"), document.getElementById("lookupAlbum")]) {
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchReleases();
  });
}

document.getElementById("exportButton").addEventListener("click", () => {
  if (!project) return;
  const panel = document.getElementById("exportPanel");
  exportReturnFocus = document.activeElement;
  panel.classList.remove("hidden");
  document.getElementById("exportButton").setAttribute("aria-expanded", "true");
  panel.scrollIntoView({ behavior: "smooth", block: "center" });
  panel.focus({ preventScroll: true });
});

document.getElementById("closeExport").addEventListener("click", () => {
  document.getElementById("exportPanel").classList.add("hidden");
  document.getElementById("exportButton").setAttribute("aria-expanded", "false");
  if (exportReturnFocus instanceof HTMLElement) exportReturnFocus.focus();
});

document.getElementById("runExport").addEventListener("click", async () => {
  if (!project) {
    setStatus("The project has not loaded; export is unavailable", "error");
    return;
  }
  const formats = [];
  if (document.getElementById("formatFlac").checked) formats.push("flac");
  if (document.getElementById("formatM4a").checked) formats.push("m4a");
  if (!formats.length) {
    setStatus("Select at least one output format", "error");
    return;
  }
  const speed = updateSpeedView();
  if (!speed) {
    setStatus("Correct the speed settings before export", "error");
    return;
  }
  if (dirty && !(await saveProject())) return;
  const button = document.getElementById("runExport");
  button.disabled = true;
  setStatus("Exporting tracks…", "busy");
  try {
    const result = await request("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...projectIdentityReceipt(),
        output_dir: document.getElementById("outputDir").value,
        formats,
        source_speed_factor: Math.abs(speed.factor - 1) < 1e-9 ? null : speed.factor,
      }),
    });
    requireCurrentResponseIdentity(result, "Export");
    setStatus(`Exported ${result.file_count} files to ${result.output_directory}`, "success");
  } catch (error) {
    if (!handleProjectConflict(error)) setStatus(error.message, "error");
  } finally {
    button.disabled = providerBusy || projectConflict;
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("resize", () => {
  resizeCanvas();
  resizeEvidenceCanvas();
});
coverArt.addEventListener("error", () => {
  coverArt.classList.add("hidden");
  coverPlaceholder.classList.remove("hidden");
  document.getElementById("coverCaption").textContent = "The saved artwork could not be displayed.";
});

async function load() {
  try {
    project = await request("/api/project");
    if (!project.source_receipt?.receipt || !project.project_sha256) {
      throw new Error("The review server did not supply an integrity receipt.");
    }
    audio.src = "/audio";
    audio.load();
    initializeBoundarySession();
    initializeSpeedControls();
    const sourceIntegrity = document.getElementById("sourceIntegrity");
    sourceIntegrity.textContent = "SOURCE VERIFIED";
    sourceIntegrity.className = "integrity-badge";
    document.getElementById("revisionIdentity").textContent = `REVISION ${project.revision}`;
    document.getElementById("saveIdentity").textContent = "STATE · all changes saved";
    document.getElementById("sourceSummary").textContent =
      `${project.source.filename} · ${formatTime(project.source.duration_seconds, false)} · ` +
      `${project.source.sample_rate.toLocaleString()} Hz · ${project.source.codec_name.toUpperCase()}`;
    document.getElementById("analysisStats").textContent =
      `${project.analysis.candidates.length} gap candidates · ` +
      `threshold ${project.analysis.silence_threshold_db.toFixed(1)} dBFS`;
    document.getElementById("outputDir").value = project.default_output_dir;
    document.getElementById("restorationScanStart").value = project.tracks[0].start_seconds.toFixed(6);
    document.getElementById("restorationScanEnd").value = project.tracks.at(-1).end_seconds.toFixed(6);
    refreshProjectView();
    resizeEvidenceCanvas();
    await loadRecognitionReadiness();
    setProviderBusy(false);
    selectMarker(project.tracks.length > 1 ? 1 : 0);
    await loadRestorationStatus();
    setStatus("Ready");
  } catch (error) {
    project = null;
    const sourceIntegrity = document.getElementById("sourceIntegrity");
    sourceIntegrity.textContent = "INTEGRITY FAILED";
    sourceIntegrity.className = "integrity-badge failed";
    document.getElementById("saveIdentity").textContent = "STATE · locked";
    setProviderBusy(true);
    setStatus(error.message, "error");
  }
}

setProviderBusy(true);
load();
