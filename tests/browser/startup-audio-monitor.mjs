// The HTML preload is deliberately cancelled when app.js clears the unverified
// source. WebKit calls this "Load request cancelled" and may type it as "other".
// Accept only a navigation-startup cancellation proved replaced by a completed
// same-origin audio response and a healthy native player. Playback failures later
// in a test remain errors; this monitor never changes media or audition state.
export function createStartupAudioMonitor({ browserName, sourceUrl, problems }) {
  let load = null;
  let sequence = 0;
  const requests = new WeakMap();

  return {
    beginLoad() {
      if (load) {
        load.active = false;
        problems.push(...load.pending.map((failure) => failure.message));
      }
      load = { pending: [], completed: [], active: true };
    },
    requestStarted(request) {
      if (load?.active && request.method() === "GET" && request.url() === sourceUrl) {
        requests.set(request, { load, sequence: ++sequence });
      }
    },
    requestFinished(request, response) {
      const record = requests.get(request);
      if (record && response) {
        record.load.completed.push({
          sequence: record.sequence,
          status: response.status(),
          contentType: response.headers()["content-type"] || "",
        });
      }
    },
    holdStartupCancellation(request, failure, message) {
      const record = requests.get(request);
      if (
        browserName !== "webkit" || !record?.load.active || record.load !== load
        || failure !== "Load request cancelled"
        || !["media", "other"].includes(request.resourceType())
      ) return false;
      record.load.pending.push({ sequence: record.sequence, message });
      return true;
    },
    hasPending() {
      return Boolean(load?.pending.length);
    },
    hasReplacementProof() {
      return Boolean(load && load.pending.every((failure) => load.completed.some((response) => (
        response.sequence > failure.sequence
        && [200, 206].includes(response.status)
        && response.contentType.split(";", 1)[0].trim() === "audio/flac"
      ))));
    },
    finishLoad(player) {
      if (!load) return;
      load.active = false;
      const healthy = player?.currentSrc === sourceUrl
        && player.readyState >= 1 && player.error === null;
      for (const failure of load.pending) {
        const replacement = load.completed.some((response) => (
          response.sequence > failure.sequence
          && [200, 206].includes(response.status)
          && response.contentType.split(";", 1)[0].trim() === "audio/flac"
        ));
        if (!healthy || !replacement) problems.push(failure.message);
      }
      load.pending = [];
    },
  };
}
