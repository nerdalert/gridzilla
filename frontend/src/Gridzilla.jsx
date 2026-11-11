import { useCallback, useEffect, useState } from "react";

const ageText = value => Number.isFinite(value) ? `${value.toFixed(1)}s old` : "sample age unavailable";
const formatNumber = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "UNAVAILABLE";
const shortRevision = value => value ? (String(value).length > 24 ? `${String(value).slice(0, 12)}…${String(value).slice(-7)}` : value) : "UNAVAILABLE";
const boundaryLabels = {
  "simulator-pressure-applied": "Temporary simulator pressure applied",
  "epp-fresh-sample": "Fresh EPP signal observed",
  "grid-selection-shift": "Grid selected another provider",
  "grid-overlay-and-gateway": "Overlay and Gateway revisions converged",
  "simulator-metrics-restored": "Simulator signal restored",
  "recovery-complete": "Baseline routing recovered",
};
const boundaryLabel = value => boundaryLabels[value] || String(value || "Unknown event").replaceAll("-", " ");
function p95EmptyMessage(chart = {}) {
  const minimum = Number(chart.percentileMinimumSamples) || 100;
  const counts = Array.isArray(chart.p95SampleCounts) ? chart.p95SampleCounts : [];
  const eligible = counts.filter(count => Number(count) >= minimum).length;
  const saved = counts.reduce((total, count) => total + (Number(count) || 0), 0);
  return eligible
    ? `p95 is not available in the saved chart data. ${eligible} windows had at least ${minimum} samples; inspect the per-window results.`
    : `p95 is hidden because each window needs at least ${minimum} latency samples. ${saved} samples were saved across ${counts.length} measurement windows.`;
}

function Badge({ value, testId }) {
  const normalized = ["LIVE", "READY", "PASS", "RECOVERED"].includes(value) ? "live" :
    ["STALE", "RUNNING", "PRESSURE"].includes(value) ? "stale" : "unavailable";
  return <span className={`badge ${normalized}`} data-testid={testId}>{value || "UNAVAILABLE"}</span>;
}

function Metric({ label, value, unit = "", status = "LIVE", age, description }) {
  return <div className="metric" data-testid={`metric-${label.toLowerCase().replaceAll(" ", "-")}`}>
    <span>{label}</span>
    <strong>{value === null || value === undefined ? "UNAVAILABLE" : `${value}${unit}`}</strong>
    {description && <small className="metric-description">{description}</small>}
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

function PoolCard({ site, controls, controlsReady, controlBusy, applyPressure, restorePressure }) {
  const metric = site.metrics || {};
  const queue = metric.queue?.[0]?.value;
  const ready = metric.readyEndpoints?.[0]?.value;
  const queueCapacity = site.provider?.queueCapacity;
  const queueNumber = Number(queue);
  const capacityNumber = Number(queueCapacity);
  const queueAvailable = queue != null && Number.isFinite(queueNumber);
  const capacityAvailable = queueCapacity != null && Number.isFinite(capacityNumber) && capacityNumber > 0 && site.provider?.queueCapacityMatches !== false;
  const queueValue = queueAvailable ? `${Number.isInteger(queueNumber) ? queueNumber : queueNumber.toFixed(1)} / ${capacityAvailable ? (Number.isInteger(capacityNumber) ? capacityNumber : capacityNumber.toFixed(1)) : "?"}` : null;
  const queueStatus = queueAvailable && capacityAvailable ? metric.status || "UNAVAILABLE" : "UNAVAILABLE";
  const queueMetricName = metric.queue?.[0]?.name || "configured EPP queue metric";
  const imageIdentities = site.resources?.imageIdentities || {};
  const pressureOptions = (controls?.pressureOptions || []).filter(item => item.site === site.name);
  const pressure = controls?.pressure || {};
  const pressureRunning = controls?.running && controls?.operation === "pressure";
  const pressureOwnedHere = pressure.site === site.name;
  return <article className="panel pool-card" data-testid={`site-card-${site.name}`}>
    <div className="panel-title"><div><span className="eyebrow">SITE · {site.name}</span><h2>{site.name}</h2><p>{site.provider?.resource || "Provider identity unavailable"}</p></div><Badge value={site.status || "UNAVAILABLE"} /></div>
    <div className="metrics-grid">
      <Metric label="Queue signal" value={queueValue} status={queueStatus} age={metric.sampleAgeSeconds}
        description={`Live EPP ${queueMetricName}: average waiting requests per endpoint. The denominator is queueCapacity, Grid's scoring scale, not a hard queue limit.`} />
      <Metric label="Ready endpoints" value={ready} status={ready > 0 ? "LIVE" : "UNAVAILABLE"} age={metric.sampleAgeSeconds} />
      <Metric label="Provider" value={site.provider?.available ? "Available" : "Unavailable"} status={site.provider?.status || "UNAVAILABLE"} />
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
    {controls?.enabled && pressureOptions.length > 0 && <div className="region-pressure" data-testid={`region-pressure-${site.name}`}>
      <div><strong>Temporary queue pressure</strong><small>Sets the synthetic average waiting requests for {site.provider?.pool || "this pool"} in {site.name}; it does not enqueue real requests. Gridzilla restores the captured simulator setting after 30 seconds, or when you choose Restore baseline now.</small></div>
      {pressureOwnedHere && pressure.phase && pressure.phase !== "idle" && <small className="region-pressure-state" data-testid={`pressure-state-${site.name}`}>
        {pressure.phase}{pressure.value != null ? ` · ${pressure.value} / ${pressure.capacity}` : ""}{pressure.selectedSite ? ` · route selected ${pressure.selectedSite}` : ""}{pressure.outcome ? ` · ${pressure.outcome}` : ""}{pressure.restorationVerified ? " · baseline restored" : ""}
      </small>}
      <div className="region-pressure-actions">{pressureOptions.map(option=><button key={`${option.value}-${option.capacity}`} data-testid={`pressure-${site.name}-${option.value}`}
        disabled={!controlsReady || controls.running || controlBusy} onClick={()=>applyPressure(site.name, option.value)}>{`Apply ${option.label}`}</button>)}
        {pressureRunning && pressureOwnedHere && <button data-testid={`restore-pressure-${site.name}`} disabled={controlBusy} onClick={restorePressure}>Restore baseline now</button>}
      </div>
    </div>}
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

function LineChart({ title, labels = [], values = [], unit = "", testId, xAxisLabel = "Measurement window", yAxisLabel = unit, emptyMessage = "No eligible samples were saved.", explanation = "" }) {
  const points = values.map((value, index) => ({ value: value == null ? null : Number(value), index }))
    .filter(point => point.value !== null && Number.isFinite(point.value));
  if (!points.length) return <section className="panel chart" data-testid={testId}><h3>{title}</h3><div className="empty">{emptyMessage}</div></section>;
  const width = 760, height = 270, left = 62, right = 22, top = 20, bottom = 62;
  const min = Math.min(...points.map(p => p.value));
  const max = Math.max(...points.map(p => p.value));
  const span = Math.max(1e-8, max - min);
  const coordinates = points.map(p => ({
    x: left + p.index * (width - left - right) / Math.max(1, values.length - 1),
    y: height - bottom - (p.value - min) / span * (height - top - bottom),
    index: p.index,
    value: p.value,
  }));
  const xTickIndexes = [...new Set([0, Math.round((values.length - 1) / 2), values.length - 1])]
    .filter(index=>coordinates.some(point=>point.index===index));
  const clockLabel = (label,index) => String(label || "").match(/T(\d{2}:\d{2}:\d{2})/)?.[1] || `#${index+1}`;
  return <section className="panel chart" data-testid={testId}>
    <h3>{title}</h3><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}>
      {[0,1,2,3,4].map(tick=>{const y=top+(height-top-bottom)*tick/4;const value=max-span*tick/4;return <g key={tick}><line className="chart-gridline" x1={left} x2={width-right} y1={y} y2={y}/><text className="chart-tick" x={left-8} y={y+4} textAnchor="end">{Number(value.toPrecision(3))}</text></g>;})}
      <line x1={left} x2={left} y1={top} y2={height-bottom} /><line x1={left} x2={width-right} y1={height-bottom} y2={height-bottom} />
      {xTickIndexes.map(index=>{const point=coordinates.find(item=>item.index===index);return <text className="chart-tick" key={index} x={point.x} y={height-bottom+17} textAnchor="middle"><title>{labels[index] || `window ${index+1}`}</title>{clockLabel(labels[index],index)}</text>;})}
      <polyline points={coordinates.map(point=>`${point.x},${point.y}`).join(" ")} />
      {coordinates.map(point=><circle key={point.index} cx={point.x} cy={point.y} r="4"><title>{labels[point.index] || `sample ${point.index+1}`}: {point.value} {unit}</title></circle>)}
      <text className="chart-axis-label" x={(left+width-right)/2} y={height-6} textAnchor="middle">{xAxisLabel}</text>
      <text className="chart-axis-label" transform={`translate(15 ${(top+height-bottom)/2}) rotate(-90)`} textAnchor="middle">{yAxisLabel}</text>
    </svg><small>Each point is one saved measurement window. X-axis shows the window start time in UTC; hover over a point for its phase, rate, repeat, and exact time. {explanation} {unit && `Values: ${unit}.`}</small>
  </section>;
}

function RouteBars({ counts = {}, windows = [], sites = [] }) {
  const displayedSites = [...new Set([...sites, ...Object.keys(counts)])];
  const entries = displayedSites.map(site=>[site, Number(counts[site] || 0)]);
  const max = Math.max(1, ...entries.map(([,value])=>Number(value)));
  return <section className="panel" data-testid="route-distribution"><div className="panel-title"><div><h3>Route distribution by measurement window</h3><p>Compared with deterministic Grid ranking; equal traffic is not expected.</p></div></div>
    {entries.length ? <><div className="bar-axis-title">Attributed Gateway requests across measured windows; direct simulator references are excluded</div><div className="bars">{entries.map(([site,value])=><div className="bar-row" key={site}><span>{site}</span><div className="bar-track"><div style={{width:`${Number(value)/max*100}%`}} /></div><strong>{value}</strong></div>)}</div><small className="bar-x-axis">Sites with 0 had no attributed requests in these windows; this is a site breakdown, not a per-pool chart.</small>
      <div className="table-wrap"><table><thead><tr><th>Phase</th><th>Window start (UTC)</th><th>Offered rate</th><th>Repeat</th><th>Attributed / HTTP</th><th>Unattributed</th><th>Selected sites</th></tr></thead><tbody>
        {windows.map((window,index)=>{const referencePath = window.referencePath || window.phase === "direct-simulator-reference";return <tr key={`${window.phase}-${window.ratePerSecond}-${window.repeat}-${index}`}>
          <td>{referencePath ? "Direct simulator reference" : window.phase}</td><td>{window.startedAtUtc || "time unavailable"}</td><td>{window.ratePerSecond} req/s</td><td>{window.repeat}</td>
          <td>{referencePath ? "not applicable" : `${window.attributedRequests} / ${window.requestDenominator}`}</td><td>{referencePath ? "not applicable" : window.unattributedRequests}</td>
          <td><code>{referencePath ? "bypasses Gateway" : JSON.stringify(Object.fromEntries(displayedSites.map(site=>[site, Number(window.routes?.[site] || 0)])))}</code></td></tr>;})}
      </tbody></table></div></> : <div className="empty">No route-attributed samples saved.</div>}
  </section>;
}

const reportWindows = report => (report?.measurements?.phases || []).flatMap(phase => phase.windows || []);
const display = value => value === null || value === undefined || value === "" ? "Unavailable" : value;
const siteNames = topology => (topology?.sites || []).map(site => site.name).filter(Boolean);
const topologyText = topology => {
  const sites = siteNames(topology);
  const count = topology?.siteCount == null ? "Site count unavailable" :
    `${topology.siteCount} site${topology.siteCount === 1 ? "" : "s"}`;
  return [count, topology?.id, topology?.description, sites.length ? sites.join(", ") : null].filter(Boolean).join(" · ");
};
const windowLabel = window => `${window.phase} · ${window.offeredRps ?? "?"} req/s · ${window.requestedBodyBytes ?? window.verifiedBodyBytes ?? "body size unavailable"} bytes · repeat ${window.repeat ?? "?"} · ${window.timing?.startedAtUtc || "time unavailable"}`;
const bodySizeText = window => window.requestedBodyBytes != null
  ? `${window.requestedBodyBytes} / ${display(window.verifiedBodyBytes)} bytes`
  : window.verifiedBodyBytes != null ? `Template / ${window.verifiedBodyBytes} bytes`
    : "Unavailable / unavailable bytes";
const median = values => {
  const sorted = values.filter(Number.isFinite).sort((a,b)=>a-b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle-1] + sorted[middle]) / 2;
};

