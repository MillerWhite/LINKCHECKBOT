const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "miniapp.html"), "utf8");
const start = html.indexOf("const linkcheckNativeFetch=window.fetch.bind(window);");
const end = html.indexOf("\nlet isAdmin=false;", start);
assert.ok(start >= 0 && end > start, "fetch retry wrapper must be present");
const wrapper = html.slice(start, end);

function buildFetch(nativeFetch, timeoutMs = 15000, delayMs = 500) {
    const source = wrapper
        .replace("LINKCHECK_API_READ_TIMEOUT_MS=15000", `LINKCHECK_API_READ_TIMEOUT_MS=${timeoutMs}`)
        .replaceAll("500*(attempt+1)", `${delayMs}*(attempt+1)`);
    const window = {
        location: {
            href: "https://linkcheck.example/",
            origin: "https://linkcheck.example"
        },
        fetch: nativeFetch
    };
    vm.runInNewContext(source, {
        window,
        AbortController,
        Request,
        URL,
        setTimeout,
        clearTimeout,
        console: { warn() {} }
    });
    return window.fetch;
}

(async () => {
    // Transient network failures are retried, then the successful response is returned.
    {
        let calls = 0;
        const fetcher = buildFetch(async () => {
            calls++;
            if (calls < 3) throw new Error("temporary network failure");
            return { status: 200, body: { cancel: async () => {} } };
        }, 100, 1);
        const response = await fetcher("/api/stats");
        assert.equal(response.status, 200);
        assert.equal(calls, 3);
    }

    // Transient gateway responses are retried.
    {
        let calls = 0;
        const fetcher = buildFetch(async () => ({
            status: ++calls === 1 ? 503 : 200,
            body: { cancel: async () => {} }
        }), 100, 1);
        const response = await fetcher("/api/catalog");
        assert.equal(response.status, 200);
        assert.equal(calls, 2);
    }

    // Mutating POST requests are never retried.
    {
        let calls = 0;
        const fetcher = buildFetch(async () => {
            calls++;
            throw new Error("POST failure");
        }, 100, 1);
        await assert.rejects(fetcher("/api/check", { method: "POST" }), /POST failure/);
        assert.equal(calls, 1);
    }

    // Cross-origin requests are left untouched.
    {
        let calls = 0;
        const fetcher = buildFetch(async () => {
            calls++;
            throw new Error("external failure");
        }, 100, 1);
        await assert.rejects(fetcher("https://cdn.example/script.js"), /external failure/);
        assert.equal(calls, 1);
    }

    // A caller-aborted request is not started or retried.
    {
        let calls = 0;
        const fetcher = buildFetch(async () => {
            calls++;
            return { status: 200 };
        }, 100, 1);
        const controller = new AbortController();
        controller.abort();
        await assert.rejects(fetcher("/api/stats", { signal: controller.signal }), /aborted/);
        assert.equal(calls, 0);
    }

    // A stalled API read is aborted by the wrapper and receives bounded retries.
    {
        let calls = 0;
        const fetcher = buildFetch((url, init) => {
            calls++;
            return new Promise((resolve, reject) => {
                if (init.signal.aborted) return reject(new Error("timed out"));
                init.signal.addEventListener("abort", () => reject(new Error("timed out")), { once: true });
            });
        }, 15, 1);
        await assert.rejects(fetcher("/api/stats"), /timed out/);
        assert.equal(calls, 3);
    }

    process.stdout.write("6 fetch resilience scenarios passed\n");
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
