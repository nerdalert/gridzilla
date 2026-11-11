import http from "k6/http";
import { check, fail } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const target = __ENV.GZ_URL;
const scenario = __ENV.GZ_SCENARIO || "grid";
const requestTemplate = JSON.parse(__ENV.GZ_BODY_JSON || "{}");
const padJsonPointer = __ENV.GZ_PAD_JSON_POINTER || "";
const targetBodyBytes = __ENV.GZ_TARGET_BODY_BYTES ? Number(__ENV.GZ_TARGET_BODY_BYTES) : null;
const requestBody = buildRequestBody(requestTemplate, padJsonPointer, targetBodyBytes);
const serializedBody = JSON.stringify(requestBody);
const serializedBodyBytes = utf8ByteLength(serializedBody);
if (targetBodyBytes !== null && serializedBodyBytes !== targetBodyBytes) {
  throw new Error(`serialized request template cannot produce requested ${targetBodyBytes}-byte body`);
}
const credentialPath = __ENV.GZ_CREDENTIAL_FILE;
const credential = credentialPath ? open(credentialPath).trim() : "";
const authHeader = __ENV.GZ_AUTH_HEADER || "Authorization";
const credentialPrefix = __ENV.GZ_CREDENTIAL_PREFIX || "Bearer ";
const requestIdHeader = __ENV.GZ_REQUEST_ID_HEADER || "X-Request-ID";
const requestIdHeaders = JSON.parse(__ENV.GZ_REQUEST_ID_HEADERS || JSON.stringify([requestIdHeader]));
const attribution = JSON.parse(__ENV.GZ_ATTRIBUTION_JSON || "{}");
const allowedSites = new Set(JSON.parse(__ENV.GZ_ALLOWED_SITES || "[]"));
const allowedPools = new Set(JSON.parse(__ENV.GZ_ALLOWED_POOLS || "[]"));
const allowedBackends = new Set(JSON.parse(__ENV.GZ_ALLOWED_BACKENDS || "[]"));
const allowedProviders = new Set(JSON.parse(__ENV.GZ_ALLOWED_PROVIDERS || "[]"));
const providerToSite = JSON.parse(__ENV.GZ_PROVIDER_TO_SITE || "{}");
const providerToPool = JSON.parse(__ENV.GZ_PROVIDER_TO_POOL || "{}");
const providerToBackend = JSON.parse(__ENV.GZ_PROVIDER_TO_BACKEND || "{}");
const requiredResponseHeaders = JSON.parse(__ENV.GZ_REQUIRED_RESPONSE_HEADERS || "[]");
const maxResponseBytes = Number(__ENV.GZ_MAX_RESPONSE_BYTES || 65536);
const targetRate = Number(__ENV.GZ_RATE || 1);
const rateTimeUnit = __ENV.GZ_TIME_UNIT || "5s";
const duration = __ENV.GZ_DURATION || "15s";
const preAllocatedVUs = Number(__ENV.GZ_PREALLOCATED_VUS || 1);
const maxVUs = Number(__ENV.GZ_MAX_VUS || 2);
const timeout = __ENV.GZ_REQUEST_TIMEOUT || "15s";
const phase = __ENV.GZ_PHASE || "measurement";
const clientSite = __ENV.GZ_CLIENT_SITE || "unknown";
const ingressName = __ENV.GZ_INGRESS_NAME || "gateway";
const referenceMode = (__ENV.GZ_REFERENCE_MODE || "false") === "true";
const expectedStatuses = new Set(JSON.parse(__ENV.GZ_EXPECTED_STATUSES || "[]"));
const genericDimensions = JSON.parse(__ENV.GZ_GENERIC_DIMENSIONS || "{}");

const attributionFailures = new Counter("gridzilla_attribution_failures");
const genericAttribution = new Counter("gridzilla_generic_attribution");
const gatewayRequests = new Counter("gridzilla_gateway_requests");
const gatewayLatency = new Trend("gridzilla_gateway_latency_ms", true);
const gatewaySuccess = new Rate("gridzilla_gateway_success");
const endpointSuccess = new Rate("gridzilla_endpoint_success");
const outputLimitFailures = new Counter("gridzilla_output_limit_failures");
const responseBytes = new Counter("gridzilla_response_bytes");
const attributedRoutes = new Counter("gridzilla_attributed_routes");
const requestBodyBytes = new Trend("gridzilla_request_body_bytes", true);