function MultiSeriesChart({ title, labels = [], series = [], unit = "", xAxisLabel = "Measurement window (UTC)", yAxisLabel = unit, testId }) {
  const colors = ["#1769aa", "#d07821", "#29844b", "#844bb0"];
  const values = series.flatMap(item => item.values || []).filter(value => value != null && Number.isFinite(Number(value))).map(Number);
  if (!values.length) return <section className="panel chart" data-testid={testId}><h3>{title}</h3><div className="empty">No eligible samples were saved.</div></section>;
  const width=760,height=270,left=62,right=22,top=20,bottom=62;
  const min=Math.min(0,...values),max=Math.max(...values),span=Math.max(1e-8,max-min);
  const lineSegments=series.map((item,si)=>{
    const points=(item.values||[]).map((value,index)=>({value:value==null?null:Number(value),index})).filter(point=>point.value!=null&&Number.isFinite(point.value));
    const coordinates=points.map(point=>({x:left+point.index*(width-left-right)/Math.max(1,labels.length-1),
      y:height-bottom-(point.value-min)/span*(height-top-bottom),...point}));
    return {item,si,coordinates};
  });
  const ticks=[...new Set([0,Math.round((labels.length-1)/2),labels.length-1])].filter(i=>i>=0&&i<labels.length);
  const clock=label=>String(label||"").match(/T(\d{2}:\d{2}:\d{2})/)?.[1]||"time unavailable";
  return <section className="panel chart multi-chart" data-testid={testId}><h3>{title}</h3><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}>
    {[0,1,2,3,4].map(tick=>{const y=top+(height-top-bottom)*tick/4;const value=max-span*tick/4;return <g key={tick}><line className="chart-gridline" x1={left} x2={width-right} y1={y} y2={y}/><text className="chart-tick" x={left-8} y={y+4} textAnchor="end">{Number(value.toPrecision(3))}</text></g>;})}
    <line x1={left} x2={left} y1={top} y2={height-bottom}/><line x1={left} x2={width-right} y1={height-bottom} y2={height-bottom}/>
    {ticks.map(index=><text className="chart-tick" key={index} x={left+index*(width-left-right)/Math.max(1,labels.length-1)} y={height-bottom+17} textAnchor="middle"><title>{labels[index]}</title>{clock(labels[index])}</text>)}
    {lineSegments.map(({item,si,coordinates})=><g key={item.name}>
      <polyline style={{stroke:colors[si%colors.length]}} points={coordinates.map(point=>`${point.x},${point.y}`).join(" ")}/>
      {coordinates.map(point=><circle key={point.index} style={{fill:colors[si%colors.length]}} cx={point.x} cy={point.y} r="4"><title>{labels[point.index]} · {item.name}: {point.value} {unit}</title></circle>)}
    </g>)}
    <text className="chart-axis-label" x={(left+width-right)/2} y={height-6} textAnchor="middle">{xAxisLabel}</text>
    <text className="chart-axis-label" transform={`translate(15 ${(top+height-bottom)/2}) rotate(-90)`} textAnchor="middle">{yAxisLabel}</text>
  </svg><div className="chart-legend">{series.map((item,index)=><span key={item.name}><i style={{background:colors[index%colors.length]}}/>{item.name}</span>)}</div>
  <small>Points are saved measurement windows. Missing observations are not plotted. Values: {unit}.</small></section>;
}

function RunsBrowser({ runs, selected, onSelect, search, setSearch, compareIds, toggleCompare, compare }) {
  const query=search.trim().toLowerCase();
  const filtered=runs.filter(run=>[
    run.id,run.status,run.scenario?.name,run.scenario?.id,run.topology?.id,topologyText(run.topology),
    run.profile?.name,run.startedAtUtc,run.sourceIdentity,
  ].some(value=>String(value||"").toLowerCase().includes(query)));
  return <section className="panel runs-browser" data-testid="runs-browser">
    <div className="panel-title"><div><h2>Runs</h2><p>Search saved and partial runs without contacting any cluster. Select multiple runs for a bounded comparison.</p></div><Badge value={runs.length ? "HISTORICAL" : "UNAVAILABLE"}/></div>
    <div className="runs-toolbar"><label htmlFor="run-search">Search runs</label><input id="run-search" data-testid="run-search" value={search} onChange={event=>setSearch(event.target.value)} placeholder="Scenario, topology, profile, source…"/>
      <button data-testid="compare-runs" disabled={compareIds.length<2} onClick={compare}>Compare selected ({compareIds.length})</button></div>
    <div className="table-wrap"><table className="runs-table"><thead><tr><th>Compare</th><th>Run / result</th><th>Scenario</th><th>Topology</th><th>Profile</th><th>Date (UTC)</th><th>Source</th><th>Throughput claim</th></tr></thead><tbody>
      {filtered.map(run=><tr key={run.id} className={selected===run.id?"selected-run":""} data-testid={`run-row-${run.id}`}>
        <td><input type="checkbox" aria-label={`Compare ${run.id}`} checked={compareIds.includes(run.id)} onChange={()=>toggleCompare(run.id)}/></td>
        <td><button className="run-select" onClick={()=>onSelect(run.id)}><strong>{run.id}</strong><Badge value={run.status}/></button></td>
        <td>{run.scenario?.name || run.scenario?.id || "Unavailable"}<small>{run.type || "type unavailable"}</small></td>
        <td>{topologyText(run.topology)}</td>
        <td>{run.profile?.name || "Unavailable"}<small>{run.profile?.version == null ? "version unavailable" : `v${run.profile.version}`}</small></td>
        <td>{run.startedAtUtc || "Unavailable"}</td><td><code>{shortRevision(run.sourceIdentity)}</code></td>
        <td>{run.throughputEligible == null ? "Unavailable" : run.throughputEligible ? "Eligible" : "Not eligible"}</td>
      </tr>)}
      {!filtered.length&&<tr><td colSpan="8" className="empty">{runs.length?"No runs match this search.":"No saved run evidence found."}</td></tr>}
    </tbody></table></div>
  </section>;
}

