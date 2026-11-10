import { expect, test } from "@playwright/test";

const revision = "0123456789abcdef0123456789abcdef01234567";

function liveState(overrides = {}) {
  const names = ["alpha", "beta", "gamma"];
  const sites = names.map((name, index) => {
    const current = `${revision.slice(0, 39)}${index}`;
    return {
      name,
      status: "LIVE",
      provider: { resource: `InferenceProvider/${name}`, available: true, status: "READY" },
      metrics: {
        queue: [{ value: index }],
        readyEndpoints: [{ value: 2 }],
        sampleAgeSeconds: 1.5,
        status: "LIVE",
      },
      overlay: {
        revision: current,
        candidates: [{ key: name, name: `provider-${name}`, site: name, score: 3 - index, rank: index, fresh: true }],
      },
      gateway: { acceptedRevision: current, servingRevision: current },
      simulators: [{ podName: `sim-${name}`, ready: true, adminApiReachable: true }],
      resources: { readyPods: 3, restarts: 0, cpu: "0.250 cores", memory: "128.0 MiB", imageIdentities: {
        "grid-controller": { expectedImageID: "sha256:expected", pods: [{ pod: `grid-${name}`, imageID: "containerd://sha256:expected", digestMatches: true, ready: true }] },
      } },
    };
  });
  return {
    schemaVersion: 1,
    status: "LIVE",
    phase: "baseline",
    observedAtUtc: "2026-11-09T12:00:00Z",
    source: { revision },
    sites,
    localOverlay: { site: "alpha", revision: sites[0].overlay.revision, candidates: sites[0].overlay.candidates },
    crossSite: { status: "LIVE", sites: sites.map(site => ({ name: site.name, overlayRevision: site.overlay.revision, servingRevision: site.gateway.servingRevision, status: site.status })) },
    gates: { simulators: "READY", eppMetrics: "LIVE", providers: "READY", overlays: "LIVE", gateways: "READY" },
    benchmark: { runId: "grid-signals-live-run", status: "RUNNING", phase: "pressure-measurement", updatedAtUtc: "2026-11-09T12:01:00Z", trafficProgress: { action: "measurement-start", index: "008-pressure-r1", ratePerSecond: 0.5, duration: "15s", completedWindows: 7, expectedWindows: 16 } },
    timeline: [],
    ...overrides,
  };
}

function savedRun(overrides = {}) {
  return {
    runId: "grid-signals-demo-20261109T120000Z",
    status: "PASS",
    profile: { name: "baseline" },
    windows: [
      { type: "measurement", phase: "baseline", ratePerSecond: 1, repeat: 1 },
      { type: "measurement", phase: "pressure", ratePerSecond: 1, repeat: 1 },
    ],
    probes: [{ requestId: "gz-smoke-1", site: "alpha" }],
    resourceSampleCount: 3,
    chartInputs: { labels: ["baseline", "pressure"], achievedRps: [1, 1], p95LatencyMs: [20, 25] },
    routeCountsBySite: { alpha: 8, beta: 2 },
    routeDistributionByWindow: [
      { phase: "baseline", ratePerSecond: 1, repeat: 1, routes: { alpha: 4 }, attributedRequests: 4, requestDenominator: 4, unattributedRequests: 0 },
      { phase: "pressure", ratePerSecond: 1, repeat: 1, routes: { beta: 2 }, attributedRequests: 2, requestDenominator: 2, unattributedRequests: 0 },
    ],
    signalBoundaries: [
      { boundary: "baseline-ready", observedAtUtc: "2026-11-09T12:00:00Z" },
      { boundary: "simulator-pressure-applied", observedAtUtc: "2026-11-09T12:01:00Z" },
      { boundary: "recovery-complete", observedAtUtc: "2026-11-09T12:02:00Z" },
    ],
    evidenceFiles: ["run-result.json", "windows.jsonl"],
    ...overrides,
  };
}

async function mockApi(page, { live = liveState(), runs = [], saved = null } = {}) {
  const calls = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    calls.push({ method: request.method(), url: request.url() });
    const url = new URL(request.url());
    if (url.pathname === "/api/state") {
      await route.fulfill({ json: live });
    } else if (url.pathname === "/api/runs") {
      await route.fulfill({ json: runs });
    } else if (url.pathname.startsWith("/api/runs/")) {
      if (saved) await route.fulfill({ json: saved });
      else await route.fulfill({ status: 404, json: { error: "saved run not found" } });
    } else {
      await route.fulfill({ status: 404, json: { error: "unexpected API route" } });
    }
  });
  return calls;
}

