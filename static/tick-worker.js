// Tick Worker - runs in background thread, not throttled when tab is inactive

let tickInterval = null;

self.onmessage = function(e) {
    if (e.data.command === 'start') {
        const tickMs = e.data.tickMs || 33;
        if (tickInterval) clearInterval(tickInterval);
        tickInterval = setInterval(() => {
            self.postMessage({ type: 'tick' });
        }, tickMs);
    } else if (e.data.command === 'stop') {
        if (tickInterval) {
            clearInterval(tickInterval);
            tickInterval = null;
        }
    }
};
