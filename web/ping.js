// Heartbeat so the packaged exe knows a browser tab is still open.
const beat = () => fetch('/api/ping', { method: 'POST' }).catch(() => {});
beat();
setInterval(beat, 10000);