test("live view presents three ready sites and keeps simulator controls disabled", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/");

  await expect(page.getByTestId("live-badge")).toHaveText("LIVE");
  await expect(page.getByTestId("benchmark-progress")).toContainText("pressure-measurement");
  await expect(page.getByTestId("benchmark-progress")).toContainText("7/16 windows complete");
  await expect(page.getByTestId("site-card-alpha")).toBeVisible();
  await expect(page.getByTestId("site-card-alpha")).toContainText("Simulator pods");
  await page.getByTestId("image-identities-alpha").locator("summary").click();
  await expect(page.getByTestId("image-identities-alpha")).toContainText("containerd://sha256:expected");
  await expect(page.getByTestId("image-identities-alpha")).toContainText("PASS");
  await expect(page.getByTestId("site-card-beta")).toBeVisible();
  await expect(page.getByTestId("site-card-gamma")).toBeVisible();
  await expect(page.getByTestId("local-overlay")).toContainText("provider-alpha");
  await expect(page.getByTestId("cross-site-snapshot")).toContainText("published");
  await expect(page.getByTestId("control-disabled").getByRole("button")).toHaveCount(2);
  await expect(page.getByTestId("control-disabled").getByRole("button").nth(0)).toBeDisabled();
  await expect(page.getByTestId("control-disabled").getByRole("button").nth(1)).toBeDisabled();
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("live metrics with no source timestamp do not claim the sample is old", async ({ page }) => {
  const live = liveState();
  live.sites[0].metrics.sampleAgeSeconds = null;
  await mockApi(page, { live });
  await page.goto("/");

  const waitingMetric = page.getByTestId("site-card-alpha").getByTestId("metric-waiting-requests");
  await expect(waitingMetric).toContainText("LIVE");
  await expect(waitingMetric).toContainText("sample age unavailable");
  await expect(waitingMetric).not.toContainText("old");
});

test("stale and missing revisions are shown as stale or unavailable, not ready", async ({ page }) => {
  const stale = liveState();
  stale.sites[1].status = "STALE";
  stale.sites[1].gateway.servingRevision = "old-revision";
  stale.sites[2].gateway.acceptedRevision = null;
  stale.status = "STALE";
  stale.gates.gateways = "STALE";
  stale.crossSite.status = "STALE";
  stale.crossSite.sites[1].servingRevision = "old-revision";
  stale.crossSite.sites[2].status = "STALE";
  await mockApi(page, { live: stale });
  await page.goto("/");

  await expect(page.getByTestId("live-badge")).toHaveText("STALE");
  await expect(page.getByTestId("site-card-beta")).toContainText("old-revision");
  const accepted = page.locator(".revision-card").filter({ hasText: "Gateway accepted" }).last();
  await expect(accepted).toContainText("UNAVAILABLE");
  await expect(accepted).toContainText("STALE");
  await expect(page.getByTestId("cross-site-snapshot")).toContainText("STALE");
});

test("pressure and recovery boundaries are visible without exposing mutation controls", async ({ page }) => {
  const pressure = liveState({
    phase: "recovery-complete",
    transition: { status: "RECOVERED" },
    timeline: [
      { boundary: "simulator-pressure-applied", observedAtUtc: "2026-11-09T12:01:00Z", durationMs: 12 },
      { boundary: "epp-fresh-sample", observedAtUtc: "2026-11-09T12:01:02Z", durationMs: 2000 },
      { boundary: "grid-selection-shift", observedAtUtc: "2026-11-09T12:01:05Z", durationMs: 3000 },
      { boundary: "recovery-complete", observedAtUtc: "2026-11-09T12:02:00Z", durationMs: 55000 },
    ],
  });
  const calls = await mockApi(page, { live: pressure });
  await page.goto("/");

  await expect(page.getByTestId("signal-timeline")).toContainText("simulator-pressure-applied");
  await expect(page.getByTestId("signal-timeline")).toContainText("grid-selection-shift");
  await expect(page.getByTestId("signal-timeline")).toContainText("recovery-complete");
  await expect(page.getByTestId("signal-timeline")).toContainText("RECOVERED");
  await expect(page.getByTestId("control-disabled").getByRole("button")).toHaveCount(2);
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("saved run mode renders offline evidence and historical signal boundaries", async ({ page }) => {
  const calls = await mockApi(page, {
    runs: [{ id: "grid-signals-demo-20261109T120000Z", status: "PASS", startedAtUtc: "2026-11-09T12:00:00Z" }],
    saved: savedRun(),
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Saved runs" }).click();

  await expect(page.getByTestId("saved-run-detail")).toContainText("baseline");
  await expect(page.getByTestId("saved-throughput-chart")).toBeVisible();
  await expect(page.getByTestId("saved-latency-chart")).toBeVisible();
  await expect(page.getByTestId("route-distribution")).toContainText("alpha");
  await expect(page.getByTestId("route-distribution")).toContainText("8");
  await expect(page.getByTestId("route-distribution")).toContainText("Attributed / HTTP");
  await expect(page.getByTestId("route-distribution")).toContainText("pressure");
  await expect(page.getByTestId("saved-timeline")).toContainText("simulator-pressure-applied");
  const evidenceLink = page.getByRole("link", { name: "run-result.json" });
  await expect(evidenceLink).toHaveAttribute("href", "/api/runs/grid-signals-demo-20261109T120000Z/files/run-result.json");
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("saved mode handles an empty evidence directory", async ({ page }) => {
  await mockApi(page, { runs: [] });
  await page.goto("/");
  await page.getByRole("button", { name: "Saved runs" }).click();
  await expect(page.getByTestId("saved-empty")).toContainText("No reportable evidence");
});

test("active benchmark evidence remains inspectable in saved-run mode", async ({ page }) => {
  const active = { runId: "grid-signals-active-run", status: "RUNNING", phase: "pressure-measurement",
    updatedAtUtc: "2026-11-09T12:01:00Z", trafficProgress: { action: "measurement-start", index: "008-pressure-r1",
      phase: "pressure", ratePerSecond: 0.5, duration: "15s", completedWindows: 7, expectedWindows: 16 },
    windows: [{ type: "measurement", phase: "baseline", ratePerSecond: 0.5, repeat: 1 }] };
  await mockApi(page, { runs: [{ id: active.runId, status: "RUNNING", startedAtUtc: "2026-11-09T12:00:00Z" }], saved: active });
  await page.goto("/");
  await page.getByRole("button", { name: "Saved runs" }).click();
  await expect(page.getByTestId("saved-run-detail")).toContainText("RUNNING");
  await expect(page.getByTestId("saved-run-detail")).toContainText("7/16 windows complete");
});
