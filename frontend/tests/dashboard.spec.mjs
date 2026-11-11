import { mkdirSync } from "node:fs";
import { expect, test } from "@playwright/test";

const revision = "0123456789abcdef0123456789abcdef01234567";

function liveState(overrides = {}) {
  const names = ["alpha", "beta", "gamma"];
  const sites = names.map((name, index) => {
    const current = `${revision.slice(0, 39)}${index}`;
    return {
      name,
      status: "LIVE",
      provider: { resource: `InferenceProvider/${name}`, available: true, status: "READY", queueCapacity: 4 },
      metrics: {
        queue: [{ name: "llm_d_epp_average_queue_size", labels: { name: `pool-${name}` }, value: index }],
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
  const runId = "grid-signals-demo-20261109T120000Z";
  const window = (id, phase, trafficPath, offeredRps, achievedRps, p95, sampleCount, dimensions) => ({
    id, phase, phaseClass: phase, trafficPath, repeat: 1, offeredRps, offeredRequests: offeredRps * 30,
    requestedBodyBytes: 16384, verifiedBodyBytes: 16384, bodySizeSamples: sampleCount,
    achievedRps, completedRequests: Math.round(achievedRps * 30),
    httpErrors: trafficPath === "gateway" ? 4 : 0, timeouts: trafficPath === "gateway" ? 1 : 0,
    droppedIterations: trafficPath === "gateway" ? 2 : 0, responseBytes: 120000,
    latencyMs: { p50: p95 == null ? null : p95 * 0.6, p95, p99: p95 == null ? null : p95 * 1.4, sampleCount, minimumSamples: 100, eligible: sampleCount >= 100 },
    attribution: { status: trafficPath === "gateway" ? "partial" : "not-applicable", attributedRequests: trafficPath === "gateway" ? 500 : null,
      requestDenominator: trafficPath === "gateway" ? 600 : null, unattributedRequests: trafficPath === "gateway" ? 100 : null,
      dimensions: trafficPath === "gateway" ? dimensions : [] },
    timing: { startedAtUtc: `2026-11-09T12:0${phase === "pressure" ? "1" : "0"}:00Z`, finishedAtUtc: "2026-11-09T12:01:30Z", elapsedSeconds: 30 },
  });
  const windows = [
    window("base-25", "baseline", "gateway", 25, 22, 120, 120, [
      { name: "site", counts: { alpha: 440, epsilon: 60 } }, { name: "pool", counts: { "pool-alpha": 440, "pool-epsilon": 60 } },
    ]),
    window("pressure-25", "pressure", "gateway", 25, 20, 145, 110, [
      { name: "site", counts: { alpha: 350, epsilon: 150 } }, { name: "pool", counts: { "pool-alpha": 350, "pool-epsilon": 150 } },
    ]),
    window("direct-25", "direct-simulator-reference", "direct-reference", 25, 24, 80, 120, []),
  ];
  const report = {
    formatVersion: "gridzilla.dev/normalized-report/v1alpha1",
    run: { id: runId, type: "benchmark", status: "PASS", scenario: { id: "grid-llmd-signals", name: "Grid llm-d signals routing" },
      topology: { id: "five-region-test", description: "Controlled synthetic five-site topology", siteCount: 5, sites: ["alpha", "beta", "gamma", "delta", "epsilon"].map(name=>({name})) },
      createdAtUtc: "2026-11-09T12:00:00Z", startedAtUtc: "2026-11-09T12:00:00Z", finishedAtUtc: "2026-11-09T12:03:00Z",
      windowExecution: { plannedMeasurement: 6, attemptedMeasurement: 2, notAttemptedMeasurement: 4,
        plannedWarmup: 2, attemptedWarmup: 1, notAttemptedWarmup: 1,
        plannedTotal: 8, attemptedTotal: 3, notAttemptedTotal: 5 },
      source: { setup: { repository: "https://example.test/grid", revision }, gridzilla: { repository: "https://example.test/gridzilla", revision, dirty: false } },
      images: [{ site: "alpha", component: "gateway", reference: "gateway:test", imageId: "sha256:abc", sourceRevision: revision }],
      profile: { name: "baseline", version: 1, percentileMinimumSamples: 100, rates: [25, 50], window: "30s" },
      result: { status: "PASS", throughputEligibility: true, failureRecorded: false }, restorationStatus: "VERIFIED",
      restoration: { verified: true, pressureSite: "alpha", restoredAtUtc: "2026-11-09T12:03:00Z", selectionRestored: true },
    },
    measurements: { phases: [{ name: "baseline", windows: [windows[0]] }, { name: "pressure", windows: [windows[1]] }, { name: "direct reference", windows: [windows[2]] }] },
    routing: { dimensions: [{name:"site",values:["alpha","beta","gamma","delta","epsilon"]},{name:"pool",values:["pool-alpha","pool-epsilon"]}],
      attributionQuality: { attributedRequests: 1000, requestDenominator: 1200, unattributedRequests: 200 },
      windows: windows.map(item=>({phase:item.phase, phaseClass:item.phaseClass, offeredRps:item.offeredRps, repeat:item.repeat, trafficPath:item.trafficPath, attribution:item.attribution})) },
    resources: { host: [{ observedAtUtc: "2026-11-09T12:01:00Z", phase: "baseline", clientLocation: "local", cpuCount: 8, loadAverage: [1,1.2,1.4], memoryAvailableBytes: 8589934592, memoryTotalBytes: 17179869184, diskFreeBytes: 107374182400, loadGeneratorSaturated: false, missing: [] }],
      workloads: [{ observedAtUtc: "2026-11-09T12:01:00Z", site: "alpha", component: "gateway", podCount: 2, cpuCores: 0.4, memoryBytes: 300000000, missing: [] }],
      throughputEligibility: { eligible: true, reason: null, loadGeneratorLocation: "local", saturatedSampleCount: 0 } },
    signals: { kind: "llm-d-signals", timestampNote: "UTC and local monotonic observations are separate.", events: [
      { kind:"synthetic-pressure", name:"Simulator pressure applied", source:"llm-d-sim", observedAtUtc:"2026-11-09T12:01:00Z", site:"alpha", syntheticWaitingRequests:2 },
      { kind:"signal-boundary", name:"Fresh EPP sample", source:"Gridzilla observer", observedAtUtc:"2026-11-09T12:01:02Z", sites:[{site:"alpha",eppQueue:[2],eppSampleTimestampUtc:"2026-11-09T12:01:02Z",score:1.2,rank:2,overlayRevision:"rev-new",gatewayServingRevision:"rev-new"}] },
      { kind:"correlated-probe", name:"pressure route probe", source:"Gridzilla probe", observedAtUtc:"2026-11-09T12:01:05Z", durationMs:12, requestId:"probe-1", status:200, attribution:{site:"epsilon",pool:"pool-epsilon",backend:"backend-1"}, receipt:{found:true} },
    ] },
    limitations: ["Illustrative controlled evidence; not a live benchmark result."],
    evidenceFiles: ["run-result.json", "windows.jsonl", "source-lock.json"],
    ...overrides,
  };
  return report;
}

async function mockApi(page, { live = liveState(), runs = [], saved = null } = {}) {
  live.controls = {
    enabled: false,
    scenarios: live.sites?.length ? ["grid-llmd-signals"] : [],
    profiles: ["smoke", "baseline", "stepped", "body-sizes"],
    profileSettings: {}, endpointConfigurations: [], running: false,
    operation: null, operationId: null, pressureOptions: [], pressure: {phase:"idle"},
    ...live.controls,
  };
  const calls = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    calls.push({ method: request.method(), url: request.url(), body: request.method() === "POST" ? request.postDataJSON() : null });
    const url = new URL(request.url());
    if (url.pathname === "/api/state") {
      await route.fulfill({ json: live });
    } else if (url.pathname === "/api/preflight" && request.method() === "POST") {
      await route.fulfill({ json: {ok:true, status:"READY"} });
    } else if (url.pathname === "/api/run" && request.method() === "POST") {
      live.controls = { ...live.controls, running:true, operation:"benchmark", operationId:"ui-operation-0001",
        scenario:request.postDataJSON().scenario, endpointId:request.postDataJSON().configurationId || null };
      await route.fulfill({ status: 202, json: { accepted: true, profile: request.postDataJSON().profile,
        operationId: "ui-operation-0001", rates: request.postDataJSON().rates,
        soakSeconds: request.postDataJSON().soakSeconds } });
    } else if (url.pathname === "/api/stop" && request.method() === "POST") {
      live.controls = { ...live.controls, running:false, operation:null, operationId:null, lastOperationId:request.postDataJSON().operationId };
      await route.fulfill({ status: 202, json: { accepted: true, operationId: request.postDataJSON().operationId,
        restoration: "Gridzilla will restore simulator state" } });
    } else if (url.pathname === "/api/pressure" && request.method() === "POST") {
      const { value, site } = request.postDataJSON();
      live.controls = { ...live.controls, running: true, operation: "pressure",
        pressure: { phase: "accepted", value, capacity: 4, site, pool: `pool-${site}`, restorationVerified: null } };
      live.controls.operationId = "ui-pressure-operation-0001";
      await route.fulfill({ status: 202, json: { accepted: true, value, capacity: 4, site, pool: `pool-${site}`,
        operationId: live.controls.operationId } });
    } else if (url.pathname === "/api/pressure/restore" && request.method() === "POST") {
      live.controls = { ...live.controls, running: false, operation: null, stopRequested: false,
        pressure: { ...live.controls.pressure, phase: "restored", outcome: "PASS", restorationVerified: true } };
      await route.fulfill({ status: 202, json: { accepted: true, operationId: request.postDataJSON().operationId,
        restoration: "Baseline restoration and routing recovery verification started" } });
    } else if (url.pathname === "/api/runs") {
      await route.fulfill({ json: runs });
    } else if (url.pathname.startsWith("/api/runs/")) {
      const id = decodeURIComponent(url.pathname.split("/").pop());
      const result = saved && !Array.isArray(saved) ? saved : (Array.isArray(saved) ? saved.find(item => item.run?.id === id) : null);
      if (result) await route.fulfill({ json: result });
      else await route.fulfill({ status: 404, json: { error: "saved run not found" } });
      } else {
      await route.fulfill({ status: 404, json: { error: "unexpected API route" } });
    }
  });
  return calls;
}

test("top-right GitHub social link opens the Gridzilla repository", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const link = page.getByRole("link", { name: "Gridzilla on GitHub" });
  await expect(link).toHaveAttribute("href", "https://github.com/nerdalert/gridzilla");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noreferrer");
});

test("live view presents three ready sites and keeps simulator controls disabled", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/");

  await expect(page.getByTestId("live-badge")).toHaveText("LIVE");
  await expect(page.getByTestId("benchmark-progress")).toContainText("pressure measurement");
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
  await expect(page.getByTestId("control-disabled")).toContainText("controls are disabled");
  await expect(page.getByTestId("start-benchmark")).toBeDisabled();
  await expect(page.getByTestId("stop-benchmark")).toBeDisabled();
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("live metrics with no source timestamp do not claim the sample is old", async ({ page }) => {
  const live = liveState();
  live.sites[0].metrics.sampleAgeSeconds = null;
  await mockApi(page, { live });
  await page.goto("/");

  const queueMetric = page.getByTestId("site-card-alpha").getByTestId("metric-queue-signal");
  await expect(queueMetric).toContainText("0 / 4");
  await expect(queueMetric).toContainText("llm_d_epp_average_queue_size");
  await expect(queueMetric).toContainText("not a hard queue limit");
  await expect(queueMetric).toContainText("LIVE");
  await expect(queueMetric).toContainText("sample age unavailable");
  await expect(queueMetric).not.toContainText("old");
});

test("queue ratio never displays a missing score scale as zero", async ({ page }) => {
  const live = liveState();
  live.sites[0].provider.queueCapacity = null;
  live.sites[0].provider.queueCapacityMatches = false;
  await mockApi(page, { live });
  await page.goto("/");

  const queueMetric = page.getByTestId("site-card-alpha").getByTestId("metric-queue-signal");
  await expect(queueMetric).toContainText("0 / ?");
  await expect(queueMetric).toContainText("UNAVAILABLE");
  await expect(queueMetric).not.toContainText("0 / 0");
});

test("explicitly enabled local controls start only a listed profile", async ({ page }) => {
  const live = liveState({controls:{enabled:true,profiles:["smoke","baseline","stepped"],running:false,
    scenarios:["grid-llmd-signals"],
    profileSettings:{smoke:{rates:[0.2],soakSeconds:15,maxArrivalRatePerSecond:0.5,signalRate:0.2},
      baseline:{rates:[0.5,1,2],soakSeconds:60,maxArrivalRatePerSecond:2,signalRate:0.5},
      stepped:{rates:[25,50],soakSeconds:30,maxArrivalRatePerSecond:100,signalRate:10}},
    operation:null,pressureOptions:[{value:2,label:"2 / 4",capacity:4,site:"alpha",pool:"pool-alpha"},
      {value:4,label:"4 / 4",capacity:4,site:"alpha",pool:"pool-alpha"}],
    pressure:{phase:"idle",restorationVerified:null},profile:null,stopRequested:false,lastOutcome:null,lastRunId:null}});
  const calls = await mockApi(page, { live });
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/");

  await expect(page.getByTestId("benchmark-controls")).toContainText("Run a performance profile");
  await expect(page.getByTestId("benchmark-controls")).not.toContainText("Run a bounded performance profile");
  const controlsBeforeHero = await page.getByTestId("benchmark-controls").evaluate(element => {
    const hero = document.querySelector(".hero");
    return Boolean(hero && (element.compareDocumentPosition(hero) & Node.DOCUMENT_POSITION_FOLLOWING));
  });
  expect(controlsBeforeHero, "the run controls should appear before the live dashboard summary").toBeTruthy();
  await expect(page.getByTestId("pressure-alpha-2")).toContainText("Apply 2 / 4");
  await expect(page.getByTestId("pressure-alpha-4")).toContainText("Apply 4 / 4");
  await page.getByLabel("Profile").selectOption("baseline");
  await expect(page.getByTestId("arrival-rate-steps")).toHaveValue("0.5,1,2");
  await expect(page.getByTestId("soak-seconds")).toHaveValue("60");
  await page.getByTestId("start-benchmark").click();
  await expect(page.getByRole("status")).toContainText("baseline accepted");
  expect(calls.some(call => call.method === "POST" && call.url.endsWith("/api/run") && call.body?.profile === "baseline"
    && call.body?.rates === "0.5,1,2" && call.body?.soakSeconds === 60))
    .toBeTruthy();
});

test("arrival rates are editable, offer 10–100 request presets, and send the selected soak", async ({ page }) => {
  const live = liveState({controls:{enabled:true,profiles:["stepped"],running:false,
    scenarios:["grid-llmd-signals"],
    profileSettings:{stepped:{rates:[25,50],soakSeconds:30,maxArrivalRatePerSecond:100,signalRate:10}},
    pressureOptions:[],pressure:{phase:"idle"}}});
  const calls = await mockApi(page, { live });
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/");
  await page.getByLabel("Profile").selectOption("stepped");
  await expect(page.getByTestId("arrival-rate-steps")).toHaveValue("25,50");
  for (const rate of [10,25,50,75,100]) await expect(page.getByTestId(`rate-preset-${rate}`)).toBeEnabled();
  await page.getByTestId("rate-preset-75").click();
  await expect(page.getByTestId("arrival-rate-steps")).toHaveValue("75");
  await page.getByTestId("arrival-rate-steps").fill("10,25,50,75,100");
  await page.getByTestId("soak-seconds").fill("15");
  await page.getByTestId("start-benchmark").click();
  await expect(page.getByRole("status")).toContainText("accepted");
  expect(calls.some(call => call.method === "POST" && call.url.endsWith("/api/run")
    && call.body?.rates === "10,25,50,75,100" && call.body?.soakSeconds === 15), JSON.stringify(calls)).toBeTruthy();
});

test("generic endpoint UI uses the listed configuration, exact sizes, and its returned operation ID", async ({ page }) => {
  const live = liveState({controls:{enabled:true,scenarios:["grid-llmd-signals","generic-endpoint"],
    profiles:["body-sizes"],running:false,operation:null,
    profileSettings:{"body-sizes":{rates:[10],soakSeconds:15,maxArrivalRatePerSecond:100,
      requestBodySizesBytes:[16384,65536,262144],warmupSeconds:1,repetitions:3}},
    endpointConfigurations:[{id:"mock-api",name:"Mock endpoint",endpoint:"http://127.0.0.1:8081/respond",
      expectedStatuses:[200],padJsonPointer:"/messages/0/content"}],pressureOptions:[]}});
  const calls = await mockApi(page, {live});
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/");
  await page.getByRole("radio", {name:/Generic HTTP endpoint/}).check();
  await expect(page.getByTestId("endpoint-configuration")).toHaveValue("mock-api");
  await page.getByLabel("Profile").selectOption("body-sizes");
  for (const size of ["16 KiB (16,384 bytes)","64 KiB (65,536 bytes)","256 KiB (262,144 bytes)"]) {
    await expect(page.getByLabel(size)).toBeChecked();
  }
  await page.getByTestId("start-benchmark").click();
  await expect(page.getByRole("status")).toContainText("accepted");
  const start = calls.find(call=>call.method==="POST"&&call.url.endsWith("/api/run"));
  expect(start?.body).toMatchObject({scenario:"generic-endpoint",configurationId:"mock-api",
    profile:"body-sizes",requestBodySizesBytes:[16384,65536,262144]});
  expect(start?.body?.url).toBeUndefined();
  await expect(page.getByTestId("stop-benchmark")).toBeEnabled();
  await page.getByTestId("stop-benchmark").click();
  const stop = calls.find(call=>call.method==="POST"&&call.url.endsWith("/api/stop"));
  expect(stop?.body).toEqual({operationId:"ui-operation-0001"});
});

test("Grid readiness follows the handoff topology without a fixed site count", async ({ page }) => {
  const live = liveState();
  live.sites = live.sites.slice(0, 2);
  live.crossSite.sites = live.crossSite.sites.slice(0, 2);
  live.status = "LIVE";
  live.controls = {enabled:true,scenarios:["grid-llmd-signals"],profiles:["smoke"],running:false,
    profileSettings:{smoke:{rates:[0.5],soakSeconds:15,maxArrivalRatePerSecond:1,
      requestBodySizesBytes:[null]}},endpointConfigurations:[],pressureOptions:[]};
  await mockApi(page, {live});
  await page.goto("/");
  await expect(page.getByTestId("start-benchmark")).toBeEnabled();
  await expect(page.getByText("2/2 sites observed")).toBeVisible();
});

test("temporary queue pressure requests only an offered level and exposes early restore", async ({ page }) => {
  const live = liveState({controls:{enabled:true,profiles:["smoke","baseline","stepped"],running:false,
    operation:null,pressureOptions:[{value:2,label:"2 / 4",capacity:4,site:"alpha",pool:"pool-alpha"},
      {value:4,label:"4 / 4",capacity:4,site:"alpha",pool:"pool-alpha"},
      {value:2,label:"2 / 4",capacity:4,site:"beta",pool:"pool-beta"},
      {value:4,label:"4 / 4",capacity:4,site:"beta",pool:"pool-beta"}],
    pressure:{phase:"idle",restorationVerified:null}}});
  const calls = await mockApi(page, { live });
  await page.goto("/");

  await expect(page.getByTestId("region-pressure-beta")).toContainText("Temporary queue pressure");
  await page.getByTestId("pressure-beta-2").click();
  await expect(page.getByTestId("restore-pressure-beta")).toBeEnabled();
  await expect(page.getByTestId("pressure-state-beta")).toContainText("accepted · 2 / 4");
  await expect(page.getByTestId("region-pressure-alpha").getByRole("button", {name:"Restore baseline now"})).toHaveCount(0);
  await page.getByTestId("restore-pressure-beta").click();
  await expect(page.getByTestId("pressure-state-beta")).toContainText("restored · 2 / 4 · PASS · baseline restored");
  expect(calls.some(call => call.method === "POST" && call.url.endsWith("/api/pressure") && call.body?.value === 2 && call.body?.site === "beta"))
    .toBeTruthy();
  expect(calls.some(call => call.method === "POST" && call.url.endsWith("/api/pressure/restore")))
    .toBeTruthy();
});

test("enabled start stays disabled until all live readiness gates pass", async ({ page }) => {
  const live = liveState({status:"STALE",gates:{simulators:"READY",eppMetrics:"LIVE",providers:"READY",overlays:"STALE",gateways:"STALE"},
    controls:{enabled:true,profiles:["smoke","baseline","stepped"],running:false}});
  await mockApi(page, { live });
  await page.goto("/");
  await expect(page.getByTestId("start-benchmark")).toBeDisabled();
  await expect(page.getByTestId("stop-benchmark")).toBeDisabled();
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

  await expect(page.getByTestId("signal-timeline")).toContainText("Temporary simulator pressure applied");
  await expect(page.getByTestId("signal-timeline")).toContainText("Grid selected another provider");
  await expect(page.getByTestId("signal-timeline")).toContainText("Baseline routing recovered");
  await expect(page.locator(".hero")).toContainText("Baseline routing recovered");
  await expect(page.getByTestId("signal-timeline")).toContainText("RECOVERED");
  await expect(page.getByTestId("control-disabled")).toContainText("controls are disabled");
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("normalized report supports five-site runs, separates references, and renders saved views", async ({ page }) => {
  const report = savedRun();
  const calls = await mockApi(page, {
    runs: [{ id: report.run.id, status: "PASS", startedAtUtc: report.run.startedAtUtc, scenario:report.run.scenario, topology:report.run.topology, profile:report.run.profile, sourceIdentity:revision, throughputEligible:true }],
    saved: report,
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  await expect(page.getByTestId("runs-browser")).toContainText("five-region-test");
  await expect(page.getByTestId("runs-browser")).toContainText("Eligible");
  await expect(page.getByTestId("run-overview")).toContainText("5 sites");
  await expect(page.getByTestId("run-overview")).toContainText("2/6");
  await expect(page.getByTestId("window-execution")).toContainText("3 of 8 total windows attempted; 5 were not started");
  await expect(page.getByTestId("normalized-rps-chart")).toBeVisible();
  await expect(page.getByTestId("normalized-p95-chart")).toContainText("Gateway p95 response time");
  await expect(page.getByTestId("normalized-reference-chart")).toContainText("separate from Gateway traffic");
  if (process.env.GRIDZILLA_CAPTURE_SCREENSHOTS) {
    mkdirSync("docs/images", {recursive:true});
    await page.screenshot({path:"docs/images/gridzilla-run-overview-illustrative.png",fullPage:true});
  }
  await expect(page.getByTestId("run-overview")).toContainText("alpha");
  await expect(page.getByTestId("run-overview")).toContainText("epsilon");
  await expect(page.getByTestId("signals-tab")).toBeVisible();
  await page.getByTestId("signals-tab").click();
  await expect(page.getByTestId("normalized-signals-view")).toContainText("Simulator pressure applied");
  await expect(page.getByTestId("normalized-signals-view")).toContainText("Fresh EPP sample");
  await expect(page.getByTestId("normalized-signals-view")).toContainText("backend-1");
  await expect(page.getByTestId("signal-waterfall")).toContainText("2026-11-09T12:01:00Z");
  if (process.env.GRIDZILLA_CAPTURE_SCREENSHOTS) await page.screenshot({path:"docs/images/gridzilla-llmd-trace-illustrative.png",fullPage:true});
  await page.getByRole("tab", { name: "Resources & evidence" }).click();
  await expect(page.getByTestId("normalized-resources-view")).toContainText("Host resource observations");
  await expect(page.getByRole("link", { name: "source-lock.json" })).toHaveAttribute("href", `/api/runs/${report.run.id}/files/source-lock.json`);
  if (process.env.GRIDZILLA_CAPTURE_SCREENSHOTS) await page.screenshot({path:"docs/images/gridzilla-resources-illustrative.png",fullPage:true});
  expect(calls.every(call => call.method === "GET")).toBeTruthy();
});

test("run browser searches five-site topology and compares matching reports with caveats", async ({ page }) => {
  const first = savedRun();
  const second = savedRun({ run: { ...first.run, id: "second-run", topology: { id:"three-site-test", siteCount:3, sites:[{name:"alpha"},{name:"beta"},{name:"gamma"}] }, source:{...first.run.source, setup:{repository:"other",revision:"fedcba"}} } });
  await mockApi(page, { runs: [
    { id:first.run.id, status:"PASS", scenario:first.run.scenario, topology:first.run.topology, profile:first.run.profile, sourceIdentity:revision, throughputEligible:true },
    { id:second.run.id, status:"PASS", scenario:second.run.scenario, topology:second.run.topology, profile:second.run.profile, sourceIdentity:"fedcba", throughputEligible:true },
  ], saved: [first, second] });
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  await page.getByTestId("run-search").fill("five-region");
  await expect(page.getByTestId(`run-row-${first.run.id}`)).toBeVisible();
  await expect(page.getByTestId(`run-row-${second.run.id}`)).toHaveCount(0);
  await page.getByTestId("run-search").fill("");
  await page.getByLabel(`Compare ${first.run.id}`).check();
  await page.getByLabel(`Compare ${second.run.id}`).check();
  await page.getByTestId("compare-runs").click();
  await expect(page.getByTestId("run-comparison")).toContainText("Topology differs");
  await expect(page.getByTestId("run-comparison")).toContainText("Source or runtime image identities differ");
  await expect(page.getByTestId("run-comparison")).toContainText("achieved RPS · p95 · samples");
  await expect(page.getByTestId("run-comparison")).toContainText("do not establish statistical significance");
  if (process.env.GRIDZILLA_CAPTURE_SCREENSHOTS) await page.screenshot({path:"docs/images/gridzilla-comparison-illustrative.png",fullPage:true});
});

test("normalized p95 stays unavailable below its saved sample gate", async ({ page }) => {
  const report = savedRun();
  for (const phase of report.measurements.phases) for (const window of phase.windows) if (window.trafficPath === "gateway") {
    window.latencyMs = {p50:null,p95:null,p99:null,sampleCount:9,minimumSamples:100,eligible:false};
  }
  await mockApi(page, { runs: [{ id:report.run.id, status:"PASS" }], saved:report });
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  await expect(page.getByTestId("normalized-p95-chart")).toContainText("p95 unavailable: each window requires 100 samples");
});

test("saved mode handles an empty evidence directory", async ({ page }) => {
  await mockApi(page, { runs: [] });
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  await expect(page.getByTestId("saved-empty")).toContainText("No reportable evidence");
});

test("running normalized evidence remains inspectable", async ({ page }) => {
  const report = savedRun({ run: { ...savedRun().run, id:"grid-signals-active-run", status:"RUNNING", result:{status:"RUNNING",throughputEligibility:null}, progress:{runId:"grid-signals-active-run",status:"RUNNING",phase:"pressure-measurement",updatedAtUtc:"2026-11-09T12:01:00Z",trafficProgress:{action:"measurement-start",index:"008-pressure-r1",ratePerSecond:25,duration:"15s",completedWindows:7,expectedWindows:16}} } });
  await mockApi(page, { runs: [{ id:report.run.id,status:"RUNNING" }], saved:report });
  await page.goto("/");
  await page.getByRole("button", { name: "Runs" }).click();
  await expect(page.getByTestId("saved-run-detail")).toContainText("RUNNING");
  await expect(page.getByTestId("benchmark-progress")).toContainText("7/16 windows complete");
});