function RunOverview({ report }) {
  const all=reportWindows(report).filter(window=>window.phaseClass!=="warmup");
  const generic=report.run?.scenario?.id==="generic-endpoint";
  const primaryPath=generic?"endpoint":"gateway";
  const primary=all.filter(window=>window.trafficPath===primaryPath);
  const reference=all.filter(window=>window.trafficPath==="direct-reference");
  const labels=primary.map(windowLabel);
  const primaryP95=primary.map(window=>window.latencyMs?.eligible?window.latencyMs.p95:null);
  const profile=report.run?.profile||{};
  const result=report.run?.result||{};
  const execution=report.run?.windowExecution;
  const measurementWindowValue=execution?.plannedMeasurement==null?all.length:`${execution.attemptedMeasurement}/${execution.plannedMeasurement}`;
  const routes=(generic?report.responseDimensions:report.routing)?.windows||[];
  const dimensionNames=(generic?report.responseDimensions:report.routing)?.dimensions||[];
  const sizeGroups=new Map();
  for(const window of primary){
    const key=`${window.requestedBodyBytes??window.verifiedBodyBytes??"unavailable"}|${window.offeredRps??"unavailable"}|${window.trafficPath}`;
    if(!sizeGroups.has(key))sizeGroups.set(key,[]);
    sizeGroups.get(key).push(window);
  }
  const sizeRows=[...sizeGroups.entries()].map(([key,windows])=>{
    const [bodyBytes,rate,path]=key.split("|");
    const achieved=windows.map(item=>Number(item.achievedRps)).filter(Number.isFinite);
    const p95=windows.filter(item=>item.latencyMs?.eligible&&Number.isFinite(Number(item.latencyMs.p95)));
    const bodyLabel=bodyBytes==="unavailable"?"Unavailable":windows[0]?.requestedBodyBytes==null
      ?`Template · ${Number(bodyBytes).toLocaleString()} bytes`:`${Number(bodyBytes).toLocaleString()} bytes`;
    return {bodyBytes,bodyLabel,rate,path,windows,meanAchieved:achieved.length?achieved.reduce((a,b)=>a+b,0)/achieved.length:null,
      eligibleP95Count:p95.length,meanP95:p95.length?p95.reduce((sum,item)=>sum+Number(item.latencyMs.p95),0)/p95.length:null};
  });
  const restoration=generic?"Not applicable":report.run?.restorationStatus||"UNKNOWN";
  const routeAttribution=generic?null:report.routing?.attributionQuality;
  return <div className="report-view" data-testid="run-overview">
    <section className="report-summary panel"><div className="panel-title"><div><span className="eyebrow">{report.run?.id}</span><h2>{report.run?.scenario?.name||"Run overview"}</h2>
      <p>{generic?`${report.run?.endpoint?.name||"Configured endpoint"} · HTTP JSON POST`:topologyText(report.run?.topology)} · {profile.name||"profile unavailable"} · {report.run?.startedAtUtc||"time unavailable"}</p></div><Badge value={report.run?.status||"UNKNOWN"}/></div>
      <div className="metrics-grid"><Metric label="Measurement windows attempted / planned" value={measurementWindowValue} status="HISTORICAL"/><Metric label="Throughput claim" value={result.throughputEligibility==null?"Unavailable":result.throughputEligibility?"Eligible":"Not eligible"} status={result.throughputEligibility?"PASS":"UNAVAILABLE"}/><Metric label="Restoration" value={restoration} status={generic?"HISTORICAL":report.run?.restorationStatus==="VERIFIED"?"RECOVERED":"UNAVAILABLE"}/>{generic?<Metric label="Response dimensions" value={dimensionNames.length||"Unavailable"} status={dimensionNames.length?"HISTORICAL":"UNAVAILABLE"}/>:<Metric label="Route attribution" value={routeAttribution?.attributedRequests==null?"Unavailable":`${routeAttribution.attributedRequests} / ${routeAttribution.requestDenominator??"?"}`} status="HISTORICAL"/>}</div>
      {execution?.plannedTotal!=null&&<small data-testid="window-execution">{execution.attemptedTotal} of {execution.plannedTotal} total windows attempted; {execution.notAttemptedTotal} were not started{execution.notAttemptedMeasurement||execution.notAttemptedWarmup?` (${execution.notAttemptedMeasurement} measurement, ${execution.notAttemptedWarmup} warmup)`:""}.</small>}
      {generic&&report.run?.endpoint&&<p className="report-limitation">Endpoint: <code>{report.run.endpoint.endpoint||"Unavailable"}</code>. The report records observed HTTP behavior for this configured workload; it does not claim Grid routing or synthetic pressure.</p>}
      {report.limitations?.map((item,index)=><p className="report-limitation" key={index}>{item}</p>)}
      {result.failureSummary&&<p className="report-limitation">{result.failureSummary}</p>}
      {!result.throughputEligibility&&<p className="report-limitation">Throughput comparison is not eligible: {report.resources?.throughputEligibility?.reason||"resource samples are missing or host saturation was observed."}</p>}
    </section>
    <div className="chart-grid report-chart-grid">
      <MultiSeriesChart title={`${generic?"Endpoint":"Gateway"} offered versus achieved requests/second`} labels={labels} unit="requests/second" yAxisLabel="Requests / second" testId="normalized-rps-chart" series={[
        {name:`Offered ${generic?"endpoint":"Gateway"} RPS`,values:primary.map(window=>window.offeredRps)},
        {name:`Achieved ${generic?"endpoint":"Gateway"} RPS`,values:primary.map(window=>window.achievedRps)},
      ]}/>
      <LineChart title={`${generic?"Endpoint":"Gateway"} p95 response time`} labels={labels} values={primaryP95} unit="ms" xAxisLabel="Measurement window (UTC)" yAxisLabel="Response time (ms)"
        emptyMessage={`p95 unavailable: each window requires ${profile.percentileMinimumSamples??"the profile minimum"} samples.`} testId="normalized-p95-chart"/>
      <MultiSeriesChart title={`${generic?"Endpoint":"Gateway"} errors and dropped iterations`} labels={labels} unit="requests" yAxisLabel="Requests" testId="normalized-errors-chart" series={[
        {name:"HTTP errors",values:primary.map(window=>window.httpErrors)},
        {name:"Dropped iterations",values:primary.map(window=>window.droppedIterations)},
      ]}/>
      {reference.length>0&&<MultiSeriesChart title={`${generic?"Direct reference endpoint":"Direct simulator reference"} (separate from ${generic?"configured endpoint":"Gateway"} traffic)`} labels={reference.map(windowLabel)} unit="requests/second" yAxisLabel="Requests / second" testId="normalized-reference-chart" series={[
        {name:"Offered reference RPS",values:reference.map(window=>window.offeredRps)},
        {name:"Achieved reference RPS",values:reference.map(window=>window.achievedRps)},
      ]}/>}
    </div>
    <section className="panel"><div className="panel-title"><div><h3>Request-size results</h3><p>Serialized UTF-8 body bytes are grouped by offered rate and traffic path; the rows retain individual measurement windows below.</p></div></div>
      <div className="table-wrap"><table><thead><tr><th>Requested body</th><th>Traffic path</th><th>Offered rate</th><th>Measurement windows</th><th>Mean achieved RPS</th><th>Mean eligible p95</th></tr></thead><tbody>{sizeRows.map(row=><tr key={`${row.bodyBytes}-${row.rate}-${row.path}`}><td>{row.bodyLabel}</td><td>{row.path}</td><td>{row.rate} req/s</td><td>{row.windows.length}</td><td>{row.meanAchieved==null?"Unavailable":row.meanAchieved.toFixed(2)}</td><td>{row.meanP95==null?`Unavailable · ${row.eligibleP95Count} eligible windows`:`${row.meanP95.toFixed(2)} ms · ${row.eligibleP95Count} windows`}</td></tr>)}{!sizeRows.length&&<tr><td colSpan="6" className="empty">No request-size measurement windows are saved.</td></tr>}</tbody></table></div>
    </section>
    <section className="panel"><div className="panel-title"><div><h3>Measurement windows</h3><p>Each row preserves its phase, repeat, traffic path, errors, drops, response size, and percentile sample gate.</p></div></div>
      <div className="table-wrap"><table><thead><tr><th>Phase / path</th><th>Requested / verified body</th><th>Rate</th><th>Repeat</th><th>Offered</th><th>Achieved RPS</th><th>Requests</th><th>HTTP errors</th><th>Timeouts</th><th>Dropped</th><th>Response bytes</th><th>p50 / p95 / p99 · samples</th></tr></thead><tbody>
        {all.map(window=><tr key={window.id}><td>{window.phase} · {window.trafficPath}</td><td>{bodySizeText(window)}<small>{window.bodySizeSamples??"?"} body-size samples</small></td><td>{display(window.offeredRps)} req/s</td><td>{display(window.repeat)}</td><td>{display(window.offeredRequests)}</td><td>{window.achievedRps==null?"Unavailable":Number(window.achievedRps).toFixed(2)}</td><td>{display(window.completedRequests)}</td><td>{display(window.httpErrors)}</td><td>{display(window.timeouts)}</td><td>{display(window.droppedIterations)}</td><td>{display(window.responseBytes)}</td><td>{window.latencyMs?.eligible?`${window.latencyMs.p50} / ${window.latencyMs.p95} / ${window.latencyMs.p99} ms · ${window.latencyMs.sampleCount}`: `Unavailable · ${display(window.latencyMs?.sampleCount)} samples`}</td></tr>)}
        {!all.length&&<tr><td colSpan="12" className="empty">No measurement windows are saved.</td></tr>}
      </tbody></table></div></section>
    {generic?<section className="panel"><div className="panel-title"><div><h3>Configured response dimensions</h3><p>These are bounded values read from configured response headers. They are observations, not proof of Grid route selection.</p></div></div>
      {routes.length?<div className="table-wrap"><table><thead><tr><th>Phase</th><th>Rate</th><th>Repeat</th><th>Path</th><th>Observed response dimensions</th><th>Request denominator</th></tr></thead><tbody>{routes.map((route,index)=><tr key={route.id||index}><td>{route.phase}</td><td>{display(route.offeredRps)} req/s</td><td>{display(route.repeat)}</td><td>{route.trafficPath}</td><td><code>{JSON.stringify(Object.fromEntries((route.dimensions||[]).map(item=>[item.name,item.counts])))||"Unavailable"}</code></td><td>{display(route.requestDenominator)}</td></tr>)}</tbody></table><small>Configured dimensions: {dimensionNames.map(dimension=>`${dimension.name} (${dimension.values.length} allowed values)`).join(" · ")||"Unavailable"}</small></div>:<div className="empty">No response-header dimensions were configured or observed.</div>}
    </section>:<section className="panel"><div className="panel-title"><div><h3>Grid route attribution by bounded dimension</h3><p>Observed values only; absent values are unavailable, not inferred as zero or equally distributed.</p></div></div>
      {routes.length?<div className="table-wrap"><table><thead><tr><th>Phase</th><th>Rate</th><th>Repeat</th><th>Path</th><th>Attribution</th><th>Observed route counts</th></tr></thead><tbody>{routes.map((route,index)=><tr key={route.id||index}><td>{route.phase}</td><td>{display(route.offeredRps)} req/s</td><td>{display(route.repeat)}</td><td>{route.trafficPath}</td><td>{route.attribution?.status||"Unavailable"} · {display(route.attribution?.attributedRequests)} / {display(route.attribution?.requestDenominator)}</td><td><code>{JSON.stringify(Object.fromEntries((route.attribution?.dimensions||[]).map(item=>[item.name,item.counts])))||"Unavailable"}</code></td></tr>)}</tbody></table><small>Known dimensions: {dimensionNames.map(dimension=>`${dimension.name} (${dimension.values.length} values)`).join(" · ")||"Unavailable"}</small></div>:<div className="empty">No routing dimensions were observed.</div>}
    </section>}
  </div>;
}

