import { useCallback, useEffect, useState } from "react";

const ageText = value => Number.isFinite(value) ? `${value.toFixed(1)}s old` : "sample age unavailable";
const formatNumber = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "UNAVAILABLE";
const shortRevision = value => value ? (String(value).length > 24 ? `${String(value).slice(0, 12)}…${String(value).slice(-7)}` : value) : "UNAVAILABLE";

function Badge({ value, testId }) {
  const normalized = ["LIVE", "READY", "PASS", "RECOVERED"].includes(value) ? "live" :
    ["STALE", "RUNNING", "PRESSURE"].includes(value) ? "stale" : "unavailable";
  return <span className={`badge ${normalized}`} data-testid={testId}>{value || "UNAVAILABLE"}</span>;
}

function Metric({ label, value, unit = "", status = "LIVE", age }) {
  return <div className="metric" data-testid={`metric-${label.toLowerCase().replaceAll(" ", "-")}`}>
    <span>{label}</span>
    <strong>{value === null || value === undefined ? "UNAVAILABLE" : `${value}${unit}`}</strong>
    <small><Badge value={status} /> {age !== undefined && <span>{ageText(age)}</span>}</small>
  </div>;
}

function CandidateTable({ candidates = [], title = "Candidates", testId }) {
  return <section className="panel" data-testid={testId}>
    <div className="panel-title"><div><h3>{title}</h3><p>Grid score and rank from this site's published overlay</p></div><Badge value={candidates.length ? "LIVE" : "UNAVAILABLE"} /></div>
    <div className="table-wrap"><table><thead><tr><th>Rank</th><th>Provider</th><th>Site / pool</th><th>Score</th><th>Signal</th></tr></thead>
      <tbody>{candidates.length ? candidates.slice().sort((a,b)=>a.rank-b.rank).map(item => <tr key={`${item.key}-${item.name}`}>
        <td><span className="rank">{item.rank}</span></td><td><code>{item.name || item.key || "unknown"}</code></td>
        <td>{item.site || item.cluster || "unknown"}</td><td>{formatNumber(item.score)}</td>
        <td><Badge value={item.fresh === true ? "LIVE" : item.fresh === false ? "STALE" : "UNAVAILABLE"} /></td>
      </tr>) : <tr><td colSpan="5" className="empty">No candidate data has been observed.</td></tr>}</tbody>
    </table></div>
  </section>;
}

function RevisionCard({ label, value, expected, status }) {
  const matches = Boolean(value && expected && value === expected);
  return <div className="revision-card">
    <span>{label}</span><strong title={value || ""}>{shortRevision(value)}</strong>
    <small><Badge value={status || (matches ? "LIVE" : "STALE")} /> {expected ? `expected ${shortRevision(expected)}` : "expected revision unavailable"}</small>
  </div>;
}