export const options = {
  hosts: JSON.parse(__ENV.GZ_HOSTS_JSON || "{}"),
  scenarios: {
    gridzilla: {
      executor: "constant-arrival-rate",
      rate: targetRate,
      timeUnit: rateTimeUnit,
      duration,
      preAllocatedVUs,
      maxVUs,
      gracefulStop: "5s",
      exec: "gatewayTraffic",
      tags: { phase, client_site: clientSite, ingress: ingressName },
    },
  },
  userAgent: "gridzilla-k6/0.1.0",
  noConnectionReuse: false,
};

function responseHeader(response, name) {
  if (!name) return "";
  const targetName = name.toLowerCase();
  for (const [key, value] of Object.entries(response.headers || {})) {
    if (key.toLowerCase() === targetName) return String(value);
  }
  return "";
}

function utf8ByteLength(value) {
  let bytes = 0;
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff && index + 1 < value.length) {
      const next = value.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        index += 1;
      } else bytes += 3;
    } else bytes += 3;
  }
  return bytes;
}

function buildRequestBody(template, pointer, targetBytes) {
  if (targetBytes === null) return template;
  if (!Number.isSafeInteger(targetBytes) || targetBytes < 1 || targetBytes > 1048576 || !pointer) {
    throw new Error("invalid bounded request body size or missing pad JSON pointer");
  }
  const parts = pointer.split("/").slice(1).map(part => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  let parent = template;
  for (const part of parts.slice(0, -1)) {
    const index = Array.isArray(parent) && /^\d+$/.test(part) ? Number(part) : part;
    if (parent === null || typeof parent !== "object" || !(index in parent)) {
      throw new Error("pad JSON pointer does not resolve to the configured request template");
    }
    parent = parent[index];
  }
  const leaf = parts[parts.length - 1];
  const key = Array.isArray(parent) && /^\d+$/.test(leaf) ? Number(leaf) : leaf;
  if (parent === null || typeof parent !== "object" || !(key in parent) || typeof parent[key] !== "string") {
    throw new Error("pad JSON pointer must name an existing string field");
  }
  const base = JSON.stringify(template);
  const delta = targetBytes - utf8ByteLength(base);
  if (delta < 0) throw new Error(`requested body size ${targetBytes} is smaller than template (${utf8ByteLength(base)} bytes)`);
  parent[key] += "x".repeat(delta);
  return template;
}

function boundedSite(value) {
  return allowedSites.has(value) ? value : "unknown";
}

export function gatewayTraffic() {
  // Request IDs are intentionally excluded from metric tags to keep cardinality bounded.
  const requestId = `gz-${clientSite}-${__VU}-${__ITER}-${Date.now()}`;
  const headers = { "content-type": "application/json" };
  for (const header of requestIdHeaders) headers[header] = requestId;
  if (credential) headers[authHeader] = `${credentialPrefix}${credential}`;
  const response = http.post(target, serializedBody, {
    headers,
    timeout,
    tags: { ingress: ingressName, client_site: clientSite, phase },
  });

  const provider = referenceMode ? "reference" : responseHeader(response, attribution.providerHeader);
  const site = responseHeader(response, attribution.siteHeader);
  const pool = responseHeader(response, attribution.poolHeader);
  const backend = responseHeader(response, attribution.backendHeader);
  const backendPod = responseHeader(response, attribution.backendPodHeader);
  const backendRequestId = responseHeader(response, attribution.requestIdEchoHeader);
  const providerGateway = attribution.providerGatewayHeader ? responseHeader(response, attribution.providerGatewayHeader) : "";
  const boundedProvider = allowedProviders.has(provider) ? provider : "unknown";
  const boundedAttributionSite = boundedSite(site);
  const observedPool = pool || providerToPool[site] || "";
  const boundedPool = allowedPools.has(observedPool) ? observedPool : "unknown";
  const boundedBackend = allowedBackends.has(backend) ? backend : "unknown";
  const statusClass = `${Math.floor(response.status / 100)}xx`;
  const boundedDimensions = {};
  if (scenario === "generic") {
    for (const [name, config] of Object.entries(genericDimensions)) {
      const observed = responseHeader(response, config.header);
      // A badly configured endpoint could echo an auth value in a dimension
      // header. Keep credential contents out of metric tags even in that case.
      boundedDimensions[name] = observed !== credential && (config.allowedValues || []).includes(observed) ? observed : "unknown";
    }
    if (Object.keys(genericDimensions).length > 0) {
      const complete = Object.values(boundedDimensions).every(value => value !== "unknown");
      genericAttribution.add(1, {phase, quality: complete ? "complete" : "incomplete"});
    }
  }
  const tags = {
    phase,
    ingress: ingressName,
    client_site: clientSite,
    status_class: statusClass,
  };
  if (scenario === "grid") {
    tags.provider_site = boundedAttributionSite;
    tags.pool = boundedPool;
  }
  for (const [name, value] of Object.entries(boundedDimensions)) tags[`dimension_${name}`] = value;
  let attributionProblem = "";
  if (scenario === "grid" && !referenceMode) {
    if (!provider || !allowedProviders.has(provider)) attributionProblem = "unknown_provider";
    else if (!site || !allowedSites.has(site)) attributionProblem = "unknown_site";
    else if (!observedPool || !allowedPools.has(observedPool)) attributionProblem = "unknown_pool";
    else if (!backend || !allowedBackends.has(backend)) attributionProblem = "unknown_backend";
    else if (providerGateway && providerGateway !== site) attributionProblem = "provider_gateway_mismatch";
    else if (providerToBackend[provider] && providerToBackend[provider] !== backend) attributionProblem = "provider_backend_mismatch";
    else if (providerToSite[provider] && providerToSite[provider] !== site) attributionProblem = "provider_site_mismatch";
    else if (!backendPod || !attribution.backendPodPrefixBySite[site] || !backendPod.startsWith(attribution.backendPodPrefixBySite[site])) attributionProblem = "backend_pod_site_mismatch";
    else if (!backendRequestId || backendRequestId !== requestId) attributionProblem = "backend_request_id_mismatch";
    else if (requiredResponseHeaders.some(name => !responseHeader(response, name))) attributionProblem = "missing_required_attribution_header";
  }
  requestBodyBytes.add(serializedBodyBytes, {phase, body_size: bodySizeClass(serializedBodyBytes)});
  gatewayRequests.add(1, tags);
  gatewayLatency.add(response.timings.duration, tags);
  const success = scenario === "generic" && expectedStatuses.size
    ? expectedStatuses.has(response.status)
    : response.status >= 200 && response.status < 300;
  gatewaySuccess.add(success, tags);
  endpointSuccess.add(success, tags);
  const responseByteCount = utf8ByteLength(response.body || "");
  responseBytes.add(responseByteCount, tags);
  if (scenario === "grid" && !referenceMode && !attributionProblem) {
    attributedRoutes.add(1, { phase, site: boundedAttributionSite, pool: boundedPool, status_class: statusClass });
  }

  if (attributionProblem) {
    attributionFailures.add(1, { phase, ingress: ingressName, reason: attributionProblem });
    // The controller watches this marker and stops the k6 process group immediately.
    console.error(`GRIDZILLA_ATTRIBUTION_FAILURE ${JSON.stringify({ requestId, status: response.status, reason: attributionProblem })}`);
  }

  const outputTooLarge = responseByteCount > maxResponseBytes;
  if (outputTooLarge) {
    outputLimitFailures.add(1, { phase });
    console.error(`GRIDZILLA_OUTPUT_LIMIT_FAILURE ${JSON.stringify({ requestId, responseBytes: responseByteCount, limit: maxResponseBytes })}`);
  }

  check(response, {
    "endpoint returned expected status": () => success,
    "response stayed within profile byte cap": () => !outputTooLarge,
    "attribution fields are valid": () => !attributionProblem,
  });

  if (attributionProblem || outputTooLarge) fail("Gridzilla contract failure; controller captures diagnostics and stops traffic");
  if (!success) {
    console.error(`GRIDZILLA_ENDPOINT_STATUS_FAILURE ${JSON.stringify({requestId, status: response.status})}`);
    fail("endpoint returned a status outside its configured expected status set");
  }
}

function bodySizeClass(bytes) {
  if (bytes === 16384) return "16KiB";
  if (bytes === 65536) return "64KiB";
  if (bytes === 262144) return "256KiB";
  return "other";
}

export function handleSummary(data) {
  const path = __ENV.GZ_SUMMARY_PATH;
  const output = JSON.stringify(data, null, 2) + "\n";
  return path ? { [path]: output, stdout: output } : { stdout: output };
}