function SignalsView({ trace }) {
  if (!trace) return <section className="panel empty">This run has no saved signal timeline.</section>;
  return <section className="panel signals-waterfall" data-testid="normalized-signals-view">
    <div className="panel-title"><div><span className="eyebrow">LLM-D SIGNALS</span><h2>Signal-to-route trace</h2><p>Synthetic llm-d-sim pressure is shown separately from measured EPP queue values and request traffic. UTC wall-clock observations and process-local durations are not combined.</p></div><Badge value="HISTORICAL"/></div>
    <div className="signal-waterfall-track" data-testid="signal-waterfall">{trace.events.map((event,index)=><article className="signal-waterfall-event" key={`waterfall-${event.kind}-${index}`}>
        <span className="signal-waterfall-dot"/><small>{event.observedAtUtc||"UTC unavailable"}</small><strong>{boundaryLabel(event.name||event.kind)}</strong><span>{event.source||"source unavailable"}</span>
        {event.durationMs!=null&&<small>{event.durationMs.toFixed(1)} ms observed duration</small>}
      </article>)}</div>
      <div className="table-wrap"><table><thead><tr><th>Observed at (UTC)</th><th>Stage / source</th><th>Simulator synthetic setting</th><th>EPP queue / freshness</th><th>Grid score / rank</th><th>Overlay revision</th><th>Gateway serving revision</th><th>Correlated route probe</th></tr></thead><tbody>
      {trace.events.map((event,index)=>{
        const sites=event.sites||[];
        const queue=sites.length?sites.map(site=>`${site.site}: ${site.eppQueue?.length?site.eppQueue.join(", "):"Unavailable"}${site.eppSampleTimestampUtc?` · sample ${site.eppSampleTimestampUtc}`:" · sample time unavailable"}`).join("\n"):"Unavailable";
        const selection=event.selected?`${event.selected.site||event.selected.key||"site unavailable"} · score ${display(event.selected.score)} · rank ${display(event.selected.rank)}`:"Unavailable";
        const overlay=sites.length?sites.map(site=>`${site.site}: ${site.previousOverlayRevision||"Unavailable"} → ${site.overlayRevision||"Unavailable"}`).join("\n"):"Unavailable";
        const serving=sites.length?sites.map(site=>`${site.site}: ${site.previousGatewayServingRevision||"Unavailable"} → ${site.gatewayServingRevision||"Unavailable"}`).join("\n"):"Unavailable";
        const probe=event.kind==="correlated-probe"?`${event.status??"Unavailable"} · ${event.attribution?.site||"site unavailable"}/${event.attribution?.pool||"pool unavailable"}/${event.attribution?.backend||"backend unavailable"} · ${event.durationMs==null?"duration unavailable":`${event.durationMs.toFixed(2)} ms`} · receipt ${event.receipt?.found===true?"matched":"unavailable"} · request ${event.requestId||"unavailable"}`:"Unavailable";
        return <tr key={`${event.kind}-${index}`}><td>{event.observedAtUtc||"Unavailable"}<small>{event.durationMs==null?"duration unavailable":`${event.durationMs.toFixed(1)} ms monotonic`}</small></td>
          <td><strong>{boundaryLabel(event.name||event.kind)}</strong><small>{event.source||"source unavailable"}</small></td>
          <td>{event.syntheticWaitingRequests==null?"Unavailable":`${event.syntheticWaitingRequests} waiting requests${event.site?` · ${event.site}`:""}`}</td>
          <td><span className="trace-multiline">{queue}</span></td><td>{selection}</td><td><span className="trace-multiline">{overlay}</span></td><td><span className="trace-multiline">{serving}</span></td><td>{probe}</td></tr>;
      })}
      {!trace.events.length&&<tr><td colSpan="8" className="empty">No signal events were saved.</td></tr>}
    </tbody></table></div>
    <p className="report-limitation">This trace links saved pressure updates, EPP observations, Grid decisions, overlay/Gateway revisions, and sampled probes. It does not trace every k6 request. Missing revision or freshness fields in older evidence remain unavailable.</p>
  </section>;
}

function ResourcesEvidenceView({ report, selectedRun }) {
  const source=report.run?.source||{};
  const images=report.run?.images||[];
  const generic=report.run?.scenario?.id==="generic-endpoint";
  const restorationStatus=generic?"NOT_APPLICABLE":report.run?.restorationStatus||"UNKNOWN";
  return <div className="report-view" data-testid="normalized-resources-view">
    <section className="metrics-grid"><Metric label="Host saturation" value={report.resources?.throughputEligibility?.eligible==null?"Unavailable":report.resources.throughputEligibility.eligible?"No saturation observed":"Saturated / samples missing"} status={report.resources?.throughputEligibility?.eligible?"PASS":"UNAVAILABLE"}/><Metric label="Restoration" value={restorationStatus} status={generic?"HISTORICAL":report.run?.restorationStatus==="VERIFIED"?"RECOVERED":"UNAVAILABLE"}/><Metric label="Host samples" value={report.resources?.host?.length??0} status="HISTORICAL"/><Metric label="Workload samples" value={report.resources?.workloads?.length??0} status="HISTORICAL"/></section>
    <section className="panel"><div className="panel-title"><div><h3>Host resource observations</h3><p>Missing markers distinguish unavailable samples from measured zero.</p></div></div><div className="table-wrap"><table><thead><tr><th>Time (UTC)</th><th>Phase</th><th>Client</th><th>CPU count</th><th>Load average</th><th>Available memory</th><th>Free disk</th><th>Saturated</th><th>Missing</th></tr></thead><tbody>{(report.resources?.host||[]).map((item,index)=><tr key={index}><td>{item.observedAtUtc||"Unavailable"}</td><td>{display(item.phase)}</td><td>{display(item.clientLocation)}</td><td>{display(item.cpuCount)}</td><td>{item.loadAverage?.length?item.loadAverage.join(" / "):"Unavailable"}</td><td>{item.memoryAvailableBytes==null?"Unavailable":`${(item.memoryAvailableBytes/1048576).toFixed(0)} MiB`}</td><td>{item.diskFreeBytes==null?"Unavailable":`${(item.diskFreeBytes/1073741824).toFixed(1)} GiB`}</td><td>{item.loadGeneratorSaturated==null?"Unavailable":item.loadGeneratorSaturated?"Yes":"No"}</td><td>{item.missing?.join(", ")||"None"}</td></tr>)}{!report.resources?.host?.length&&<tr><td colSpan="9" className="empty">No host resource samples saved.</td></tr>}</tbody></table></div></section>
    <section className="panel"><div className="panel-title"><div><h3>Workload resources by topology dimension</h3><p>Pod names and UIDs are intentionally omitted.</p></div></div><div className="table-wrap"><table><thead><tr><th>Time (UTC)</th><th>Site / workload</th><th>Component</th><th>Pods observed</th><th>CPU cores</th><th>Memory</th><th>Missing</th></tr></thead><tbody>{(report.resources?.workloads||[]).map((item,index)=><tr key={index}><td>{item.observedAtUtc||"Unavailable"}</td><td>{item.site||"Cluster aggregate"}</td><td>{item.component}</td><td>{display(item.podCount)}</td><td>{item.cpuCores==null?"Unavailable":Number(item.cpuCores).toFixed(3)}</td><td>{item.memoryBytes==null?"Unavailable":`${(item.memoryBytes/1048576).toFixed(1)} MiB`}</td><td>{item.missing?.join(", ")||"None"}</td></tr>)}{!report.resources?.workloads?.length&&<tr><td colSpan="7" className="empty">No workload resource samples saved.</td></tr>}</tbody></table></div></section>
    <section className="panel"><div className="panel-title"><div><h3>Source and image identities</h3><p>Saved provenance; image digests are evidence fields, not metric labels.</p></div></div>
      <div className="identity-grid">{["setup","gridzilla"].map(name=><article key={name}><strong>{name} source</strong><span>{source[name]?.repository||"Repository unavailable"}</span><code>{source[name]?.revision||source[name]?.commit||source[name]?.sourceFingerprintSha256||"Revision unavailable"}</code><small>{source[name]?.branch||"Branch unavailable"}{source[name]?.dirty==null?"":source[name].dirty?" · dirty source":" · clean source"}</small></article>)}</div>
      <div className="table-wrap"><table><thead><tr><th>Site</th><th>Component</th><th>Image reference</th><th>Immutable image ID</th><th>Image source revision</th></tr></thead><tbody>{images.map((image,index)=><tr key={`${image.site}-${image.component}-${index}`}><td>{image.site||"Unspecified"}</td><td>{image.component||"Unspecified"}</td><td><code>{image.reference||"Unavailable"}</code></td><td><code>{image.imageId||"Unavailable"}</code></td><td><code>{image.sourceRevision||"Unavailable"}</code></td></tr>)}{!images.length&&<tr><td colSpan="5" className="empty">No runtime image identities saved.</td></tr>}</tbody></table></div>
    </section>
    <section className="panel"><div className="panel-title"><div><h3>{generic?"Scenario applicability":"Restoration proof"}</h3><p>{generic?"Simulator pressure and restoration do not apply to generic endpoint runs.":`Restoration status: ${restorationStatus}`}</p></div><Badge value={generic?"NOT APPLICABLE":report.run?.restorationStatus==="VERIFIED"?"RECOVERED":"UNAVAILABLE"}/></div>{!generic&&<p>Verified: {report.run?.restoration?.verified==null?"Unavailable":report.run.restoration.verified?"Yes":"No"} · pressure site {report.run?.restoration?.pressureSite||"Unavailable"} · restored at {report.run?.restoration?.restoredAtUtc||"Unavailable"} · route selection restored {report.run?.restoration?.selectionRestored==null?"Unavailable":report.run.restoration.selectionRestored?"Yes":"No"}</p>}</section>
    <section className="panel"><div className="panel-title"><div><h3>Saved evidence</h3><p>Open source records and raw observations used to create this report.</p></div></div>{report.evidenceFiles?.length?<ul className="evidence-files">{report.evidenceFiles.map(file=><li key={file}><a href={`/api/runs/${encodeURIComponent(selectedRun)}/files/${encodeURIComponent(file)}`}>{file}</a></li>)}</ul>:<div className="empty">No downloadable evidence files were indexed.</div>}</section>
  </div>;
}