function PoolCard({ site }) {
  const metric = site.metrics || {};
  const queue = metric.queue?.[0]?.value;
  const ready = metric.readyEndpoints?.[0]?.value;
  const imageIdentities = site.resources?.imageIdentities || {};
  return <article className="panel pool-card" data-testid={`site-card-${site.name}`}>
    <div className="panel-title"><div><span className="eyebrow">SITE · {site.name}</span><h2>{site.name}</h2><p>{site.provider?.resource || "Provider identity unavailable"}</p></div><Badge value={site.status || "UNAVAILABLE"} /></div>
    <div className="metrics-grid">
      <Metric label="Waiting requests" value={queue} unit="" status={metric.status || "UNAVAILABLE"} age={metric.sampleAgeSeconds} />
      <Metric label="Ready endpoints" value={ready} status={ready > 0 ? "LIVE" : "UNAVAILABLE"} age={metric.sampleAgeSeconds} />
      <Metric label="Provider" value={site.provider?.available ? "Available" : "Unavailable"} status={site.provider?.status || "UNAVAILABLE"} />
      <Metric label="Queue capacity" value={site.provider?.queueCapacity ?? null} status={site.provider?.queueCapacityMatches ? "READY" : "UNAVAILABLE"} />
      <Metric label="Simulator pods" value={site.simulators?.length ? `${site.simulators.filter(item=>item.ready && item.adminApiReachable).length}/${site.simulators.length}` : null} status={site.simulators?.length && site.simulators.every(item=>item.ready && item.adminApiReachable) ? "READY" : "UNAVAILABLE"} />
    </div>
    <div className="revision-grid">
      <RevisionCard label="Published overlay" value={site.overlay?.revision} expected={site.overlay?.revision} status={site.overlay?.revision ? "LIVE" : "UNAVAILABLE"} />
      <RevisionCard label="Gateway accepted" value={site.gateway?.acceptedRevision} expected={site.overlay?.revision} />
      <RevisionCard label="Gateway serving" value={site.gateway?.servingRevision} expected={site.overlay?.revision} />
    </div>
    <div className="resource-line">
      <span>Pods</span><strong>{site.resources?.readyPods ?? "UNAVAILABLE"}</strong>
      <span>Restarts</span><strong>{site.resources?.restarts ?? "UNAVAILABLE"}</strong>
      <span>CPU</span><strong>{site.resources?.cpu ?? "UNAVAILABLE"}</strong>
      <span>Memory</span><strong>{site.resources?.memory ?? "UNAVAILABLE"}</strong>
    </div>
    <details className="image-identities" data-testid={`image-identities-${site.name}`}>
      <summary>Runtime image identity</summary>
      {Object.keys(imageIdentities).length ? <div className="image-identity-list">
        {Object.entries(imageIdentities).map(([component, identity]) => <div key={component}>
          <strong>{component}</strong><span>expected <code>{identity.expectedImageID || "UNAVAILABLE"}</code></span>
          {(identity.pods || []).map(pod => <span key={pod.pod || pod.imageID}>
            {pod.pod || "pod unavailable"}: <code>{pod.imageID || "UNAVAILABLE"}</code>
            <Badge value={pod.digestMatches === true ? "PASS" : pod.digestMatches === false ? "STALE" : "UNAVAILABLE"} />
          </span>)}
          {!identity.pods?.length && <span className="empty">No matching runtime Pods were observed.</span>}
        </div>)}
      </div> : <div className="empty">Runtime image identities are unavailable.</div>}
    </details>
  </article>;
}

function LineChart({ title, labels = [], values = [], unit = "", testId }) {
  const points = values.map((value, index) => ({ value: value == null ? null : Number(value), index }))
    .filter(point => point.value !== null && Number.isFinite(point.value));
  if (!points.length) return <section className="panel chart" data-testid={testId}><h3>{title}</h3><div className="empty">No eligible samples were saved.</div></section>;
  const width = 720, height = 220, pad = 35;
  const min = Math.min(...points.map(p => p.value));
  const max = Math.max(...points.map(p => p.value));
  const span = Math.max(1e-8, max - min);
  const coordinates = points.map(p => ({
    x: pad + p.index * (width - 2 * pad) / Math.max(1, values.length - 1),
    y: height - pad - (p.value - min) / span * (height - 2 * pad),
    index: p.index,
    value: p.value,
  }));
  return <section className="panel chart" data-testid={testId}>
    <h3>{title}</h3><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}>
      <line x1={pad} x2={pad} y1={pad} y2={height-pad} /><line x1={pad} x2={width-pad} y1={height-pad} y2={height-pad} />
      <polyline points={coordinates.map(point=>`${point.x},${point.y}`).join(" ")} />
      {coordinates.map(point=><circle key={point.index} cx={point.x} cy={point.y} r="4"><title>{labels[point.index] || `sample ${point.index+1}`}: {point.value} {unit}</title></circle>)}
    </svg><small>{unit} · only saved measurement windows are plotted</small>
  </section>;
}

function RouteBars({ counts = {}, windows = [] }) {
  const entries = Object.entries(counts);
  const max = Math.max(1, ...entries.map(([,value])=>Number(value)));
  return <section className="panel" data-testid="route-distribution"><div className="panel-title"><div><h3>Route distribution by measurement window</h3><p>Compared with deterministic Grid ranking; equal traffic is not expected.</p></div></div>
    {entries.length ? <><div className="bars">{entries.map(([site,value])=><div className="bar-row" key={site}><span>{site} · all windows</span><div className="bar-track"><div style={{width:`${Number(value)/max*100}%`}} /></div><strong>{value}</strong></div>)}</div>
      <div className="table-wrap"><table><thead><tr><th>Phase</th><th>Offered rate</th><th>Repeat</th><th>Attributed / HTTP</th><th>Unattributed</th><th>Selected sites</th></tr></thead><tbody>
        {windows.map((window,index)=><tr key={`${window.phase}-${window.ratePerSecond}-${window.repeat}-${index}`}>
          <td>{window.phase}</td><td>{window.ratePerSecond} req/s</td><td>{window.repeat}</td>
          <td>{window.attributedRequests} / {window.requestDenominator}</td><td>{window.unattributedRequests}</td>
          <td><code>{JSON.stringify(window.routes || {})}</code></td></tr>)}
      </tbody></table></div></> : <div className="empty">No route-attributed samples saved.</div>}
  </section>;
}