function ComparisonView({ reports }) {
  const loaded=reports.filter(Boolean);
  if(loaded.length<2) return <section className="panel empty">Select at least two runs and load their reports to compare.</section>;
  const first=loaded[0].run;
  const profileSignature=profile=>JSON.stringify(Object.fromEntries(["name","version","window","warmup","signalRate","requestTimeout","maxOutputTokens","maxResponseBytes","maxConcurrency","preallocatedVUs","maxVUs","repetitions","requestBodySizesBytes"].map(key=>[key,profile?.[key]??null])));
  const compatible=loaded.every(report=>report.run?.scenario?.id===first?.scenario?.id&&profileSignature(report.run?.profile)===profileSignature(first?.profile));
  const warnings=[];
  const describe=run=>JSON.stringify({id:run?.topology?.id,sites:siteNames(run?.topology)});
  if(new Set(loaded.map(report=>describe(report.run))).size>1)warnings.push("Topology differs.");
  const sourceSignature=report=>JSON.stringify({source:report.run?.source,images:(report.run?.images||[]).map(image=>[image.site,image.component,image.imageId])});
  if(new Set(loaded.map(sourceSignature)).size>1)warnings.push("Source or runtime image identities differ.");
  const hostSignature=report=>{const host=report.resources?.host?.find(item=>item.cpuCount!=null||item.memoryTotalBytes!=null);return JSON.stringify(host?{cpu:host.cpuCount,memory:host.memoryTotalBytes}:null);};
  if(new Set(loaded.map(hostSignature)).size>1)warnings.push("Host capacity differs or is unavailable for some runs.");
  if(loaded.some(report=>!report.resources?.throughputEligibility?.eligible))warnings.push("At least one run is ineligible for a throughput claim.");
  if(loaded.some(report=>reportWindows(report).some(window=>window.offeredRps!=null&&(!window.latencyMs?.eligible||window.latencyMs?.p95==null))))warnings.push("At least one matching window lacks eligible p95 samples.");
  const keyFor=window=>`${window.phase}|${window.trafficPath}|${window.offeredRps}|${window.requestedBodyBytes??window.verifiedBodyBytes}|${window.repeat??"unavailable"}`;
  const keysByRun=loaded.map(report=>new Map(reportWindows(report).filter(window=>window.offeredRps!=null&&(window.requestedBodyBytes!=null||window.verifiedBodyBytes!=null)).map(window=>[keyFor(window),window])));
  const common=[...keysByRun[0].keys()].filter(key=>keysByRun.every(map=>map.has(key))).sort();
  return <section className="panel comparison-view" data-testid="run-comparison"><div className="panel-title"><div><h2>Run comparison</h2><p>Matched one window at a time by scenario, traffic path, phase, offered rate, serialized body size, and repeat. Repeats stay visible; a few repeats do not establish statistical significance.</p></div></div>
    <div className="comparison-warnings">{!compatible&&<p className="report-limitation">Selected runs do not share a matching scenario/profile contract, so rate windows are not compared.</p>}{warnings.map(item=><p className="report-limitation" key={item}>{item}</p>)}</div>
    {compatible&&common.length?<div className="table-wrap"><table><thead><tr><th>Matched window</th>{loaded.map(report=><th key={report.run.id}>{report.run.id}<small>achieved RPS · p95 · samples</small></th>)}</tr></thead><tbody>{common.map(key=>{const windows=keysByRun.map(map=>map.get(key));const baseline=windows[0];return <tr key={key}><td>{key.replaceAll("|"," · ")}</td>{windows.map((window,index)=>{const p95=window.latencyMs?.eligible?window.latencyMs.p95:null;const delta=baseline.achievedRps&&window.achievedRps!=null?`${((window.achievedRps-baseline.achievedRps)/baseline.achievedRps*100).toFixed(1)}% vs first run`:"throughput change unavailable";const p95Delta=baseline.latencyMs?.eligible&&p95!=null?`${(p95-baseline.latencyMs.p95).toFixed(2)} ms vs first run`:"p95 change unavailable";return <td key={loaded[index].run.id}>{window.achievedRps==null?"Unavailable":`${Number(window.achievedRps).toFixed(2)} req/s`} · {p95==null?"p95 unavailable":`${p95} ms`}<small>{window.completedRequests??"?"} completed · {window.latencyMs?.sampleCount??"?"} latency samples · {delta} · {p95Delta}</small></td>;})}</tr>;})}</tbody></table></div>:compatible?<div className="empty">No matching phase/path/rate/body-size/repeat windows exist across all selected runs.</div>:null}
    <p className="report-limitation">A few repeats do not establish statistical significance. Compare setup, source/image locks, host capacity, sample counts, and eligibility before interpreting differences.</p>
  </section>;
}

function BenchmarkProgress({ benchmark, scenario = "grid-llmd-signals" }) {
  if (!benchmark) return <section className="panel benchmark-progress" data-testid="benchmark-progress">
    <div><h3>Benchmark progress</h3><p>{scenario === "generic-endpoint" ? "No endpoint run is active." : "No Grid run is active for this setup handoff."}</p></div><Badge value="IDLE" />
  </section>;
  const traffic = benchmark.trafficProgress;
  const generic = benchmark.scenario === "generic-endpoint";
  const phase = benchmark.phase || "phase unavailable";
  const index = traffic?.index || benchmark.currentWindow;
  const rate = traffic?.ratePerSecond ?? benchmark.offeredRatePerSecond;
  const requestedBytes = traffic?.requestedBodyBytes ?? benchmark.requestedBodyBytes;
  return <section className="panel benchmark-progress" data-testid="benchmark-progress">
    <div><span className="eyebrow">{generic ? "GENERIC ENDPOINT" : "GRIDZILLA RUN"} · {benchmark.runId || "run ID pending"}</span>
      <h3>{generic ? phase.replaceAll("-", " ") : boundaryLabel(phase)}</h3>
      <p>{index ? `${traffic?.action || "window"} · ${index} · ${rate ?? "?"} req/s · ${requestedBytes == null ? "template body" : `${requestedBytes} bytes`}` : "Readiness and signal gates are running before traffic."}</p>
      <small>{traffic ? `${traffic.completedWindows}/${traffic.expectedWindows} windows complete · status updated ${benchmark.updatedAtUtc || "time unavailable"}` : `status updated ${benchmark.updatedAtUtc || "time unavailable"}`}</small>
    </div><Badge value={benchmark.status || "UNAVAILABLE"} />
  </section>;
}