function BenchmarkProgress({ benchmark }) {
  if (!benchmark) return <section className="panel benchmark-progress" data-testid="benchmark-progress">
    <div><h3>Benchmark progress</h3><p>No Gridzilla run is active for this setup handoff.</p></div><Badge value="IDLE" />
  </section>;
  const traffic = benchmark.trafficProgress;
  return <section className="panel benchmark-progress" data-testid="benchmark-progress">
    <div><span className="eyebrow">GRIDZILLA RUN · {benchmark.runId || "unknown"}</span>
      <h3>{benchmark.phase || "phase unavailable"}</h3>
      <p>{traffic ? `${traffic.action} · ${traffic.index} · ${traffic.ratePerSecond} req/s · ${traffic.duration}` : "Readiness and signal gates are running before traffic."}</p>
      <small>{traffic ? `${traffic.completedWindows}/${traffic.expectedWindows} windows complete · status updated ${benchmark.updatedAtUtc || "time unavailable"}` : `status updated ${benchmark.updatedAtUtc || "time unavailable"}`}</small>
    </div><Badge value={benchmark.status || "UNAVAILABLE"} />
  </section>;
}

function Gridzilla() {
  const [mode, setMode] = useState("live");
  const [live, setLive] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState("");
  const [saved, setSaved] = useState(null);
  const [error, setError] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("smoke");
  const [controlMessage, setControlMessage] = useState("");
  const [pendingLaunch, setPendingLaunch] = useState(null);
  const [controlBusy, setControlBusy] = useState(false);

  const refreshLive = useCallback(async () => {
    const response = await fetch("/api/state", {cache:"no-store"});
    if (!response.ok) throw new Error(`Live state HTTP ${response.status}`);
    setLive(await response.json());
    setError("");
  }, []);

  const refreshRuns = useCallback(async () => {
    const response = await fetch("/api/runs", {cache:"no-store"});
    if (!response.ok) throw new Error(`Saved runs HTTP ${response.status}`);
    const items = await response.json();
    setRuns(items);
    if (!selectedRun && items.length) setSelectedRun(items[0].id);
  }, [selectedRun]);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try { await Promise.all([refreshLive(), refreshRuns()]); }
      catch (cause) { if (active) setError(cause instanceof Error ? cause.message : "Data unavailable"); }
    };
    void refresh();
    const interval = window.setInterval(refresh, 2000);
    return () => { active = false; window.clearInterval(interval); };
  }, [refreshLive, refreshRuns]);

  useEffect(() => {
    let active = true;
    if (!selectedRun) { setSaved(null); return () => { active = false; }; }
    fetch(`/api/runs/${encodeURIComponent(selectedRun)}`, {cache:"no-store"})
      .then(response => { if (!response.ok) throw new Error(`Saved evidence HTTP ${response.status}`); return response.json(); })
      .then(value => { if (active) setSaved(value); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : "Saved run unavailable"); });
    return () => { active = false; };
  }, [selectedRun]);

  useEffect(() => {
    if (!pendingLaunch || live?.controls?.running) return;
    const benchmark = live?.benchmark;
    const controls = live?.controls || {};
    const resultId = controls.lastRunId || benchmark?.runId;
    const outcome = controls.lastOutcome || benchmark?.status;
    if (controls.lastOutcome === "FAIL" && !controls.lastRunId) {
      setPendingLaunch(null);
      setControlMessage("Benchmark failed before result evidence was saved; check the local Gridzilla process and evidence directory.");
      return;
    }
    if (resultId && resultId !== pendingLaunch.previousRunId && ["PASS", "FAIL"].includes(outcome)) {
      setSelectedRun(resultId);
      setMode("saved");
      setPendingLaunch(null);
      setControlMessage(`Benchmark ${outcome.toLowerCase()}; showing its saved report.`);
    }
  }, [live, pendingLaunch]);

  const localSite = live?.localOverlay?.site;
  const liveSites = live?.sites || [];
  const localOverlay = live?.localOverlay || {};
  const crossSite = live?.crossSite || {};
  const allLive = live?.status === "LIVE" && liveSites.length === 3 && liveSites.every(site => site.status === "LIVE");
  const controls = live?.controls || {};
  const controlsReady = allLive && live?.gates?.simulators === "READY" && live?.gates?.eppMetrics === "LIVE" &&
    live?.gates?.providers === "READY" && live?.gates?.overlays === "LIVE" && live?.gates?.gateways === "READY";
  const startEnabled = controls.enabled && controlsReady && !controls.running && !controlBusy;
  const chart = saved?.chartInputs || {};
  const resourceSeries = saved?.resourceSeries || [];

  const startBenchmark = async () => {
    if (!window.confirm(`Run the bounded ${selectedProfile} profile? It sends measured Gateway traffic, applies synthetic simulator pressure, then restores and verifies the original state.`)) return;
    setControlBusy(true);
    setControlMessage(`Checking readiness before starting ${selectedProfile}…`);
    try {
      const response = await fetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({profile:selectedProfile})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Run start rejected (${response.status})`);
      const previousRunId = live?.benchmark?.runId || null;
      await refreshLive();
      setPendingLaunch({previousRunId});
      setControlMessage(`${selectedProfile} accepted. Progress is shown above; the saved report opens when restoration finishes.`);
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Benchmark start was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  const stopBenchmark = async () => {
    if (!window.confirm("Stop the current benchmark? Gridzilla will restore simulator state and verify routing recovery before it finishes.")) return;
    setControlBusy(true);
    try {
      const response = await fetch("/api/stop", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Stop rejected (${response.status})`);
      setControlMessage(result.restoration || "Stop requested; Gridzilla is restoring simulator state.");
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Stop request was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  return <main className="app-shell">
    <header className="topbar"><div className="brand"><span className="brand-mark">G</span><div><strong>GRIDZILLA</strong><small>Signals-based routing performance</small></div></div>
      <nav><button className={mode === "live" ? "active" : ""} onClick={()=>setMode("live")}>Live environment</button><button className={mode === "saved" ? "active" : ""} onClick={()=>setMode("saved")}>Saved runs</button></nav>
      <div className="header-status"><Badge value={mode === "saved" ? "HISTORICAL" : allLive ? "LIVE" : live?.status || "UNAVAILABLE"} testId="live-badge" /><span>{mode === "live" && controls.enabled ? "local controls enabled" : "read-only"}</span></div>
    </header>
    {error && <div className="alert" role="status">{error}</div>}
    {mode === "live" ? <>
      <section className="hero panel"><div><span className="eyebrow">{live?.phase || "CONNECTING"}</span><h1>Live Grid + llm-d signal path</h1>
        <p>llm-d-sim → authenticated EPP metrics → Grid score/rank → distributed overlay → Gateway revision → attributed request.</p>
        <small>Synthetic simulator signals are separate from measured Gateway traffic. {controls.enabled ? "Local controls run bounded profiles and restore simulator state." : "This view is read-only and does not create load or modify the cluster."}</small></div>
        <div className="hero-meta"><Badge value={live?.status || "UNAVAILABLE"} /><span>last sample {live?.observedAtUtc ? new Date(live.observedAtUtc).toLocaleTimeString() : "unavailable"}</span><span>source {live?.source?.revision ? `${live.source.revision.slice(0,12)}…` : "unavailable"}</span></div></section>
      <BenchmarkProgress benchmark={live?.benchmark} />
      <section className="state-strip" data-testid="readiness-states"><div><span>Simulators</span><Badge value={live?.gates?.simulators || "UNAVAILABLE"} /></div><div><span>EPP authenticated metrics</span><Badge value={live?.gates?.eppMetrics || "UNAVAILABLE"} /></div><div><span>Providers</span><Badge value={live?.gates?.providers || "UNAVAILABLE"} /></div><div><span>Overlays</span><Badge value={live?.gates?.overlays || "UNAVAILABLE"} /></div><div><span>Gateways serving</span><Badge value={live?.gates?.gateways || "UNAVAILABLE"} /></div></section>
      <section className="section-heading"><div><span className="eyebrow">POOL SIGNALS</span><h2>Simulator and EPP state</h2><p>Freshness is reported from the authenticated EPP scrape; missing timestamps are not shown as measured sample age.</p></div><span>{liveSites.length}/3 sites observed</span></section>
      <section className="site-grid">{liveSites.map(site=><PoolCard key={site.name} site={site} />)}{liveSites.length===0 && <div className="panel empty" data-testid="empty-state">No live cluster state has been observed. Check the handoff and local kubeconfig files.</div>}</section>
      <section className="overlay-layout">
        <CandidateTable title={`Local published overlay · ${localSite || "site unavailable"}`} candidates={localOverlay.candidates || []} testId="local-overlay" />
        <section className="panel" data-testid="cross-site-snapshot"><div className="panel-title"><div><h3>Cross-site overlay and Gateway snapshot</h3><p>Per-site revisions are compared independently; local overlay and peer distribution are distinct observations.</p></div><Badge value={crossSite.status || "UNAVAILABLE"} /></div>
          <div className="site-revisions">{(crossSite.sites || []).map(site=><div key={site.name} className="site-revision"><strong>{site.name}</strong><span>published {shortRevision(site.overlayRevision)}</span><span>serving {shortRevision(site.servingRevision)}</span><Badge value={site.status || "UNAVAILABLE"} /></div>)}
            {!crossSite.sites?.length && <div className="empty">No cross-site snapshot is available.</div>}</div></section>
      </section>
      <section className="panel timeline-panel" data-testid="signal-timeline"><div className="panel-title"><div><h3>Signal-to-route timeline</h3><p>Boundary events use local monotonic durations; timestamps from different clusters may have clock skew.</p></div><Badge value={live?.transition?.status || "UNAVAILABLE"} /></div>
        <div className="timeline">{(live?.timeline || []).map((item,index)=><div className="timeline-item" key={`${item.boundary}-${index}`}><span className="timeline-dot" /><strong>{item.boundary}</strong><span>{item.observedAtUtc || "time unavailable"}</span><small>{item.durationMs == null ? "duration not measured" : `${formatNumber(item.durationMs)} ms`}</small></div>)}
          {!live?.timeline?.length && <div className="empty">No transition has been run in this attached environment.</div>}</div></section>
      {controls.enabled ? <section className="panel read-only-controls benchmark-controls" data-testid="benchmark-controls">
        <div><h3>Run a bounded performance profile</h3><p>Start is enabled only while every simulator, EPP, provider, overlay, and Gateway gate is current. Pressure is synthetic and restoration is verified before completion.</p>
          {controlMessage && <small role="status">{controlMessage}</small>}</div>
        <div className="control-actions"><label htmlFor="benchmark-profile">Profile</label><select id="benchmark-profile" value={selectedProfile} disabled={!startEnabled}
          onChange={event=>setSelectedProfile(event.target.value)}>{(controls.profiles || []).map(name=><option key={name} value={name}>{name}</option>)}</select>
          <button data-testid="start-benchmark" disabled={!startEnabled} onClick={startBenchmark}>{controlBusy ? "Starting…" : "Start benchmark"}</button>
          <button data-testid="stop-benchmark" disabled={!controls.running || controlBusy} onClick={stopBenchmark}>{controls.stopRequested ? "Stopping safely…" : "Stop safely"}</button></div>
      </section> : <section className="panel read-only-controls" data-testid="control-disabled"><div><h3>Controller actions are disabled</h3><p>Restart the localhost dashboard with explicit <code>--allow-controls</code> to start or stop a bounded benchmark.</p></div><div><button disabled>Start benchmark</button><button disabled>Stop safely</button></div></section>}
      <section className="section-heading"><div><span className="eyebrow">RECENT RUNS</span><h2>Saved evidence</h2><p>Browse reports without contacting the clusters.</p></div><button onClick={()=>setMode("saved")}>Open run browser</button></section>
      <RunList runs={runs} onSelect={id=>{setSelectedRun(id);setMode("saved");}} />
    </> : <>
      <section className="hero panel"><div><span className="eyebrow">OFFLINE EVIDENCE</span><h1>Saved benchmark runs</h1><p>Reports and charts are read from saved JSON evidence only; no cluster requests are made in this mode.</p></div><Badge value="HISTORICAL" /></section>
      <section className="saved-layout"><section className="panel saved-list"><div className="panel-title"><div><h3>Run directory</h3><p>Choose a completed or partial evidence record.</p></div><Badge value={runs.length ? "HISTORICAL" : "UNAVAILABLE"} /></div>
        <RunList runs={runs} selected={selectedRun} onSelect={setSelectedRun} />{!runs.length && <div className="empty" data-testid="saved-empty">No reportable evidence directories found.</div>}</section>
        <section className="saved-detail" data-testid="saved-run-detail">{saved ? <>
          <div className="panel saved-summary"><div className="panel-title"><div><span className="eyebrow">{saved.runId || selectedRun}</span><h2>{saved.profile?.name || "Saved run"}</h2></div><Badge value={saved.status || "HISTORICAL"} /></div>
            <div className="metrics-grid"><Metric label="Windows" value={saved.windows?.filter(w=>w.type==="measurement").length ?? 0} status="HISTORICAL"/><Metric label="Probes" value={saved.probes?.length ?? 0} status="HISTORICAL"/><Metric label="Resource samples" value={saved.resourceSampleCount ?? 0} status="HISTORICAL"/></div>
          </div>
          {saved.status === "RUNNING" && <BenchmarkProgress benchmark={saved} />}
          <div className="metrics-grid"><Metric label="Restoration" value={saved.restoration?.verified ? "Verified" : "Not verified"} status={saved.restoration?.verified ? "RECOVERED" : "UNAVAILABLE"}/><Metric label="Throughput eligibility" value={saved.throughputClaim?.eligible ? "Eligible" : "Invalid"} status={saved.throughputClaim?.eligible ? "PASS" : "UNAVAILABLE"}/></div>
          <div className="chart-grid"><LineChart title="Achieved requests / second" labels={chart.labels} values={chart.achievedRps} unit="req/s" testId="saved-throughput-chart"/><LineChart title="p95 response time" labels={chart.labels} values={chart.p95LatencyMs} unit="ms" testId="saved-latency-chart"/><LineChart title="Host available memory" labels={resourceSeries.map(item=>`${item.phase || "sample"} · ${item.observedAtUtc || ""}`)} values={resourceSeries.map(item=>item.hostMemoryAvailableBytes == null ? null : item.hostMemoryAvailableBytes / 1024 / 1024)} unit="MiB" testId="saved-host-memory-chart"/><LineChart title="Cluster CPU sample" labels={resourceSeries.map(item=>`${item.phase || "sample"} · ${item.observedAtUtc || ""}`)} values={resourceSeries.map(item=>item.clusterCpuCores)} unit="cores" testId="saved-cluster-cpu-chart"/></div>
          <RouteBars counts={saved.routeCountsBySite || {}} windows={saved.routeDistributionByWindow || []} />
          <section className="panel" data-testid="saved-timeline"><div className="panel-title"><div><h3>Saved signal boundaries</h3><p>Replayed from evidence; values are historical.</p></div><Badge value="HISTORICAL" /></div>
            {(saved.signalBoundaries || []).length ? <div className="timeline">{saved.signalBoundaries.map((item,index)=><div className="timeline-item" key={index}><span className="timeline-dot"/><strong>{item.boundary}</strong><span>{item.observedAtUtc || item.time?.utc || "time unavailable"}</span></div>)}</div> : <div className="empty">No signal timeline was saved.</div>}</section>
          <section className="panel"><div className="panel-title"><div><h3>Evidence files</h3><p>Download saved run inputs and raw results.</p></div></div>
            {(saved.evidenceFiles || []).length ? <ul className="evidence-files">{saved.evidenceFiles.map(file => <li key={file}>
              <a href={`/api/runs/${encodeURIComponent(saved.runId || selectedRun)}/files/${encodeURIComponent(file)}`}>{file}</a>
            </li>)}</ul> : <div className="empty">Evidence index unavailable</div>}
          </section>
        </> : <div className="panel empty">Select a saved run to view its report.</div>}</section>
      </section>
    </>}
    <footer><span>Gridzilla · synthetic llm-d-sim signals</span><span>Not a real GPU capacity or token-throughput benchmark</span></footer>
  </main>;
}

function RunList({ runs = [], selected, onSelect }) {
  return <div className="run-list">{runs.map(run=><button key={run.id} className={`run-row ${selected===run.id?"selected":""}`} onClick={()=>onSelect(run.id)} data-testid={`run-${run.id}`}>
    <span><strong>{run.id}</strong><small>{run.startedAtUtc || "time unavailable"}</small></span><Badge value={run.status || "HISTORICAL"} /><span className="arrow">›</span>
  </button>)}</div>;
}

export default Gridzilla;