function GenericLiveView({ endpoint, operation, runs, onSelectRun }) {
  return <>
    <section className="hero panel generic-endpoint-hero" data-testid="generic-endpoint-live">
      <div><span className="eyebrow">GENERIC HTTP ENDPOINT</span><h1>Endpoint benchmark</h1>
        <p>Measure observed HTTP behavior for the configured JSON POST workload. Grid, Kubernetes, simulator signals, and synthetic restoration are not used.</p>
        {endpoint ? <div className="endpoint-summary"><strong>{endpoint.name}</strong><code>{endpoint.endpoint}</code><span>Expected HTTP {endpoint.expectedStatuses?.join(", ") || "status unavailable"}</span><span>{endpoint.padJsonPointer ? `Size padding field ${endpoint.padJsonPointer}` : "Template-sized request only"}</span></div> : <small>No validated endpoint configuration is loaded.</small>}
        <small>This measures the selected endpoint and workload. It does not establish GPU capacity, model quality, or real token throughput.</small>
      </div><Badge value={operation?.status || (endpoint ? "CONFIGURED" : "UNAVAILABLE")}/>
    </section>
    <BenchmarkProgress benchmark={operation} scenario="generic-endpoint"/>
    <section className="section-heading"><div><span className="eyebrow">SAVED EVIDENCE</span><h2>Recent endpoint runs</h2><p>Reports are regenerated from saved evidence and do not require the endpoint to remain available.</p></div></section>
    <RecentRunList runs={runs.filter(run=>run.scenario?.id==="generic-endpoint")} onSelect={onSelectRun}/>
  </>;
}

function DashboardControls({ controls, gridReady, scenario, setScenario, endpointId, setEndpointId,
                             selectedProfile, setSelectedProfile, rateSteps, setRateSteps,
                             soakSeconds, setSoakSeconds, selectedSizes, setSelectedSizes,
                             runInputError, controlMessage, controlBusy, preflightResult,
                             preflightBusy, runInputKey, runPreflight, startBenchmark,
                             stopBenchmark, operationId }) {
  const profileSettings = controls.profileSettings?.[selectedProfile] || {};
  const scenarios = controls.scenarios || [];
  const endpoints = controls.endpointConfigurations || [];
  const generic = scenario === "generic-endpoint";
  const scenarioAvailable = scenarios.includes(scenario) && (!generic || endpoints.some(item => item.id === endpointId));
  const controlsReady = generic ? scenarioAvailable : scenarioAvailable && gridReady;
  const startEnabled = controls.enabled && controlsReady && !controls.running && !controlBusy;
  const presets = [10, 25, 50, 75, 100];
  const profileDescriptions = {
    smoke: "Quick low-rate check that the attached environment can serve the workload.",
    baseline: "Higher-rate baseline starting at 50 req/s, with room to compare a second load step.",
    stepped: "Higher offered-load profile for checking how request rate and routing behave under increasing traffic.",
  };
  return <section className="panel benchmark-controls" data-testid="benchmark-controls">
    <div><h3>Run a performance profile</h3>
      <p>Choose a scenario, profile, serialized request size, arrival rate, and measurement duration. The server validates and runs the selected configuration.</p>
      {!controls.enabled&&<small className="read-only-note" data-testid="control-disabled">Starting and mutation controls are disabled. Restart the localhost dashboard with <code>--allow-controls</code> to enable them.</small>}
      <fieldset className="scenario-picker" data-testid="scenario-picker"><legend>Scenario</legend>
        {scenarios.map(item=><label key={item}>
          <input type="radio" name="benchmark-scenario" value={item} checked={scenario===item} disabled={controls.running}
            onChange={()=>setScenario(item)}/>
          <span><strong>{item === "generic-endpoint" ? "Generic HTTP endpoint" : "Grid / llm-d signals"}</strong>
          <small>{item === "generic-endpoint" ? "POST JSON to a server-configured endpoint; no cluster or Grid setup is used." : "Use the combined-site handoff, authenticated EPP proof, and simulator pressure/recovery."}</small></span>
        </label>)}
      </fieldset>
      {generic && <label className="endpoint-select" htmlFor="endpoint-configuration">Configured endpoint
        <select id="endpoint-configuration" data-testid="endpoint-configuration" value={endpointId} disabled={!startEnabled}
          onChange={event=>setEndpointId(event.target.value)}>
          {endpoints.map(endpoint=><option key={endpoint.id} value={endpoint.id}>{endpoint.name} · {endpoint.endpoint}</option>)}
        </select>
      </label>}
      <div className="profile-guide" data-testid="profile-guide"><strong>Profiles and defaults</strong><ul>{(controls.profiles || []).map(name=>{
        const settings = controls.profileSettings?.[name] || {};
        const exampleRates = Array.isArray(settings.rates) ? settings.rates.join(" → ") : "unavailable";
        const sizes = (settings.requestBodySizesBytes || [null]).map(value=>value==null?"template":`${Math.round(value/1024)} KiB`).join(", ");
        const plan = generic ? (settings.genericPlanByConfiguration?.[endpointId] || settings.genericPlan) : null;
        return <li key={name}><strong>{name}</strong> — {profileDescriptions[name] || "Configured workload profile."} Example: {exampleRates} req/s, {settings.soakSeconds ?? "?"} seconds per window, {settings.repetitions ?? "?"} repeats per rate, {settings.warmupSeconds ?? "?"} seconds warmup; sizes {sizes}; cap {settings.maxArrivalRatePerSecond ?? "?"} req/s.{generic && plan ? ` Default generic plan: ${plan.measurementWindowCount} measurement windows over ${plan.durationSeconds} seconds.` : ""}</li>;
      })}</ul><small>The rate and soak inputs below override the selected profile's baseline steps and measured-window duration. Profile warmup, repetitions, and safety cap stay in effect.</small></div>
      {controlMessage && <small role="status">{controlMessage}</small>}
    </div>
    <div className="control-actions"><label htmlFor="benchmark-profile">Profile</label><select id="benchmark-profile" value={selectedProfile} disabled={!startEnabled}
      onChange={event=>setSelectedProfile(event.target.value)}>{(controls.profiles || []).map(name=><option key={name} value={name}>{name}</option>)}</select>
      <button type="button" data-testid="run-preflight" disabled={!startEnabled || Boolean(runInputError) || preflightBusy} onClick={runPreflight}>{preflightBusy ? "Checking…" : generic ? "Preflight endpoint" : "Check Grid readiness"}</button>
      {preflightResult?.key===runInputKey && <small data-testid="preflight-result">{preflightResult.data.ok ? "Preflight passed" : "Preflight blocked"}{preflightResult.data.endpointStatus ? ` · HTTP ${preflightResult.data.endpointStatus}` : ""}{preflightResult.data.plan ? ` · ${preflightResult.data.plan.windowCount} planned windows · ${preflightResult.data.plan.durationSeconds}s` : ""}{preflightResult.data.error ? ` · ${preflightResult.data.error}` : ""}</small>}
      <button data-testid="start-benchmark" disabled={!startEnabled || Boolean(runInputError)} onClick={startBenchmark}>{controlBusy ? "Starting…" : "Start benchmark"}</button>
      <button data-testid="stop-benchmark" disabled={!controls.running || controls.operation !== "benchmark" || !operationId || controlBusy} onClick={stopBenchmark}>{controls.stopRequested ? (generic ? "Stopping safely…" : "Restoring safely…") : "Stop safely"}</button>
    </div>
    <div className="run-parameters" data-testid="run-parameters">
      <label htmlFor="arrival-rate-steps">Arrival rate steps (requests/second)</label>
      <input id="arrival-rate-steps" data-testid="arrival-rate-steps" value={rateSteps} disabled={!startEnabled}
        onChange={event=>setRateSteps(event.target.value)} placeholder="25,50" inputMode="decimal" />
      <small>Each value is a separate offered-rate step. For example, 25,50 runs a window at 25 req/s and then at 50 req/s. {generic ? "Generic runs send traffic only to the selected endpoint and do not apply synthetic pressure." : `Grid signal, direct-reference, pressure, and recovery phases use the profile signal rate (${profileSettings.signalRate ?? "unavailable"} req/s).`} Profile cap: {profileSettings.maxArrivalRatePerSecond ?? "unavailable"} req/s.</small>
      <div className="rate-presets" aria-label="Arrival rate presets">{presets.map(rate=><button type="button" key={rate} data-testid={`rate-preset-${rate}`}
        disabled={!startEnabled || rate > Number(profileSettings.maxArrivalRatePerSecond || 0)} onClick={()=>setRateSteps(String(rate))}>{rate} req/s</button>)}</div>
      <label htmlFor="soak-seconds">Soak per measurement window (seconds)</label>
      <input id="soak-seconds" data-testid="soak-seconds" type="number" min="15" max="600" step="1" value={soakSeconds} disabled={!startEnabled}
        onChange={event=>setSoakSeconds(event.target.value)} />
      <small>Each measured window uses this duration. Warmup stays at the profile's configured length. Choose 15–600 seconds.</small>
      <fieldset className="body-size-picker" data-testid="body-size-picker"><legend>Serialized JSON request sizes</legend>
        {[null, ...(profileSettings.requestBodySizesBytes || []).filter(value=>value!=null), ...[16384,65536,262144].filter(value=>!(profileSettings.requestBodySizesBytes||[]).includes(value))]
          .filter((value,index,all)=>all.findIndex(other=>other===value)===index).map(value=>{
            const key=value==null?"template":String(value);
            const label=value==null?"Template size":`${Math.round(value/1024)} KiB (${value.toLocaleString()} bytes)`;
            return <label key={key}><input type="checkbox" checked={selectedSizes.includes(key)} disabled={!startEnabled}
              onChange={event=>setSelectedSizes(current=>event.target.checked?[...current,key]:current.filter(item=>item!==key))}/>{label}</label>;
          })}
      </fieldset>
      <small>Gridzilla counts exact serialized UTF-8 bytes and records requested and k6-verified size for each window. Padding is generated by k6; request bodies are not saved. A template that cannot fit a selected size is rejected before traffic.</small>
      {runInputError && <small className="input-error" role="alert">{runInputError}</small>}
    </div>
  </section>;
}

function Gridzilla() {
  const [mode, setMode] = useState("live");
  const [live, setLive] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selectedRun, setSelectedRun] = useState("");
  const [saved, setSaved] = useState(null);
  const [runSearch, setRunSearch] = useState("");
  const [compareIds, setCompareIds] = useState([]);
  const [comparisonReports, setComparisonReports] = useState([]);
  const [showComparison, setShowComparison] = useState(false);
  const [runTab, setRunTab] = useState("overview");
  const [error, setError] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("baseline");
  const [scenario, setScenario] = useState("grid-llmd-signals");
  const [endpointId, setEndpointId] = useState("");
  const [selectedSizes, setSelectedSizes] = useState(["template"]);
  const [preflightResult, setPreflightResult] = useState(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [activeOperationId, setActiveOperationId] = useState("");
  const [rateSteps, setRateSteps] = useState("50,75");
  const [soakSeconds, setSoakSeconds] = useState("30");
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
      setRunTab("overview");
      setShowComparison(false);
      setMode("saved");
      setPendingLaunch(null);
      setControlMessage(`Benchmark ${outcome.toLowerCase()}; showing its saved report.`);
    }
  }, [live, pendingLaunch]);

  const localSite = live?.localOverlay?.site;
  const liveSites = live?.sites || [];
  const localOverlay = live?.localOverlay || {};
  const crossSite = live?.crossSite || {};
  const allLive = live?.status === "LIVE" && liveSites.length > 0 && liveSites.every(site => site.status === "LIVE");
  const controls = live?.controls || {};
  const availableScenarios = controls.scenarios || [];
  const effectiveScenario = availableScenarios.includes(scenario) ? scenario : (availableScenarios[0] || scenario);
  const endpointConfigurations = controls.endpointConfigurations || [];
  const endpointIdsKey = endpointConfigurations.map(item=>item.id).join("|");
  const scenarioKey = availableScenarios.join("|");
  useEffect(() => {
    if (endpointConfigurations.length && !endpointConfigurations.some(item=>item.id===endpointId)) setEndpointId(endpointConfigurations[0].id);
  }, [endpointIdsKey]);
  useEffect(() => {
    if (availableScenarios.length && !availableScenarios.includes(scenario)) setScenario(availableScenarios[0]);
  }, [scenarioKey]);
  const selectedProfileSettings = controls.profileSettings?.[selectedProfile] || {};
  const profileSizeKey = JSON.stringify(selectedProfileSettings.requestBodySizesBytes || [null]);
  useEffect(() => {
    const values = JSON.parse(profileSizeKey).map(value=>value==null?"template":String(value));
    setSelectedSizes(values.length ? values : ["template"]);
  }, [selectedProfile, profileSizeKey]);
  const selectedSizesBytes = selectedSizes.map(value=>value==="template"?null:Number(value));
  const runInputKey = JSON.stringify([effectiveScenario, endpointId, selectedProfile, rateSteps, soakSeconds, selectedSizesBytes]);
  useEffect(() => { setPreflightResult(null); }, [runInputKey]);
  const gridReady = allLive && live?.gates?.simulators === "READY" && live?.gates?.eppMetrics === "LIVE" &&
    live?.gates?.providers === "READY" && live?.gates?.overlays === "LIVE" && live?.gates?.gateways === "READY";
  const parsedRateSteps = rateSteps.split(",").map(part=>part.trim());
  const numericRates = parsedRateSteps.map(Number);
  const runInputError = !selectedSizes.length || selectedSizes.length > 6
    ? "Select 1–6 serialized request sizes."
    : parsedRateSteps.length < 1 || parsedRateSteps.length > 6 || parsedRateSteps.some(part=>!part) ||
    numericRates.some(rate=>!Number.isFinite(rate) || rate <= 0)
      ? "Enter 1–6 positive requests-per-second values. The shared server planner checks ordering and the profile's safety cap."
      : !/^\d+$/.test(String(soakSeconds))
        ? "Enter a whole-number measurement duration in seconds. The shared server planner checks the allowed range and total run bounds." : "";
  const changeProfile = name => {
    setSelectedProfile(name);
    const settings = controls.profileSettings?.[name];
    if (settings) {
      setRateSteps(settings.rates.join(","));
      setSoakSeconds(String(settings.soakSeconds));
    }
  };

  const requestPreflight = async () => {
    setPreflightBusy(true);
    try {
      const body = {scenario:effectiveScenario, profile:selectedProfile, rates:rateSteps,
        soakSeconds:Number(soakSeconds), requestBodySizesBytes:selectedSizesBytes};
      if (effectiveScenario === "generic-endpoint") body.configurationId = endpointId;
      const response = await fetch("/api/preflight", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error?.message || `Preflight rejected (${response.status})`);
      setPreflightResult({key:runInputKey,data:value});
      setControlMessage(value.ok ? "Preflight passed." : "Preflight is blocked; review the result before starting.");
    } catch (cause) {
      setPreflightResult({key:runInputKey,data:{ok:false,error:cause instanceof Error?cause.message:"Preflight failed"}});
      setControlMessage(cause instanceof Error?cause.message:"Preflight failed");
    } finally { setPreflightBusy(false); }
  };

  const toggleCompare = id => setCompareIds(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  const compareSelected = async () => {
    if (compareIds.length < 2) return;
    setControlBusy(true);
    try {
      const responses = await Promise.all(compareIds.map(async id => {
        const response = await fetch(`/api/runs/${encodeURIComponent(id)}`, {cache:"no-store"});
        if (!response.ok) throw new Error(`Could not load run ${id} (${response.status})`);
        return response.json();
      }));
      setComparisonReports(responses);
      setShowComparison(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Selected runs could not be loaded");
    } finally {
      setControlBusy(false);
    }
  };

  const startBenchmark = async () => {
    const scenarioLabel=effectiveScenario==="generic-endpoint"?`the configured endpoint ${endpointConfigurations.find(item=>item.id===endpointId)?.name||endpointId}`:"the Grid / llm-d signals environment";
    if (!window.confirm(`Run ${selectedProfile} against ${scenarioLabel} at ${rateSteps} requests/second, with ${soakSeconds} seconds per window?`)) return;
    setControlBusy(true);
    setControlMessage(`Checking readiness before starting ${selectedProfile}…`);
    try {
      const preflightBody = {scenario:effectiveScenario, profile:selectedProfile, rates:rateSteps,
        soakSeconds:Number(soakSeconds), requestBodySizesBytes:selectedSizesBytes};
      if (effectiveScenario === "generic-endpoint") preflightBody.configurationId = endpointId;
      const preflightResponse = await fetch("/api/preflight", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(preflightBody)});
      const preflight = await preflightResponse.json();
      if (!preflightResponse.ok) throw new Error(preflight.error?.message || `Preflight rejected (${preflightResponse.status})`);
      if (!preflight.ok) throw new Error("Preflight did not pass; no benchmark traffic was started.");
      setPreflightResult({key:runInputKey,data:preflight});
      const previousRunId = live?.controls?.lastRunId || live?.benchmark?.runId || null;
      const response = await fetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({scenario:effectiveScenario, configurationId:endpointId||undefined,
          profile:selectedProfile, rates:rateSteps, soakSeconds:Number(soakSeconds), requestBodySizesBytes:selectedSizesBytes})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Run start rejected (${response.status})`);
      setActiveOperationId(result.operationId || "");
      await refreshLive();
      setPendingLaunch({previousRunId, operationId:result.operationId});
      setControlMessage(`${selectedProfile} accepted for ${scenarioLabel}: ${rateSteps} requests/second, ${soakSeconds} seconds per measured window.`);
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Benchmark start was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  const stopBenchmark = async () => {
    const generic=effectiveScenario==="generic-endpoint";
    if (!window.confirm(generic?"Stop this endpoint benchmark safely? Gridzilla will stop the active k6 window and save partial evidence.":"Stop the current Grid benchmark? Gridzilla will restore simulator state and verify routing recovery before it finishes.")) return;
    setControlBusy(true);
    try {
      const response = await fetch("/api/stop", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({operationId:live?.controls?.operationId||activeOperationId})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Stop rejected (${response.status})`);
      setControlMessage(result.restoration || "Stop requested; Gridzilla is restoring simulator state.");
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Stop request was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  const applyPressure = async (site, value) => {
    setControlBusy(true);
    setControlMessage("Checking readiness and capturing the current simulator configuration…");
    try {
      const response = await fetch("/api/pressure", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({site, value})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Pressure request rejected (${response.status})`);
      await refreshLive();
      setControlMessage(`Applying ${result.value} / ${result.capacity} temporary queue pressure to ${result.pool} at ${result.site}; Gridzilla will verify route selection and restore the baseline.`);
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Pressure request was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  const restorePressure = async () => {
    setControlBusy(true);
    try {
      const response = await fetch("/api/pressure/restore", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({operationId:live?.controls?.operationId||activeOperationId})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || `Restore request rejected (${response.status})`);
      setControlMessage(result.restoration || "Restoration requested; routing recovery will be verified.");
    } catch (cause) {
      setControlMessage(cause instanceof Error ? cause.message : "Restore request was rejected");
    } finally {
      setControlBusy(false);
    }
  };

  return <main className="app-shell">
    <header className="topbar"><div className="brand"><span className="brand-mark">G</span><div><strong>GRIDZILLA</strong><small>Signals-based routing performance</small></div></div>
      <nav><button className={mode === "live" ? "active" : ""} onClick={()=>setMode("live")}>Live environment</button><button className={mode === "saved" ? "active" : ""} onClick={()=>setMode("saved")}>Runs</button></nav>
      <div className="topbar-actions"><div className="header-status"><Badge value={mode === "saved" ? "HISTORICAL" : effectiveScenario === "generic-endpoint" ? (controls.running ? "RUNNING" : endpointConfigurations.length ? "CONFIGURED" : "UNAVAILABLE") : allLive ? "LIVE" : live?.status || "UNAVAILABLE"} testId="live-badge" /><span>{mode === "live" && controls.enabled ? "local controls enabled" : "read-only"}</span></div>
        <a className="github-link" href="https://github.com/nerdalert/gridzilla" target="_blank" rel="noreferrer" aria-label="Gridzilla on GitHub" title="Gridzilla on GitHub">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 .9a11.1 11.1 0 0 0-3.51 21.63c.55.1.76-.24.76-.54v-2.1c-3.1.68-3.76-1.31-3.76-1.31-.5-1.29-1.24-1.64-1.24-1.64-1.01-.69.08-.68.08-.68 1.12.08 1.71 1.15 1.71 1.15.99 1.7 2.6 1.21 3.23.93.1-.72.39-1.21.7-1.49-2.48-.28-5.09-1.24-5.09-5.52 0-1.22.44-2.21 1.15-2.99-.12-.28-.5-1.42.11-2.95 0 0 .94-.3 3.05 1.14a10.6 10.6 0 0 1 5.55 0c2.12-1.44 3.05-1.14 3.05-1.14.61 1.53.23 2.67.11 2.95.72.78 1.15 1.77 1.15 2.99 0 4.29-2.61 5.24-5.1 5.52.4.34.75 1 .75 2.02v3.12c0 .3.2.65.77.54A11.1 11.1 0 0 0 12 .9Z"/></svg>
        </a>
      </div>
    </header>
    {error && <div className="alert" role="status">{error}</div>}
    {mode === "live" ? <>
      <DashboardControls controls={controls} gridReady={gridReady} scenario={effectiveScenario} setScenario={setScenario}
        endpointId={endpointId} setEndpointId={setEndpointId} selectedSizes={selectedSizes} setSelectedSizes={setSelectedSizes}
        preflightResult={preflightResult} preflightBusy={preflightBusy} runInputKey={runInputKey} runPreflight={requestPreflight}
        operationId={live?.controls?.operationId||activeOperationId}
        selectedProfile={selectedProfile} setSelectedProfile={changeProfile} rateSteps={rateSteps} setRateSteps={setRateSteps}
        soakSeconds={soakSeconds} setSoakSeconds={setSoakSeconds} runInputError={runInputError} controlMessage={controlMessage}
        controlBusy={controlBusy} startBenchmark={startBenchmark} stopBenchmark={stopBenchmark}
        />
      {effectiveScenario === "generic-endpoint" ? <GenericLiveView endpoint={endpointConfigurations.find(item=>item.id===endpointId)}
        operation={live?.operationState || null} runs={runs} onSelectRun={id=>{setSelectedRun(id);setRunTab("overview");setMode("saved");}} /> : <>
      <section className="hero panel"><div><span className="eyebrow">{boundaryLabel(live?.phase || "CONNECTING")}</span><h1>Live Grid + llm-d signal path</h1>
        <p>llm-d-sim → authenticated EPP metrics → Grid score/rank → distributed overlay → Gateway revision → attributed request.</p>
        <small>Synthetic simulator signals are separate from measured Gateway traffic. {controls.enabled ? "Local controls run bounded profiles and restore simulator state." : "This view is read-only and does not create load or modify the cluster."}</small></div>
        <div className="hero-meta"><Badge value={live?.status || "UNAVAILABLE"} /><span>last sample {live?.observedAtUtc ? new Date(live.observedAtUtc).toLocaleTimeString() : "unavailable"}</span><span>source {live?.source?.revision ? `${live.source.revision.slice(0,12)}…` : "unavailable"}</span></div></section>
      <BenchmarkProgress benchmark={live?.benchmark} />
      <section className="state-strip" data-testid="readiness-states"><div><span>Simulators</span><Badge value={live?.gates?.simulators || "UNAVAILABLE"} /></div><div><span>EPP authenticated metrics</span><Badge value={live?.gates?.eppMetrics || "UNAVAILABLE"} /></div><div><span>Providers</span><Badge value={live?.gates?.providers || "UNAVAILABLE"} /></div><div><span>Overlays</span><Badge value={live?.gates?.overlays || "UNAVAILABLE"} /></div><div><span>Gateways serving</span><Badge value={live?.gates?.gateways || "UNAVAILABLE"} /></div></section>
      <section className="section-heading"><div><span className="eyebrow">POOL SIGNALS</span><h2>Simulator and EPP state</h2><p>Freshness is reported from the authenticated EPP scrape; missing timestamps are not shown as measured sample age.</p></div><span>{liveSites.length}/{live?.sites?.length??"?"} sites observed</span></section>
      <section className="site-grid">{liveSites.map(site=><PoolCard key={site.name} site={site} controls={controls} controlsReady={gridReady} controlBusy={controlBusy} applyPressure={applyPressure} restorePressure={restorePressure} />)}{liveSites.length===0 && <div className="panel empty" data-testid="empty-state">No live cluster state has been observed. Check the handoff and local kubeconfig files.</div>}</section>
      <section className="overlay-layout">
        <CandidateTable title={`Local published overlay · ${localSite || "site unavailable"}`} candidates={localOverlay.candidates || []} testId="local-overlay" />
        <section className="panel" data-testid="cross-site-snapshot"><div className="panel-title"><div><h3>Cross-site overlay and Gateway snapshot</h3><p>Per-site revisions are compared independently; local overlay and peer distribution are distinct observations.</p></div><Badge value={crossSite.status || "UNAVAILABLE"} /></div>
          <div className="site-revisions">{(crossSite.sites || []).map(site=><div key={site.name} className="site-revision"><strong>{site.name}</strong><span>published {shortRevision(site.overlayRevision)}</span><span>serving {shortRevision(site.servingRevision)}</span><Badge value={site.status || "UNAVAILABLE"} /></div>)}
            {!crossSite.sites?.length && <div className="empty">No cross-site snapshot is available.</div>}</div></section>
      </section>
      <section className="panel timeline-panel" data-testid="signal-timeline"><div className="panel-title"><div><h3>Signal-to-route timeline</h3><p>Boundary events use local monotonic durations; timestamps from different clusters may have clock skew.</p></div><Badge value={live?.transition?.status || "UNAVAILABLE"} /></div>
        <div className="timeline">{(live?.timeline || []).map((item,index)=><div className="timeline-item" key={`${item.boundary}-${index}`}><span className="timeline-dot" /><strong title={item.boundary}>{boundaryLabel(item.boundary)}</strong><span>{item.observedAtUtc || "time unavailable"}</span><small>{item.durationMs == null ? "duration not measured" : `${formatNumber(item.durationMs)} ms`}</small></div>)}
          {!live?.timeline?.length && <div className="empty">No transition has been run in this attached environment.</div>}</div></section>
      <section className="section-heading"><div><span className="eyebrow">RECENT RUNS</span><h2>Saved evidence</h2><p>Browse reports without contacting the clusters.</p></div><button onClick={()=>setMode("saved")}>Open run browser</button></section>
      <RecentRunList runs={runs} onSelect={id=>{setSelectedRun(id);setRunTab("overview");setMode("saved");}} />
      </>}
    </> : <>
      <section className="hero panel"><div><span className="eyebrow">OFFLINE EVIDENCE</span><h1>Runs</h1><p>Search normalized reports and historical evidence. All views are regenerated from saved files; no cluster requests are made.</p></div><Badge value="HISTORICAL" /></section>
      <RunsBrowser runs={runs} selected={selectedRun} onSelect={id=>{setSelectedRun(id);setRunTab("overview");setShowComparison(false);}}
        search={runSearch} setSearch={setRunSearch} compareIds={compareIds} toggleCompare={toggleCompare} compare={compareSelected}/>
      {showComparison && <ComparisonView reports={comparisonReports}/>}
      {saved?.run?.status === "RUNNING" && saved.run.progress && <BenchmarkProgress benchmark={saved.run.progress}/>}
      {saved ? <section className="panel saved-detail normalized-saved-detail" data-testid="saved-run-detail">
        <div className="report-tabs" role="tablist" aria-label="Run report views">
          <button role="tab" aria-selected={runTab==="overview"} onClick={()=>setRunTab("overview")}>Run overview</button>
          {saved.signals?.kind && <button role="tab" aria-selected={runTab==="signals"} data-testid="signals-tab" onClick={()=>setRunTab("signals")}>llm-d signals</button>}
          <button role="tab" aria-selected={runTab==="resources"} onClick={()=>setRunTab("resources")}>Resources &amp; evidence</button>
        </div>
        {runTab==="overview" && <RunOverview report={saved}/>}
        {runTab==="signals" && <SignalsView trace={saved.signals}/>}
        {runTab==="resources" && <ResourcesEvidenceView report={saved} selectedRun={selectedRun}/>}
      </section> : <section className="panel empty" data-testid="saved-empty">{runs.length ? "Select a run to view its report." : "No reportable evidence directories found."}</section>}
        </>}
    <footer><span>Gridzilla · generic endpoint and Grid/llm-d signals scenarios</span><span>Observed HTTP or synthetic routing/control-path results; not a GPU, model-quality, or real-token-throughput benchmark</span></footer>
  </main>;
}

function RecentRunList({ runs = [], selected, onSelect }) {
  return <div className="run-list">{runs.map(run=><button key={run.id} className={`run-row ${selected===run.id?"selected":""}`} onClick={()=>onSelect(run.id)} data-testid={`run-${run.id}`}>
    <span><strong>{run.id}</strong><small>{run.startedAtUtc || "time unavailable"}</small></span><Badge value={run.status || "HISTORICAL"} /><span className="arrow">›</span>
  </button>)}</div>;
}

export default Gridzilla;
